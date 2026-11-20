"""Doppelwerk's verdict schema (`verdict.schema.json`, copied from summationai/doppelwerk at 36aff3f).

The harness in the pod validates a reviewer's answer against it before printing it; the control
plane validates again before posting (DSV-6). Keep the copy identical to Doppelwerk's: the GitHub
contract must not drift.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator


@cache
def schema() -> dict[str, Any]:
    return json.loads(files("doppel_shared").joinpath("verdict.schema.json").read_text())


def problems(verdict: Any) -> list[str]:
    """Every way `verdict` breaks the schema, as readable lines; empty when it's valid."""
    validator = Draft202012Validator(schema())
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '(top)'}: {e.message}"
        for e in sorted(validator.iter_errors(verdict), key=lambda e: list(e.absolute_path))
    ]
