"""Interfaces to the outside world, as Protocols. Roles depend on these, never on an implementation.

    Directory   the BFF's records: reviewers per repo, who pays, who has a working sign-in
    Sandbox     pods: one per PR, one Codex process per reviewer run       (real one: checkpoint 5)
    GitHub      reviews, replies, the merger's status comment               (real one: checkpoint 6)

Imports only `doppel_runtime.types`. Fakes for tests live in tests/fakes.py.
"""

from doppel_runtime.ports.directory import Directory
from doppel_runtime.ports.github import GitHub, InlineComment, PostedReview
from doppel_runtime.ports.sandbox import ReviewJob, Sandbox

__all__ = ["Directory", "GitHub", "InlineComment", "PostedReview", "ReviewJob", "Sandbox"]
