"""One reviewer run, inside the PR's pod:  python -m doppel_sandbox.review /job/<slot>

The runtime starts this with `docker exec --user <uid>`, one Unix user per reviewer run, and the
run's capability in `AGENT_CAPABILITY` (an environment variable another user can't read: review #5).
It reads `/job/<slot>/job.json` and `/job/<slot>/diff.patch` (read-only), runs one Codex process
whose model provider is the gateway, and prints the verdict as one JSON line on stdout.

Codex has no sign-in here, only the capability. It never reads the checkout's AGENTS.md or
.codex/config.toml (`project_doc_max_bytes = 0`; spike 0c). Codex's own sandbox (bwrap) is off: it
can't create namespaces in a pod with no capabilities, and the pod is the boundary instead, as in
steven-universe: read-only root and checkout, no capabilities, its own Unix user, a network that
reaches only the gateway, and no toolchains to run anything from the PR with. Codex can write only
to its own 0700 folder in /tmp.

Exit 0: the verdict passed the schema. Exit 2: no valid verdict after one correction (stderr says why).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from doppel_shared.verdict_schema import problems

CAPABILITY_ENV = "AGENT_CAPABILITY"

PROMPT = """You are the code reviewer `{reviewer}`, reviewing pull request #{pr} at commit {head}.
The repository is in your working directory, read-only. The PR's diff is at {diff}.
{instructions}{focus}
Review what this diff changes. Read whatever code you need to (rg, sed, cat, ls). Never run, build,
install or test anything from the repository. Report only real problems you can prove from code
you read: a claim, and the evidence that proves it.

Severity: P0 breaks production, data or security; P1 a real bug or contract break; P2 worth fixing;
P3 a nit. The verdict follows the findings: any P0/P1 -> "changes_requested"; only P2/P3 ->
"comments"; none -> "clean"; too large or impossible to review -> "stood_down".

Answer with ONLY one JSON object, no prose and no code fence:
{{"verdict": "clean|comments|changes_requested|stood_down", "summary": "one paragraph",
  "findings": [{{"id": "F1", "severity": "P0|P1|P2|P3",
                 "category": "correctness|security|contract|perf|tests|style|architecture",
                 "file": "path/in/repo", "line": 1, "introduced_by_diff": true,
                 "claim": "what is wrong, one sentence", "evidence": "the code you read that proves it",
                 "suggestion": "a concrete fix", "precondition": "what must hold for it to fire, or none",
                 "reachability": "always|conditional|rare", "fix_scope": "in_pr|follow_up"}}],
  "scope": {{"files_reviewed": 0, "files_skipped": []}},
  "fixed": ["the key of each earlier finding of yours, listed above, that this commit fixes"]}}
"""

RETRY = "That answer doesn't match the required shape: {problems}. Answer again with ONLY the JSON object."


@dataclass(frozen=True)
class Job:
    reviewer: str
    pr: int
    head_sha: str
    workdir: str  # /work/<head_sha>
    gateway_url: str  # http://gateway:8700
    model: str | None = None  # None: Codex's default; the runtime passes only one this Codex knows
    effort: str | None = None  # None: the model's default
    instructions: str = ""  # the reviewer's rubric, read from the PR's base (DSV-9)
    focus: str = ""  # a command's description

    @classmethod
    def load(cls, folder: Path) -> Job:
        return cls(**json.loads((folder / "job.json").read_text()))


def extract_json(text: str | None) -> Any:
    """The JSON object in a model's answer, tolerating a code fence or stray prose around it."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None


def stamp(verdict: Any, job: Job, engine: str) -> Any:
    """The runner's fields, never the model's: who reviewed what, with which engine."""
    if isinstance(verdict, dict):
        verdict.update(doppel=job.reviewer, pr=job.pr, head_sha=job.head_sha, engine=engine)
    return verdict


