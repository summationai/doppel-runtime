"""In-memory stand-ins for the ports. They record every call so tests can assert on what was done."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from doppel_shared.ports import ReviewJob, Run
from doppel_shared.types import PRRef


@dataclass
class FakeSandbox:
    pods: dict[str, PRRef] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    started: list[tuple[str, ReviewJob]] = field(default_factory=list)
    stopped: list[tuple[str, int]] = field(default_factory=list)
    _n: int = 0

    def create_pod(self, pr: PRRef) -> str:
        self._n += 1
        pod_id = f"pod-{self._n}"
        self.pods[pod_id] = pr
        return pod_id

    def delete_pod(self, pod_id: str) -> None:
        self.pods.pop(pod_id, None)
        self.deleted.append(pod_id)

    def start_reviewer(self, pod_id: str, job: ReviewJob) -> None:
        assert pod_id in self.pods, f"start in a pod that doesn't exist: {pod_id}"
        self.started.append((pod_id, job))

    def stop_reviewer(self, pod_id: str, slot_id: int) -> None:
        self.stopped.append((pod_id, slot_id))

    def job(self, slot_id: int) -> ReviewJob:
        return next(job for _, job in self.started if job.slot_id == slot_id)


@dataclass
class FakeRuns:
    """The `Runs` port in memory: running slots by capability hash, tokens and usage resets recorded."""

    by_hash: dict[str, Run] = field(default_factory=dict)
    tokens: dict[int, int] = field(default_factory=dict)
    resets: dict[str, datetime] = field(default_factory=dict)
    errors: dict[int, tuple] = field(default_factory=dict)

    def running(self, capability_hash: str):
        run = self.by_hash.get(capability_hash)
        if run is None:
            return None
        return replace(run, tokens_used=run.tokens_used + self.tokens.get(run.slot_id, 0))

    def add_tokens(self, slot_id: int, tokens: int) -> None:
        self.tokens[slot_id] = self.tokens.get(slot_id, 0) + tokens

    def record_usage_reset(self, payer: str, reset_at: datetime) -> None:
        self.resets[payer] = reset_at

    def provider_error(self, slot_id: int, status: int, code: str | None, kind: str | None) -> None:
        self.errors[slot_id] = (status, code, kind)
