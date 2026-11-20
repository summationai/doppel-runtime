"""One bare mirror per repo; a checkout and a diff per head (spec section 6, "Getting the PR in").

The runtime fetches; the pod never does (it has no GitHub credential and no route to GitHub).
A head's checkout is `git archive` of that commit, so `.git` never enters the pod.

Reviewers on one PR start together, each preparing in its own thread: everything that touches a
mirror holds that repo's lock, or two clones (or two fetches into the same ref) collide.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{7,40}$")


class MirrorError(Exception):
    pass


def _git(*args: str, cwd: Path | None = None) -> str:
    run = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=600)
    if run.returncode != 0:
        raise MirrorError(f"git {args[0]} failed: {run.stderr.strip()[-500:]}")
    return run.stdout


class Mirrors:
    def __init__(self, root: Path, *, partial: bool = True):
        self.root = root
        self.partial = partial  # --filter=blob:none, as Doppelwerk does (off for servers that refuse filters)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, repo: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(repo, threading.Lock())

    def path(self, repo: str) -> Path:
        owner, _, name = repo.partition("/")
        if not owner or not name or "/" in name or ".." in repo:
            raise MirrorError(f"not a repo name: {repo!r}")
        return self.root / f"{owner}__{name}.git"

    def sync(self, repo: str, url: str, pr_number: int, pr_ref: str) -> None:
        """Clone if needed; fetch every branch and the PR's head (`pr_ref`) into refs/doppel/pr/<n>."""
        mirror = self.path(repo)
        with self._lock(repo):
            self._sync(mirror, url, pr_number, pr_ref)

    def _sync(self, mirror: Path, url: str, pr_number: int, pr_ref: str) -> None:
        if not mirror.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            _git("clone", "--bare", *(["--filter=blob:none"] if self.partial else []), url, str(mirror))
        _git("fetch", "--prune", "--force", url, "+refs/heads/*:refs/heads/*",
             f"+{pr_ref}:refs/doppel/pr/{pr_number}", cwd=mirror)  # fmt: skip

    def default_branch(self, repo: str) -> str:
        return _git("symbolic-ref", "--short", "HEAD", cwd=self.path(repo)).strip()

    def checkout(self, repo: str, sha: str, dest: Path) -> Path:
        """Write the commit's files to `dest` (once): no .git, nothing else of the repo."""
        if not SHA.match(sha):
            raise MirrorError(f"not a commit: {sha!r}")
        with self._lock(repo):
            return self._checkout(repo, sha, dest)

    def _checkout(self, repo: str, sha: str, dest: Path) -> Path:
        if dest.is_dir():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=".checkout-"))
        try:
            archive = subprocess.run(["git", "archive", sha], cwd=self.path(repo), capture_output=True, timeout=600)
            if archive.returncode != 0:
                raise MirrorError(f"git archive failed: {archive.stderr.decode(errors='replace').strip()[-500:]}")
            subprocess.run(["tar", "-x", "-C", str(tmp)], input=archive.stdout, check=True, timeout=600)
            tmp.chmod(0o755)
            tmp.rename(dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return dest

    def diff(self, repo: str, sha: str, base: str | None = None) -> str:
        """What the PR changes: from where it left its base branch (GitHub's `base.ref`) to this head."""
        base = base or self.default_branch(repo)
        if base.startswith("-") or ".." in base:
            raise MirrorError(f"not a branch: {base!r}")
        return _git("diff", f"refs/heads/{base}...{sha}", cwd=self.path(repo))