def codex_config(job: Job, home: Path, capability: str):
    from openai_codex import CodexConfig

    overrides = {
        "model_provider": '"gateway"',
        "model_providers.gateway.name": '"doppel gateway"',
        "model_providers.gateway.base_url": json.dumps(f"{job.gateway_url.rstrip('/')}/model"),
        "model_providers.gateway.env_key": json.dumps(CAPABILITY_ENV),
        "model_providers.gateway.wire_api": '"responses"',
        "model_providers.gateway.requires_openai_auth": "false",
        "project_doc_max_bytes": "0",  # never the checkout's AGENTS.md (spike 0c)
        "check_for_update_on_startup": "false",
        "analytics.enabled": "false",
        "feedback.enabled": "false",
        "web_search": '"disabled"',
    }
    env = {"CODEX_HOME": str(home), "HOME": str(home), CAPABILITY_ENV: capability, "PATH": os.environ.get("PATH", "")}
    return CodexConfig(config_overrides=tuple(f"{k}={v}" for k, v in overrides.items()), env=env)


async def review(job: Job, folder: Path, capability: str) -> dict:
    """The verdict, on the doppel's model and effort; when Codex refuses those (the payer's plan lacks the model,
    say), once more on Codex's defaults. An answer that never fits the schema isn't retried that way."""
    try:
        return await _review(job, folder, capability, job.model, job.effort)
    except NotAVerdict:
        raise
    except Exception as e:
        if not (job.model or job.effort):
            raise
        print(f"review: {job.model or 'the default model'} at {job.effort or 'its default effort'} failed "
              f"({type(e).__name__}): reviewing on Codex's defaults", file=sys.stderr)  # fmt: skip
        return await _review(job, folder, capability, None, None)


class NotAVerdict(ValueError):
    """The model answered, but never with a verdict that fits the schema."""


def codex_api():
    """The Codex SDK's parts a review uses, imported only in the pod (tests import them through here too)."""
    from openai_codex import ApprovalMode, AsyncCodex, Sandbox
    from openai_codex.types import ReasoningEffort

    return ApprovalMode, AsyncCodex, ReasoningEffort, Sandbox


async def _review(job: Job, folder: Path, capability: str, model: str | None, effort: str | None) -> dict:
    ApprovalMode, AsyncCodex, ReasoningEffort, Sandbox = codex_api()

    home = Path(tempfile.mkdtemp(prefix="codex-"))  # this run's scratch: mode 0700, wiped below
    try:
        focus = f"Look in particular at: {job.focus}\n" if job.focus else ""
        instructions = f"{job.instructions.strip()}\n" if job.instructions.strip() else ""
        prompt = PROMPT.format(reviewer=job.reviewer, pr=job.pr, head=job.head_sha,
                               diff=folder / "diff.patch", instructions=instructions, focus=focus)  # fmt: skip
        async with AsyncCodex(codex_config(job, home, capability)) as codex:
            thread = await codex.thread_start(
                model=model, cwd=job.workdir, ephemeral=True,
                sandbox=Sandbox.full_access, approval_mode=ApprovalMode.deny_all,  # the pod is the boundary
            )  # fmt: skip
            engine = model or "codex"
            level = ReasoningEffort(effort) if effort else None
            result = await thread.run(prompt, effort=level)
            verdict = stamp(extract_json(result.final_response), job, engine)
            wrong = problems(verdict) if verdict is not None else ["the answer held no JSON object"]
            if wrong:
                result = await thread.run(RETRY.format(problems="; ".join(wrong[:8])), effort=level)
                verdict = stamp(extract_json(result.final_response), job, engine)
                wrong = problems(verdict) if verdict is not None else ["the answer held no JSON object"]
            if wrong:
                raise NotAVerdict("no valid verdict after one correction: " + "; ".join(wrong[:8]))
            return verdict
    finally:
        shutil.rmtree(home, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m doppel_sandbox.review /job/<slot>", file=sys.stderr)
        return 64
    capability = os.environ.pop(CAPABILITY_ENV, "")
    if not capability:
        print(f"{CAPABILITY_ENV} is not set", file=sys.stderr)
        return 64
    folder = Path(argv[1])
    try:
        verdict = asyncio.run(review(Job.load(folder), folder, capability))
    except Exception as e:  # the runtime retries or fails the slot; it never sees a token
        print(f"review failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(verdict, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
