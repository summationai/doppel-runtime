from __future__ import annotations

from dataclasses import dataclass

KNOWN_COMMANDS = ("review", "help")


@dataclass(frozen=True)
class ParsedCommand:
    """`@doppelwerk <command> [reviewers...] [description]`, parsed."""

    command: str  # review | help | anything else (refused)
    targets: tuple[str, ...]  # empty: every reviewer on the PR
    description: str

    @property
    def known(self) -> bool:
        return self.command in KNOWN_COMMANDS
