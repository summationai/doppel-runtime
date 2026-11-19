"""Plain data shared by every layer: dataclasses and enums. No logic beyond parsing, no I/O.

Import from here (`from doppel_runtime.types import PRRef`), not from the modules.
"""

from doppel_runtime.types.command import ParsedCommand
from doppel_runtime.types.merge import Decision, MergeInput, MergeState, OpenFinding, ReviewerStatus
from doppel_runtime.types.pr import PRRef, PRState
from doppel_runtime.types.reviewer import Reviewer, ReviewerKind
from doppel_runtime.types.slot import SlotReason, SlotState, WaitReason
from doppel_runtime.types.verdict import BLOCKING_SEVERITIES, Finding, InvalidVerdict, Verdict

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
