"""Checkpoint 6b's first live check, read-only: the App's credentials work on the configured repos.

    uv run python scripts/live_github_check.py --config doppel.toml

Needs `github.mode = "app"` in the config, with the App's id, its private key file (chmod 600) and each
tenant's installation_id. Writes nothing to GitHub. Prints no token.

1. The private key signs a JWT GitHub accepts: an installation token per tenant.
2. The poller's reads work on every configured repo: open PRs, PR comments, and CI for one head.
3. A mirror fetches each repo with the token (the token never stored in the mirror).
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from doppel_control_plane.config import load, read_secret
from doppel_control_plane.mirrors import Mirrors
from doppel_gateway.github import AppAuth, GitHubClient, RestReader


def step(n: int, text: str, ok: bool) -> bool:
    print(f"{n}. {'PASS' if ok else 'FAIL'}  {text}", flush=True)
    return ok


async def main(config: Path) -> int:
    cfg = load(config)
    gh = cfg.github
    if gh.mode != "app":
        print("the config needs github.mode = app", file=sys.stderr)
        return 64
    auth = AppAuth(gh.app_id, read_secret(gh.private_key), gh.api_url)
    installations = {repo: t.installation_id for t in cfg.tenants for repo in t.repos}
    client = GitHubClient(auth, installations)
    results = []

    for t in cfg.tenants:
        try:
            auth.token(t.installation_id)
            results.append(step(1, f"tenant {t.id}: a token for installation {t.installation_id}", True))
        except Exception as e:
            results.append(step(1, f"tenant {t.id}: installation {t.installation_id}: {e}", False))

    reader = RestReader(client)
    for repo in installations:
        try:
            prs = await reader.open_prs(repo)
            comments = await reader.comments(repo, datetime.now(UTC) - timedelta(days=1))
            ci = await reader.ci_state(repo, prs[0].head_sha) if prs else "no open PR"
            left = client.rate_remaining.get(installations[repo])
            results.append(step(2, f"{repo}: {len(prs)} open PR(s), {len(comments)} PR comment(s) today, "
                                   f"CI of the first: {ci}; {left} calls left this hour", True))  # fmt: skip
        except Exception as e:
            results.append(step(2, f"{repo}: {e}", False))

    with tempfile.TemporaryDirectory() as tmp:
        mirrors = Mirrors(Path(tmp), git_env=client.git_env)
        for repo in installations:
            prs = await reader.open_prs(repo)
            if not prs:
                print(f"3. SKIP  {repo}: no open PR to fetch")
                continue
            try:
                mirrors.sync(repo, cfg.github.repo_url.format(repo=repo), prs[0].number,
                             f"refs/pull/{prs[0].number}/head")  # fmt: skip
                stored = subprocess.run(["git", "config", "--list", "--local"], cwd=mirrors.path(repo),
                                        capture_output=True, text=True).stdout  # fmt: skip
                results.append(step(3, f"{repo}: fetched #{prs[0].number}; nothing of the token in the mirror",
                                    "extraheader" not in stored))  # fmt: skip
            except Exception as e:
                results.append(step(3, f"{repo}: {e}", False))

    print("ALL PASS" if all(results) else "SOME FAILED", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    sys.exit(asyncio.run(main(p.parse_args().config)))
