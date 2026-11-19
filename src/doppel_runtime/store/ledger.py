"""The ledger: one SQLite file (WAL) on the runtime's own disk, keyed by tenant.

The runtime is the only writer. WAL is safe because the file sits on the Linux VM's own disk;
Doppelwerk's WAL trouble came from summini's virtiofs mount. Times are stored as ISO 8601 in UTC
with seconds (`iso`), so they sort and compare as strings.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from doppel_runtime.store.schema import MIGRATIONS


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def now() -> str:
    return iso(datetime.now(UTC))


@dataclass(frozen=True)
class Slot:
    id: int
    pr_id: int
    head_sha: str
    reviewer: str
    reason: str
    command_id: str
    state: str


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        current = self._db.execute("PRAGMA user_version").fetchone()[0]
        for version in sorted(v for v in MIGRATIONS if v > current):
            # One script, one transaction: the tables and the version land together or not at all.
            self._db.executescript(f"BEGIN IMMEDIATE; {MIGRATIONS[version]} PRAGMA user_version={version}; COMMIT;")

    @property
    def schema_version(self) -> int:
        return self._db.execute("PRAGMA user_version").fetchone()[0]

    @property
    def journal_mode(self) -> str:
        return self._db.execute("PRAGMA journal_mode").fetchone()[0]

    # ---- plain access, for the roles ---------------------------------------------------------------

    @contextmanager
    def tx(self) -> Generator[sqlite3.Connection]:
        """A write transaction. Nested use joins the outer one."""
        if self._db.in_transaction:
            yield self._db
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def rows(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self._db.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self._db.execute(sql, params).fetchone()

    def value(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self._db.execute(sql, params).fetchone()
        return row[0] if row else None

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.tx() as db:
            return db.execute(sql, params)

    # ---- PRs and slots -----------------------------------------------------------------------------

    def upsert_pr(self, tenant: str, repo: str, pr: int, head: str | None = None, at: str | None = None) -> int:
        with self.tx() as db:
            db.execute(
                "INSERT INTO prs (tenant, repo, pr, current_head, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tenant, repo, pr) "
                "DO UPDATE SET current_head=coalesce(excluded.current_head, current_head)",
                (tenant, repo, pr, head, at or now()),
            )
            return db.execute("SELECT id FROM prs WHERE tenant=? AND repo=? AND pr=?", (tenant, repo, pr)).fetchone()[0]

    def queue_slot(
        self,
        pr_id: int,
        head_sha: str,
        reviewer: str,
        reason: str,
        command_id: str | None = "",
        payer_user: str | None = None,
        at: str | None = None,
    ) -> tuple[int, bool]:
        """Queue a reviewer on a head. Queuing the same one twice is harmless: (id, False)."""
        with self.tx() as db:
            cur = db.execute(
                "INSERT INTO slots (pr_id, head_sha, reviewer, reason, command_id, payer_user, queued_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(pr_id, head_sha, reviewer, command_id) DO NOTHING",
                (pr_id, head_sha, reviewer, reason, command_id or "", payer_user, at or now()),
            )
            created = cur.rowcount == 1
            row = db.execute(
                "SELECT id FROM slots WHERE pr_id=? AND head_sha=? AND reviewer=? AND command_id=?",
                (pr_id, head_sha, reviewer, command_id or ""),
            ).fetchone()
            return row[0], created

    def slot(self, slot_id: int) -> Slot:
        r = self._db.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        return Slot(r["id"], r["pr_id"], r["head_sha"], r["reviewer"], r["reason"], r["command_id"], r["state"])

    def mark_running(self, slot_id: int) -> None:
        with self.tx() as db:
            db.execute(
                "UPDATE slots SET state='running', started_at=?, attempts=attempts+1 WHERE id=? AND state='queued'",
                (now(), slot_id),
            )

    def requeue_running(self) -> int:
        """At startup: slots left `running` by a crash go back to `queued`. Returns how many."""
        with self.tx() as db:
            return db.execute(
                "UPDATE slots SET state='queued', started_at=NULL, capability_hash=NULL WHERE state='running'"
            ).rowcount
