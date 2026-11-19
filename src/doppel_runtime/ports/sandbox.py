from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from doppel_runtime.types import PRRef


@dataclass(frozen=True)
class ReviewJob:
    slot_id: int
    reviewer: str
    head_sha: str
    capability: str  # a secret: handed to the pod, never logged or stored (only its hash is)
    focus: str = ""  # a command's description, given to the reviewer as what to look at

    def __repr__(self) -> str:  # keep the capability out of logs and tracebacks
        return f"ReviewJob(slot_id={self.slot_id}, reviewer={self.reviewer!r}, head_sha={self.head_sha!r})"


class Sandbox(Protocol):
    def create_pod(self, pr: PRRef) -> str:
        """Start the PR's pod; returns its id."""
        ...

    def delete_pod(self, pod_id: str) -> None: ...

    def start_reviewer(self, pod_id: str, job: ReviewJob) -> None:
        """Start one Codex process in the pod for this reviewer run."""
        ...

    def stop_reviewer(self, pod_id: str, slot_id: int) -> None:
        """Interrupt that reviewer's turn and close its process. The pod stays."""
        ...
