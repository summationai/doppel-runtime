from datetime import UTC, datetime, timedelta

import pytest

from doppel_runtime.core.matching import Blocked, Limits, SlotFacts, Start, Wait, check
from doppel_runtime.types import WaitReason

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)


def facts(**kw):
    base = dict(
        payer="u-steven",
        payer_signed_in=True,
        is_command=False,
        settle_until=None,
        usage_reset=None,
        payer_running=0,
        host_running=0,
    )
    return SlotFacts(**{**base, **kw})


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({}, Start()),
        ({"payer": None}, Blocked()),
        ({"payer_signed_in": False}, Blocked()),
        ({"settle_until": LATER}, Wait(WaitReason.SETTLE, LATER)),
        ({"settle_until": LATER, "is_command": True}, Start()),  # an explicit ask skips the settle window
        ({"settle_until": NOW - timedelta(seconds=1)}, Start()),
        ({"usage_reset": LATER}, Wait(WaitReason.USAGE_WINDOW, LATER)),
        ({"usage_reset": LATER, "is_command": True}, Wait(WaitReason.USAGE_WINDOW, LATER)),  # commands keep it
        ({"payer_running": 2}, Wait(WaitReason.PAYER_CAP)),
        ({"payer_running": 2, "is_command": True}, Wait(WaitReason.PAYER_CAP)),
        ({"payer_running": 1}, Start()),
        ({"host_running": 10}, Wait(WaitReason.HOST_CAP)),
        ({"payer_signed_in": False, "settle_until": LATER}, Blocked()),  # blocked is said first
    ],
)
def test_check(kw, expected):
    assert check(facts(**kw), NOW, Limits()) == expected


def test_no_condition_is_ci():
    assert "ci" not in {r.value for r in WaitReason}
