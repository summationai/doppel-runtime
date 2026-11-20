from datetime import UTC, datetime, timedelta

from doppel_control_plane.runs import LedgerRuns
from doppel_control_plane.store import Ledger, iso
from doppel_shared.capability import capability_hash

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def test_the_gateway_sees_only_running_slots_by_capability(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    runs = LedgerRuns(ledger)
    pr = ledger.upsert_pr("summation", "summationai/sandbox", 42, head="aaa111")
    slot, _ = ledger.queue_slot(pr, "aaa111", "steven", "review_requested", payer_user="u-steven")
    ledger.execute(
        "UPDATE slots SET state='running', started_at=?, capability_hash=?, token_budget=500 WHERE id=?",
        (iso(NOW), capability_hash("cap"), slot),
    )
    run = runs.running(capability_hash("cap"))
    assert (run.slot_id, run.payer, run.started_at, run.tokens_used, run.token_budget) == (
        slot,
        "u-steven",
        NOW,
        0,
        500,
    )
    assert runs.running(capability_hash("other")) is None

    runs.add_tokens(slot, 120)
    assert runs.running(capability_hash("cap")).tokens_used == 120
    runs.record_usage_reset("u-steven", NOW + timedelta(hours=1))
    assert ledger.value("SELECT reset_at FROM usage_resets") == iso(NOW + timedelta(hours=1))

    runs.provider_error(slot, 401, "token_revoked", "invalid_request_error")
    row = ledger.one("SELECT provider_status, provider_code, provider_type FROM slots WHERE id=?", (slot,))
    assert tuple(row) == (401, "token_revoked", "invalid_request_error")

    ledger.execute("UPDATE slots SET state='posted' WHERE id=?", (slot,))
    assert runs.running(capability_hash("cap")) is None
    ledger.close()
