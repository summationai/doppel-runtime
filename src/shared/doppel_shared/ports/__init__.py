"""Interfaces to the outside world, as Protocols. Roles depend on these, never on an implementation.

    Sandbox     pods: one per PR, one Codex process per reviewer run
    Credentials the broker as the gateway sees it: a payer's model credential (model-provider)
    Runs        the running jobs as the gateway sees them (control-plane)

Imports only `doppel_shared.types`. Fakes for tests live in doppel_shared.testing.
"""

from doppel_shared.ports.credentials import Credential, Credentials, NotSignedIn
from doppel_shared.ports.runs import Run, Runs
from doppel_shared.ports.sandbox import ReviewJob, Sandbox

__all__ = ["Credential", "Credentials", "NotSignedIn", "ReviewJob", "Run", "Runs", "Sandbox"]
