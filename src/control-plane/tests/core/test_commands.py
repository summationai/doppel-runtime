import pytest
from doppel_control_plane.core.commands import MAX_DESCRIPTION, parse

REVIEWERS = {"steven", "troy", "repo-owner"}


@pytest.mark.parametrize(
    ("body", "command", "targets", "description"),
    [
        ("@doppelwerk review", "review", (), ""),
        ("@doppelwerk review steven look at the retry cap", "review", ("steven",), "look at the retry cap"),
        ("@doppelwerk review steven troy is the backoff right?", "review", ("steven", "troy"), "is the backoff right?"),
        ("@doppelwerk review steven, troy check it", "review", ("steven", "troy"), "check it"),
        ("@doppelwerk steven", "review", ("steven",), ""),  # Doppelwerk's form
        ("@doppelwerk", "review", (), ""),
        ("@doppel review all please", "review", (), "please"),
        ("@DoppelWerk REVIEW Steven", "review", ("steven",), ""),
        ("lgtm, but @doppelwerk review troy the retry", "review", ("troy",), "the retry"),
        ("@doppelwerk help", "help", (), ""),
        ("@doppelwerk deploy it", "deploy", (), "it"),
        ("@doppelwerk review look at steven's change", "review", (), "look at steven's change"),
    ],
)
def test_parse(body, command, targets, description):
    parsed = parse(body, REVIEWERS)
    assert (parsed.command, parsed.targets, parsed.description) == (command, targets, description)


def test_known_commands():
    assert parse("@doppelwerk review", REVIEWERS).known
    assert parse("@doppelwerk help", REVIEWERS).known
    assert not parse("@doppelwerk deploy", REVIEWERS).known


@pytest.mark.parametrize("body", ["", "lgtm", "ping @doppelganger", "email doppelwerk@x.com"])
def test_not_a_command(body):
    assert parse(body, REVIEWERS) is None


def test_description_is_capped():
    assert len(parse("@doppelwerk review " + "x" * 2000, REVIEWERS).description) == MAX_DESCRIPTION
