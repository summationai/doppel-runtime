"""`@doppelwerk <command> [reviewers...] [description]`.

After the command come zero or more reviewer handles; the first word that isn't one starts the
description. Doppelwerk's forms still work: `@doppelwerk` alone and `@doppelwerk <handle>` mean
`review`. Who may command (org insiders) is checked by the caller, which knows the author.
"""

from __future__ import annotations

import re

from doppel_runtime.types import ParsedCommand
from doppel_runtime.types.command import KNOWN_COMMANDS

MAX_DESCRIPTION = 500
ALL_TARGETS = "all"

_TRIGGER = re.compile(r"@doppel(?:werk)?\b(.*)", re.IGNORECASE | re.DOTALL)

HELP = (
    "Commands: `@doppelwerk review [reviewers...] [what to look at]` reviews the current head. "
    "With no reviewers named, every reviewer on this PR runs. `@doppelwerk help` shows this."
)


def parse(body: str, reviewers: set[str]) -> ParsedCommand | None:
    """A comment body -> the command it carries, or None when it doesn't mention @doppelwerk."""
    m = _TRIGGER.search(body or "")
    if not m:
        return None
    words = m.group(1).split()
    known = {r.lower() for r in reviewers}

    if not words:
        return ParsedCommand("review", (), "")
    first = words[0].lower()
    if first in KNOWN_COMMANDS:
        command, words = first, words[1:]
    elif first in known or first == ALL_TARGETS:
        command = "review"  # Doppelwerk's form: a reviewer first means review
    else:
        return ParsedCommand(first, (), _description(words[1:]))

    targets: list[str] = []
    while words:
        word = words[0].lower().rstrip(",")
        if word == ALL_TARGETS:
            targets, words = [], words[1:]  # explicit "all" = every reviewer on the PR
            break
        if word not in known:
            break
        if word not in targets:
            targets.append(word)
        words = words[1:]
    return ParsedCommand(command, tuple(targets), _description(words))


def _description(words: list[str]) -> str:
    return " ".join(words)[:MAX_DESCRIPTION]
