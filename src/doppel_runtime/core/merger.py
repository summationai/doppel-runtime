"""The PR's one approval decision (spec section 5, "The merger"; DSV-11).

Reviewers only comment. For the PR's current head, in order:

1. Every required reviewer has a verdict on this head?        else waiting_reviews
2. A required reviewer stood down?                             blocked: a person has to review
3. CI finished and green?                                      else waiting_ci, or blocked when red
4. Any open P0/P1 from any reviewer (personal ones included)?   blocked
5. Otherwise                                                    approved

A PR with no required reviewer is never approved: nobody the app trusts has looked at it.
"""

from __future__ import annotations

from doppel_runtime.types import Decision, MergeInput, MergeState

_MARK = {"clean": "✓", "comments": "✓", "changes_requested": "✗", "stood_down": "stood down"}


def decide(inp: MergeInput) -> Decision:
    required = [r for r in inp.reviewers if r.required]
    if not required:
        return Decision(MergeState.WAITING_REVIEWS, "no app reviewer on this PR, so nothing can approve it")
    missing = [r.reviewer for r in required if r.verdict is None]
    if missing:
        return Decision(MergeState.WAITING_REVIEWS, "waiting on " + ", ".join(missing))
    stood = [r.reviewer for r in required if r.verdict == "stood_down"]
    if stood:
        return Decision(MergeState.BLOCKED, ", ".join(stood) + " stood down: a person has to review this PR")
    if inp.ci in (None, "pending"):
        return Decision(MergeState.WAITING_CI, "waiting on CI")
    if inp.ci != "success":
        return Decision(MergeState.BLOCKED, "CI failed")
    if inp.blocking:
        return Decision(MergeState.BLOCKED, _findings_line(inp))
    return Decision(MergeState.APPROVED, "every required reviewer is done, CI is green, no P0 or P1 is open")


def _findings_line(inp: MergeInput) -> str:
    by: dict[str, list[str]] = {}
    for f in inp.blocking:
        by.setdefault(f.reviewer, []).append(f.severity)
    parts = [f"{len(sevs)} open {'/'.join(sorted(set(sevs)))} from {who}" for who, sevs in sorted(by.items())]
    return "; ".join(parts)


def status_text(inp: MergeInput, decision: Decision) -> str:
    """The one-line state of the PR, kept current in the merger's status comment."""
    reviewers = [
        f"{r.reviewer} {_MARK.get(r.verdict or '', r.status)}" for r in sorted(inp.reviewers, key=lambda r: r.reviewer)
    ]
    ci = {None: "CI not started", "pending": "CI pending", "success": "CI green"}.get(inp.ci, "CI failed")
    parts = [*reviewers, ci]
    if inp.blocking:
        parts.append(_findings_line(inp))
    return f"**{decision.state.value.replace('_', ' ')}** · " + " · ".join(parts)


def review_body(inp: MergeInput, decision: Decision) -> str:
    """The body of the merger's formal review: APPROVE when approved, a COMMENT when blocked."""
    lines = [f"Doppelwerk merger: **{decision.state.value.replace('_', ' ')}**. {decision.reason}."]
    for f in inp.blocking:
        lines.append(f"- {f.severity} from {f.reviewer}: {f.claim} (`{f.file}:{f.line}`)")
    return "\n".join(lines)
