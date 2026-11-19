"""A reviewer's verdict: Doppelwerk's `schema/verdict.schema.json`, the one file a reviewer writes.

Full JSON-schema validation comes with the harness (checkpoint 5). `Verdict.parse` checks what the
scheduler and merger rely on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VERDICTS = ("clean", "comments", "changes_requested", "stood_down")
SEVERITIES = ("P0", "P1", "P2", "P3")
BLOCKING_SEVERITIES = frozenset({"P0", "P1"})


class InvalidVerdict(ValueError):
    pass


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    category: str
    file: str
    line: int
    claim: str
    introduced_by_diff: bool = True  # False: the problem predates the PR, so it is advisory only

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES and self.introduced_by_diff


@dataclass(frozen=True)
class Verdict:
    reviewer: str
    head_sha: str
    verdict: str
    summary: str
    findings: tuple[Finding, ...]
    raw: dict[str, Any]

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Verdict:
        try:
            verdict = raw["verdict"]
            if verdict not in VERDICTS:
                raise InvalidVerdict(f"verdict {verdict!r} not one of {VERDICTS}")
            findings = []
            for f in raw.get("findings", []):
                if f["severity"] not in SEVERITIES:
                    raise InvalidVerdict(f"severity {f['severity']!r} not one of {SEVERITIES}")
                findings.append(
                    Finding(
                        id=str(f["id"]),
                        severity=f["severity"],
                        category=f.get("category", ""),
                        file=f["file"],
                        line=int(f["line"]),
                        claim=f.get("claim", ""),
                        introduced_by_diff=bool(f.get("introduced_by_diff", True)),
                    )
                )
            return cls(
                reviewer=raw["doppel"],
                head_sha=raw["head_sha"],
                verdict=verdict,
                summary=raw.get("summary", ""),
                findings=tuple(findings),
                raw=raw,
            )
        except (KeyError, TypeError, ValueError) as e:
            if isinstance(e, InvalidVerdict):
                raise
            raise InvalidVerdict(f"malformed verdict: {e!r}") from e
