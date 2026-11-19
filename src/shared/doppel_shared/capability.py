"""A run's capability: a random secret the control plane mints and the gateway checks.

Only its hash is ever stored. Both sides must hash the same way, so it lives here.
"""

from __future__ import annotations

import hashlib
import secrets


def mint() -> str:
    return secrets.token_urlsafe(32)


def capability_hash(capability: str) -> str:
    return hashlib.sha256(capability.encode()).hexdigest()
