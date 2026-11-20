"""GitHub as the App: installation tokens, and one request at a time against the REST API.

The App's private key is the one key pair in the system (spec section 7). It signs a 9-minute JWT,
which buys an hour-long token per installation; the token is cached and reused until 5 minutes
before it expires. No token is ever logged, written to disk, or put on a command line.

Requests are synchronous (urllib): the scheduler posts inline, and intake runs reads in a thread.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import jwt

log = logging.getLogger(__name__)

API = "https://api.github.com"
REFRESH_BEFORE = 300  # seconds: a token this close to expiry is replaced
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


class RateLimited(GitHubError):
    def __init__(self, status: int, message: str, reset_at: float | None):
        super().__init__(status, message)
        self.reset_at = reset_at


@dataclass
class Response:
    status: int
    body: Any
    headers: dict[str, str]


def _send(method: str, url: str, headers: dict[str, str], body: Any = None, timeout: float = 30) -> Response:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return Response(r.status, json.loads(raw) if raw else None, {k.lower(): v for k, v in r.headers.items()})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = {"message": raw.decode(errors="replace")[:300]}
        return Response(e.code, parsed, {k.lower(): v for k, v in (e.headers or {}).items()})


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "doppel-runtime",
    }


class AppAuth:
    """The App's identity: a JWT from its private key, and a cached token per installation."""

    def __init__(self, app_id: str, private_key: str, api_url: str = API, clock: Callable[[], float] = time.time):
        self.app_id, self._key, self.api_url, self.clock = str(app_id), private_key, api_url.rstrip("/"), clock
        self._tokens: dict[int, tuple[str, float]] = {}  # installation -> (token, expires at)
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # keep the key out of logs and tracebacks
        return f"AppAuth(app_id={self.app_id!r})"

    def jwt(self) -> str:
        now = int(self.clock())
        # Backdated a minute for clock drift; GitHub allows at most 10 minutes.
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self._key, algorithm="RS256")

    def token(self, installation: int) -> str:
        with self._lock:
            cached = self._tokens.get(installation)
            if cached and cached[1] - self.clock() > REFRESH_BEFORE:
                return cached[0]
            r = _send("POST", f"{self.api_url}/app/installations/{installation}/access_tokens", _headers(self.jwt()))
            if r.status != 201:
                raise GitHubError(r.status, f"installation {installation}: {(r.body or {}).get('message', '')}")
            expires = datetime.fromisoformat(r.body["expires_at"].replace("Z", "+00:00")).timestamp()
            self._tokens[installation] = (r.body["token"], expires)
            log.info("github: token for installation %s, valid until %s", installation, r.body["expires_at"])
            return r.body["token"]

    def forget(self, installation: int) -> None:
        with self._lock:
            self._tokens.pop(installation, None)


class GitHubClient:
    """Requests as the installation that covers a repo. A 401 drops the cached token and retries once."""

    def __init__(self, auth: AppAuth, installations: dict[str, int]):
        self.auth = auth
        self.installations = installations  # repo (owner/name) -> installation id
        self.rate_remaining: dict[int, int] = {}

    def installation(self, repo: str) -> int:
        if not REPO.match(repo) or any(part in (".", "..") for part in repo.split("/")):
            raise ValueError(f"not a repo name: {repo!r}")
        try:
            return self.installations[repo]
        except KeyError:
            raise GitHubError(404, f"no installation configured for {repo}") from None

    def request(self, method: str, repo: str, path: str, *, params: dict | None = None, body: Any = None) -> Response:
        inst = self.installation(repo)
        url = f"{self.auth.api_url}/repos/{repo}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in (1, 2):
            r = _send(method, url, _headers(self.auth.token(inst)), body)
            if r.status == 401 and attempt == 1:  # revoked or expired early: a new token, once
                self.auth.forget(inst)
                continue
            break
        if "x-ratelimit-remaining" in r.headers:
            self.rate_remaining[inst] = int(r.headers["x-ratelimit-remaining"])
        if r.status >= 400:
            message = (r.body or {}).get("message", "") if isinstance(r.body, dict) else ""
            limited = r.status == 429 or (r.status == 403 and r.headers.get("x-ratelimit-remaining") == "0")
            if limited or "retry-after" in r.headers:
                reset = r.headers.get("x-ratelimit-reset")
                after = r.headers.get("retry-after")
                at = float(reset) if reset else (self.auth.clock() + float(after) if after else None)
                raise RateLimited(r.status, f"{method} {path}: {message}", at)
            raise GitHubError(r.status, f"{method} {path}: {message}")
        return r

    def git_env(self, repo: str) -> dict[str, str]:
        """Environment for `git fetch` of this repo as the installation: the token in an HTTP header set
        through git's environment config, so it is never in a URL, the mirror's config, or `ps`."""
        token = self.auth.token(self.installation(repo))
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            "GIT_TERMINAL_PROMPT": "0",
        }
