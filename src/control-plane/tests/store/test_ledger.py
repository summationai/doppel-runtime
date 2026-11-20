import sqlite3

import pytest
from doppel_control_plane.store import SCHEMA_VERSION, Ledger


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    yield led
    led.close()


def test_schema_is_migrated_and_uses_wal(ledger):
    assert ledger.schema_version == SCHEMA_VERSION
    assert ledger.journal_mode == "wal"
    tables = {r[0] for r in ledger._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "prs",
        "slots",
        "commands",
        "command_responses",
        "findings",
        "finding_anchors",
        "posts",
        "dispositions",
        "pr_reviewers",
        "merges",
        "ci_status",
        "usage_resets",
    } <= tables


def test_reopening_does_not_migrate_twice(tmp_path):
    Ledger(tmp_path / "l.db").close()
    led = Ledger(tmp_path / "l.db")
    assert led.schema_version == SCHEMA_VERSION
    led.close()


def test_pr_is_one_row_per_tenant_repo_pr_and_keeps_its_head(ledger):
    a = ledger.upsert_pr("summation", "summationai/sandbox", 7, head="aaa")
    b = ledger.upsert_pr("summation", "summationai/sandbox", 7)
    other_tenant = ledger.upsert_pr("acme", "summationai/sandbox", 7)
    assert a == b != other_tenant
    head = ledger._db.execute("SELECT current_head FROM prs WHERE id=?", (a,)).fetchone()[0]
    assert head == "aaa"


def test_queuing_a_slot_twice_is_harmless(ledger):
    pr = ledger.upsert_pr("summation", "summationai/sandbox", 7, head="aaa")
    first, created = ledger.queue_slot(pr, "aaa", "steven", "review_requested")
    again, created_again = ledger.queue_slot(pr, "aaa", "steven", "review_requested")
    assert created and not created_again and first == again
    assert ledger._db.execute("SELECT count(*) FROM slots").fetchone()[0] == 1


def test_non_command_slots_dedupe_even_without_a_command_id(ledger):
    """The bug in the first schema draft: a NULL command_id would make every insert distinct."""
    pr = ledger.upsert_pr("summation", "summationai/sandbox", 7, head="aaa")
    ledger.queue_slot(pr, "aaa", "repo-owner", "app", command_id=None)
    ledger.queue_slot(pr, "aaa", "repo-owner", "app", command_id="")
    assert ledger._db.execute("SELECT count(*) FROM slots").fetchone()[0] == 1


def test_each_command_gets_its_own_slot(ledger):
    pr = ledger.upsert_pr("summation", "summationai/sandbox", 7, head="aaa")
    ledger.queue_slot(pr, "aaa", "steven", "review_requested")
    ledger.queue_slot(pr, "aaa", "steven", "command", command_id="c1")
    ledger.queue_slot(pr, "aaa", "steven", "command", command_id="c2")
    assert ledger._db.execute("SELECT count(*) FROM slots").fetchone()[0] == 3


def test_a_new_head_is_a_new_slot(ledger):
    pr = ledger.upsert_pr("summation", "summationai/sandbox", 7, head="aaa")
    ledger.queue_slot(pr, "aaa", "steven", "review_requested")
    ledger.queue_slot(pr, "bbb", "steven", "review_requested")
    assert ledger._db.execute("SELECT count(*) FROM slots").fetchone()[0] == 2


def test_running_slots_go_back_to_queued(ledger):
    pr = ledger.upsert_pr("summation", "summationai/sandbox", 7, head="aaa")
    running, _ = ledger.queue_slot(pr, "aaa", "steven", "review_requested")
    waiting, _ = ledger.queue_slot(pr, "aaa", "troy", "review_requested")
    ledger.mark_running(running)
    assert ledger.slot(running).state == "running"
    assert ledger.requeue_running() == 1
    assert ledger.slot(running).state == "queued"
    assert ledger.slot(waiting).state == "queued"


def test_states_outside_the_schema_are_refused(ledger):
    pr = ledger.upsert_pr("summation", "summationai/sandbox", 7, head="aaa")
    slot, _ = ledger.queue_slot(pr, "aaa", "steven", "review_requested")
    with pytest.raises(sqlite3.IntegrityError):
        ledger._db.execute("UPDATE slots SET state='bogus' WHERE id=?", (slot,))


def test_a_slot_needs_its_pr(ledger):
    with pytest.raises(sqlite3.IntegrityError):
        ledger.queue_slot(999, "aaa", "steven", "review_requested")


def test_a_version_1_ledger_migrates_to_the_current_version(tmp_path):
    from doppel_control_plane.store.schema import V1

    db = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    db.executescript(f"BEGIN; {V1} PRAGMA user_version=1; COMMIT;")
    db.close()
    led = Ledger(tmp_path / "old.db")
    assert led.schema_version == SCHEMA_VERSION == 3
    cols = {r[1] for r in led._db.execute("PRAGMA table_info(slots)")}
    assert {"tokens_used", "token_budget", "provider_status", "provider_code", "provider_type"} <= cols
    led.close()
