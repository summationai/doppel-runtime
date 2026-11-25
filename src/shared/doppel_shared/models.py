"""Which model, and how hard it reasons, for one doppel's review.

A doppel's `doppel.yaml` may name a `model` and an `effort`. The runner honors them only when the Codex it ships
with knows them: anything else (a model this Codex doesn't have, an effort the model doesn't take, a typo) runs on
Codex's default instead, and says so. Nothing here ever stops a review.

CODEX_MODELS is the model catalog built into the pinned Codex (openai-codex 0.167.2, the sandbox image's), as its
own picker lists them, in its order; tests/test_models.py checks it against the installed binary. Bump both together.
"""

from __future__ import annotations

CODEX_VERSION = "0.167.2"

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

_ULTRA = ("low", "medium", "high", "xhigh", "max", "ultra")
_MAX = ("low", "medium", "high", "xhigh", "max")
CODEX_MODELS: dict[str, tuple[str, ...]] = {
    "gpt-6.1-sol": _ULTRA,
    "gpt-6-astra": _ULTRA,
    "gpt-6-sol": _ULTRA,
    "gpt-6-luna": _MAX,
    "gpt-5.6-sol": _ULTRA,
    "gpt-6.1-terra": _ULTRA,
    "gpt-5.6-terra": _ULTRA,
    "gpt-5.6-luna": _MAX,
    "gpt-5.5": ("low", "medium", "high", "xhigh"),
}

# With no model named, Codex picks one per account: an effort is passed only when every listed model takes it.
DEFAULT_EFFORTS = tuple(e for e in EFFORTS if all(e in efforts for efforts in CODEX_MODELS.values()))


def choose(model: object, effort: object) -> tuple[str | None, str | None, list[str]]:
    """The model and effort to run with (None: Codex's default), and a note for each setting that was dropped."""
    notes: list[str] = []
    name = model.strip() if isinstance(model, str) else ""
    level = effort.strip().lower() if isinstance(effort, str) else ""
    chosen: str | None = None
    if name:
        if name in CODEX_MODELS:
            chosen = name
        else:
            notes.append(f"model {name!r} isn't one this runner's Codex ({CODEX_VERSION}) has: used its default")
    allowed = CODEX_MODELS[chosen] if chosen else DEFAULT_EFFORTS
    level_chosen: str | None = None
    if level:
        if level in allowed:
            level_chosen = level
        else:
            notes.append(f"effort {level!r} isn't one {chosen or 'the default model'} takes: used its default")
    return chosen, level_chosen, notes
