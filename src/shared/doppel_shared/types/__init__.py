"""Plain data shared by every layer: dataclasses and enums. No logic beyond parsing, no I/O.

Import from here (`from doppel_shared.types import PRRef`), not from the modules.
"""

from doppel_shared.types.pr import PRRef, PRState

__all__ = ["PRRef", "PRState"]
