"""Plain data shared by every layer: dataclasses and enums. No logic beyond parsing, no I/O.

Import from here (`from doppel_shared.types import PRRef`), not from the modules.
"""

from doppel_shared.types.command import ParsedCommand
from doppel_shared.types.merge import Decision, MergeInput, MergeState, OpenFinding, ReviewerStatus
from doppel_shared.types.pr import PRRef, PRState
from doppel_shared.types.reviewer import Reviewer, ReviewerKind
from doppel_shared.types.slot import SlotReason, SlotState, WaitReason
from doppel_shared.types.verdict import BLOCKING_SEVERITIES, Finding, InvalidVerdict, Verdict

__all__ = [
    "BLOCKING_SEVERITIES",
    "Decision",
    "Finding",
    "InvalidVerdict",
    "MergeInput",
    "MergeState",
    "OpenFinding",
    "PRRef",
    "PRState",
    "ParsedCommand",
    "Reviewer",
    "ReviewerKind",
    "ReviewerStatus",
    "SlotReason",
    "SlotState",
    "Verdict",
    "WaitReason",
]
