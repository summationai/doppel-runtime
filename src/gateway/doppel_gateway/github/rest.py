"""The `GitHubReader` and `GitHub` ports on GitHub's REST API, as the App's installation.

The gateway is the only GitHub writer (spec section 2). Reads run in a thread so a slow GitHub never
holds up the event loop; posts are synchronous, as the scheduler makes them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Literal

from doppel_shared.ports import PAGE, CIState, InlineComment, OpenPR, PostedReview, PRComment
from doppel_shared.types import PRRef

from doppel_gateway.github.client import GitHubClient, GitHubError

log = logging.getLogger(__name__)

# Check-run conclusions that fail CI. neutral and skipped pass, as GitHub's own merge box treats them.
FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}


def _time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class RestReader:
    def __init__(self, client: GitHubClient):
        self.client = client

    async def _get(self, repo: str, path: str, **params):
        r = await asyncio.to_thread(self.client.request, "GET", repo, path, params=params or None)
        return r.body

    async def open_prs(self, repo: str, page: int = 1) -> list[OpenPR]:
        body = await self._get(repo, "/pulls", state="open", per_page=PAGE, page=page)
        return [
            OpenPR(
                number=p["number"],
                head_sha=p["head"]["sha"],
                base_ref=p["base"]["ref"],
                author=p["user"]["login"],
                requested=frozenset(u["login"].lower() for u in p.get("requested_reviewers") or []),
                draft=bool(p.get("draft")),
                author_association=p.get("author_association", "NONE"),
            )
            for p in body
        ]

    async def pr_state(self, repo: str, number: int) -> Literal["open", "merged", "closed"]:
        p = await self._get(repo, f"/pulls/{number}")
        if p["state"] == "open":
            return "open"
        return "merged" if p.get("merged_at") else "closed"

    async def comments(self, repo: str, since: datetime, page: int = 1) -> list[PRComment]:
        body = await self._get(repo, "/issues/comments", since=since.isoformat().replace("+00:00", "Z"),
                               sort="updated", direction="asc", per_page=PAGE, page=page)  # fmt: skip
        found = []
        for c in body:
            if "/pull/" not in c.get("html_url", ""):  # a comment on an issue, not a PR
                continue
            number = int(c["issue_url"].rsplit("/", 1)[1])
            found.append(PRComment(str(c["id"]), number, c["user"]["login"], c.get("author_association", "NONE"),
                                   c.get("body") or "", _time(c["updated_at"])))  # fmt: skip
        return found

    async def review_authors(self, repo: str, number: int, page: int = 1) -> list[str]:
        body = await self._get(repo, f"/pulls/{number}/reviews", per_page=PAGE, page=page)
        return [r["user"]["login"] for r in body if r.get("user")]

    async def ci_state(self, repo: str, sha: str) -> CIState | None:
        """The head's check runs, read as one state. Over 100 check runs, only the first 100 count (POC)."""
        body = await self._get(repo, f"/commits/{sha}/check-runs", per_page=PAGE)
        runs = body.get("check_runs", [])
        if not runs:
            return None
        if any(r["status"] != "completed" for r in runs):
            return "pending"
        return "failure" if any(r.get("conclusion") in FAILED for r in runs) else "success"


class RestGitHub:
    """Posting: reviews, dismissals, replies and the merger's status comment."""

    def __init__(self, client: GitHubClient):
        self.client = client

    def post_review(self, pr: PRRef, head_sha: str, event: Literal["COMMENT", "APPROVE"], body: str,
                    comments: list[InlineComment] | None = None) -> PostedReview:  # fmt: skip
        comments = comments or []
        inline = [{"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body} for c in comments]
        payload = {"commit_id": head_sha, "event": event, "body": body, "comments": inline}
        try:
            r = self.client.request("POST", pr.repo, f"/pulls/{pr.number}/reviews", body=payload)
        except GitHubError as e:
            if e.status != 422 or not comments:
                raise
            # A line outside the diff can't carry a comment. Post them in the body instead of losing them.
            log.warning("github: %s: inline comments refused (%s); posting them in the review body", pr, e)
            lines = "\n".join(f"- `{c.path}:{c.line}`: {c.body}" for c in comments)
            payload = {"commit_id": head_sha, "event": event, "body": f"{body}\n\n{lines}", "comments": []}
            r = self.client.request("POST", pr.repo, f"/pulls/{pr.number}/reviews", body=payload)
            comments = []
        review_id = r.body["id"]
        ids: dict[str, str] = {}
        if comments:
            posted = self.client.request(
                "GET", pr.repo, f"/pulls/{pr.number}/reviews/{review_id}/comments", params={"per_page": PAGE}
            ).body
            for c in comments:
                match = next((p for p in posted if p["path"] == c.path and p["body"] == c.body
                              and p.get("line", p.get("original_line")) == c.line), None)  # fmt: skip
                if match is not None:
                    ids[c.fid] = str(match["id"])
        return PostedReview(str(review_id), r.body.get("html_url", ""), ids)

    def dismiss_review(self, pr: PRRef, review_id: str, message: str) -> None:
        self.client.request("PUT", pr.repo, f"/pulls/{pr.number}/reviews/{review_id}/dismissals",
                            body={"message": message, "event": "DISMISS"})  # fmt: skip

    def reply(self, pr: PRRef, body: str) -> str:
        r = self.client.request("POST", pr.repo, f"/issues/{pr.number}/comments", body={"body": body})
        return str(r.body["id"])

    def upsert_status(self, pr: PRRef, comment_id: str | None, body: str) -> str:
        if comment_id:
            try:
                self.client.request("PATCH", pr.repo, f"/issues/comments/{comment_id}", body={"body": body})
                return comment_id
            except GitHubError as e:
                if e.status != 404:
                    raise
                log.info("github: %s: the status comment was deleted; posting a new one", pr)
        return self.reply(pr, body)
