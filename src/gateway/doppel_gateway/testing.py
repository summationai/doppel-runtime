"""A stand-in for GitHub's REST API, for tests: the endpoints the App uses, answering as GitHub does.

It checks what GitHub checks: the App's JWT (signed with the key it handed out, issuer the App id,
not expired), and that each request's installation token is live and covers the repo. Inline
comments on lines outside the PR's diff are refused with 422, as GitHub does. Every request is
recorded so a test can count calls and see what was sent.

Runs a real HTTP server on 127.0.0.1 in a thread, so the client's real code path is exercised.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_KEYS: list = []  # generating an RSA key takes a moment: one per test run unless asked for another


def _key(fresh: bool):
    if fresh or not _KEYS:
        _KEYS.append(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    return _KEYS[-1] if fresh else _KEYS[0]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Call:
    method: str
    path: str
    token: str
    body: dict | None


@dataclass
class FakeGitHubAPI:
    app_id: str = "4242"
    installations: dict[str, int] = field(default_factory=dict)  # repo -> installation id
    token_lifetime: float = 3600
    prs: dict[str, dict[int, dict]] = field(default_factory=dict)  # repo -> number -> pull
    comments: dict[str, list[dict]] = field(default_factory=dict)  # repo -> issue comments
    reviews: dict[tuple[str, int], list[dict]] = field(default_factory=dict)
    review_comments: dict[int, list[dict]] = field(default_factory=dict)  # review id -> its comments
    checks: dict[str, list[dict]] = field(default_factory=dict)  # sha -> check runs
    diff_lines: dict[tuple[str, int], set[tuple[str, int]]] = field(default_factory=dict)  # lines a comment may use
    calls: list[Call] = field(default_factory=list)
    tokens_issued: int = 0
    revoke_next: bool = False  # the next repo request's token is refused once (401), as after a revocation
    rate_remaining: int = 5000
    fresh_key: bool = False  # a key pair of its own, unlike every other fake's

    def __post_init__(self):
        key = _key(self.fresh_key)
        self.private_key_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        self._public = key.public_key()
        self._tokens: dict[str, tuple[int, float]] = {}
        self._ids = itertools.count(5000)
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # ---- lifecycle ----

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> FakeGitHubAPI:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _do(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                status, body, headers = api._handle(self.command, self.path, dict(self.headers), raw)
                out = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                for k, v in {"Content-Type": "application/json", **headers}.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = do_PUT = do_PATCH = _do  # noqa: N815

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    # ---- driving it from a test ----

    def open(self, repo: str, number: int, head: str, *, author: str = "troyb", association: str = "MEMBER",
             requested=(), base: str = "main", draft: bool = False) -> None:  # fmt: skip
        self.prs.setdefault(repo, {})[number] = {
            "number": number, "state": "open", "merged_at": None, "draft": draft,
            "head": {"sha": head}, "base": {"ref": base}, "user": {"login": author},
            "author_association": association,
            "requested_reviewers": [{"login": login} for login in requested],
        }  # fmt: skip

    def push(self, repo: str, number: int, head: str) -> None:
        self.prs[repo][number]["head"]["sha"] = head

    def merge(self, repo: str, number: int) -> None:
        self.prs[repo][number].update(state="closed", merged_at=_iso(time.time()))

    def comment(self, repo: str, number: int, body: str, *, author: str = "stevenliu", association: str = "MEMBER",
                on_issue: bool = False) -> str:  # fmt: skip
        cid = next(self._ids)
        kind = "issues" if on_issue else "pull"
        now = _iso(time.time())
        self.comments.setdefault(repo, []).append({
            "id": cid, "body": body, "user": {"login": author}, "author_association": association,
            "html_url": f"https://github.com/{repo}/{kind}/{number}#issuecomment-{cid}",
            "issue_url": f"https://api.github.com/repos/{repo}/issues/{number}",
            "created_at": now, "updated_at": now,
        })  # fmt: skip
        return str(cid)

    def check(self, sha: str, status: str = "completed", conclusion: str | None = "success") -> None:
        self.checks.setdefault(sha, []).append({"status": status, "conclusion": conclusion})

    def calls_to(self, method: str, pattern: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and re.search(pattern, c.path)]

    # ---- the API ----

    def _handle(self, method: str, raw_path: str, headers: dict, raw: bytes):
        with self._lock:
            url = urlparse(raw_path)
            path, query = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
            body = json.loads(raw) if raw else None
            auth = headers.get("Authorization", "")
            token = auth.removeprefix("Bearer ")
            self.calls.append(Call(method, path, token, body))

            m = re.fullmatch(r"/app/installations/(\d+)/access_tokens", path)
            if m and method == "POST":
                return self._mint(int(m.group(1)), token)
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)", path)
            if not m:
                return 404, {"message": "Not Found"}, {}
            repo, rest = m.groups()
            if self.revoke_next:
                self.revoke_next = False
                self._tokens.pop(token, None)
            held = self._tokens.get(token)
            if held is None or held[1] < time.time():
                return 401, {"message": "Bad credentials"}, {}
            if self.installations.get(repo) != held[0]:
                return 404, {"message": "Not Found"}, {}
            self.rate_remaining -= 1
            reset = str(int(time.time()) + 3600)
            limit = {"x-ratelimit-remaining": str(self.rate_remaining), "x-ratelimit-reset": reset}
            if self.rate_remaining < 0:
                return 403, {"message": "API rate limit exceeded"}, {**limit, "x-ratelimit-remaining": "0"}
            status, out = self._repo(method, repo, rest, query, body)
            return status, out, limit

    def _mint(self, installation: int, token: str):
        try:
            claims = jwt.decode(token, self._public, algorithms=["RS256"])
        except jwt.PyJWTError as e:
            return 401, {"message": f"A JSON web token could not be decoded: {e}"}, {}
        if claims.get("iss") != self.app_id or claims["exp"] - claims["iat"] > 600:
            return 401, {"message": "'Issuer' claim or expiry is wrong"}, {}
        if installation not in self.installations.values():
            return 404, {"message": "Not Found"}, {}
        self.tokens_issued += 1
        issued = f"ghs_fake{self.tokens_issued:04d}installationtoken"
        expires = time.time() + self.token_lifetime
        self._tokens[issued] = (installation, expires)
        return 201, {"token": issued, "expires_at": _iso(expires)}, {}

    @staticmethod
    def _page(items: list, query: dict) -> list:
        per, page = int(query.get("per_page", 30)), int(query.get("page", 1))
        return items[(page - 1) * per : page * per]

    def _repo(self, method: str, repo: str, rest: str, q: dict, body: dict | None):
        prs = self.prs.get(repo, {})
        if method == "GET" and rest == "/pulls":
            return 200, self._page([p for _, p in sorted(prs.items()) if p["state"] == "open"], q)
        if m := re.fullmatch(r"/pulls/(\d+)", rest):
            pr = prs.get(int(m.group(1)))
            return (200, pr) if pr else (404, {"message": "Not Found"})
        if method == "GET" and rest == "/issues/comments":
            since = q.get("since", "")
            found = sorted((c for c in self.comments.get(repo, []) if c["updated_at"] >= since),
                           key=lambda c: c["updated_at"])  # fmt: skip
            return 200, self._page(found, q)
        if m := re.fullmatch(r"/pulls/(\d+)/reviews", rest):
            number = int(m.group(1))
            if method == "GET":
                return 200, self._page(self.reviews.get((repo, number), []), q)
            return self._post_review(repo, number, body)
        if m := re.fullmatch(r"/pulls/(\d+)/reviews/(\d+)/comments", rest):
            return 200, self.review_comments.get(int(m.group(2)), [])
        if (m := re.fullmatch(r"/pulls/(\d+)/reviews/(\d+)/dismissals", rest)) and method == "PUT":
            for r in self.reviews.get((repo, int(m.group(1))), []):
                if r["id"] == int(m.group(2)):
                    if r["state"] != "APPROVED":
                        return 422, {"message": "Can not dismiss a commented pull request review"}
                    r.update(state="DISMISSED", dismissal=body["message"])
                    return 200, r
            return 404, {"message": "Not Found"}
        if m := re.fullmatch(r"/commits/([0-9a-f]+)/check-runs", rest):
            runs = self.checks.get(m.group(1), [])
            return 200, {"total_count": len(runs), "check_runs": runs}
        if (m := re.fullmatch(r"/issues/(\d+)/comments", rest)) and method == "POST":
            cid = self.comment(repo, int(m.group(1)), body["body"], author="doppelwerk-dev[bot]", association="NONE")
            return 201, {"id": int(cid), "body": body["body"]}
        if (m := re.fullmatch(r"/issues/comments/(\d+)", rest)) and method == "PATCH":
            for c in self.comments.get(repo, []):
                if c["id"] == int(m.group(1)):
                    c.update(body=body["body"], updated_at=_iso(time.time()))
                    return 200, c
            return 404, {"message": "Not Found"}
        return 404, {"message": f"Not Found: {method} {rest}"}

    def _post_review(self, repo: str, number: int, body: dict):
        allowed = self.diff_lines.get((repo, number))
        for c in body.get("comments", []):
            if allowed is not None and (c["path"], c["line"]) not in allowed:
                return 422, {"message": "Unprocessable Entity", "errors": ["Line could not be resolved"]}
        rid = next(self._ids)
        state = {"APPROVE": "APPROVED", "COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[body["event"]]
        review = {"id": rid, "state": state, "body": body["body"], "commit_id": body["commit_id"],
                  "user": {"login": "doppelwerk-dev[bot]"},
                  "html_url": f"https://github.com/{repo}/pull/{number}#pullrequestreview-{rid}"}  # fmt: skip
        self.reviews.setdefault((repo, number), []).append(review)
        self.review_comments[rid] = [{"id": next(self._ids), "path": c["path"], "line": c["line"], "body": c["body"]}
                                     for c in body.get("comments", [])]  # fmt: skip
        return 200, review
