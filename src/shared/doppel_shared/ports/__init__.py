"""Interfaces to the outside world, as Protocols. Roles depend on these, never on an implementation.

    Directory   the BFF's records: reviewers per repo, who pays, who has a working sign-in
    Sandbox     pods: one per PR, one Codex process per reviewer run       (real one: checkpoint 5)
    GitHub      reviews, replies, the merger's status comment               (real one: checkpoint 6)
    Credentials the broker as the gateway sees it: a payer's model credential (model-provider)
    Runs        the control plane's running slots as the gateway sees them (control-plane)

Imports only `doppel_shared.types`. Fakes for tests live in tests/fakes.py.
"""

from doppel_shared.ports.credentials import Credential, Credentials, NotSignedIn
from doppel_shared.ports.directory import Directory
from doppel_shared.ports.github import GitHub, InlineComment, PostedReview
from doppel_shared.ports.runs import Run, Runs
from doppel_shared.ports.sandbox import ReviewJob, Sandbox

__all__ = [
    "Credential",
    "Credentials",
    "Directory",
    "GitHub",
    "InlineComment",
    "NotSignedIn",
    "PostedReview",
    "ReviewJob",
    "Run",
    "Runs",
    "Sandbox",
]
