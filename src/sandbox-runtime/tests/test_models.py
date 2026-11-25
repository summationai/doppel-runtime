"""A doppel's model and effort: honored when this runner's Codex knows them, its defaults otherwise."""

import re
from pathlib import Path

import pytest
from doppel_shared.models import CODEX_MODELS, CODEX_VERSION, DEFAULT_EFFORTS, choose


def test_a_known_model_and_an_effort_it_takes_are_used():
    assert choose("gpt-6-sol", "xhigh") == ("gpt-6-sol", "xhigh", [])
    assert choose(" gpt-5.5 ", "HIGH") == ("gpt-5.5", "high", [])


def test_nothing_named_is_codex_default_quietly():
    assert choose(None, None) == (None, None, [])
    assert choose("", "") == (None, None, [])
    assert choose(42, ["high"]) == (None, None, [])


def test_an_unknown_model_runs_on_the_default_and_says_so():
    model, effort, notes = choose("gpt-4o", "high")
    assert (model, effort) == (None, "high"), "the effort still holds when every model takes it"
    assert len(notes) == 1 and "gpt-4o" in notes[0] and CODEX_VERSION in notes[0]


def test_an_effort_the_model_doesnt_take_runs_on_its_default():
    assert choose("gpt-5.5", "ultra")[:2] == ("gpt-5.5", None)
    assert choose("gpt-6-luna", "ultra")[:2] == ("gpt-6-luna", None)
    assert choose(None, "ultra")[:2] == (None, None), "the default model may not take it"
    assert choose(None, "loud")[2] != []
    assert DEFAULT_EFFORTS == ("low", "medium", "high", "xhigh")


def _binary() -> Path | None:
    try:
        import codex_cli_bin
    except ImportError:
        return None
    found = next(Path(codex_cli_bin.__file__).parent.glob("bin/codex"), None)
    return found if found and found.is_file() else None


@pytest.mark.skipif(_binary() is None, reason="the Codex binary isn't installed here")
def test_the_catalog_is_the_pinned_codexs_own():
    from importlib.metadata import version

    assert version("openai-codex") == CODEX_VERSION, "bump CODEX_VERSION and CODEX_MODELS with the Codex pin"
    data = _binary().read_bytes().decode("utf-8", "replace")
    for slug, efforts in CODEX_MODELS.items():
        at = data.find(f'"slug": "{slug}"')
        assert at >= 0, f"{slug} isn't in Codex {CODEX_VERSION}'s catalog"
        entry = data[at:].split('"slug"', 2)[1]
        assert re.search(r'"visibility"\s*:\s*"list"', entry), f"{slug} isn't one Codex lists"
        listed = tuple(dict.fromkeys(re.findall(r'"effort"\s*:\s*"([a-z]+)"', entry)))
        assert listed == efforts, f"{slug} takes {listed} in Codex {CODEX_VERSION}"


def test_every_effort_a_runner_passes_is_one_the_sdk_takes():
    from doppel_sandbox.review import codex_api

    _, _, reasoning_effort, _ = codex_api()  # the same imports a review makes in the pod
    for efforts in CODEX_MODELS.values():
        for effort in efforts:
            assert reasoning_effort(effort).value == effort
