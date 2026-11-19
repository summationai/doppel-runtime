"""The ledger: one SQLite file (WAL) on the runtime's own disk, keyed by tenant.

`prs` and `slots` are the runtime's own (spec section 5). `commands`, `command_responses`,
`findings`, `finding_anchors` and `posts` are Doppelwerk's tables, re-keyed by tenant and PR.

The runtime is the only writer. WAL is safe here because the file sits on the Linux VM's own disk;
Doppelwerk's WAL trouble came from summini's virtiofs mount.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
-- One row per PR the runtime has seen. The PR's pod, if it has one, and its idle clock live here.
CREATE TABLE prs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant         TEXT NOT NULL,
  repo           TEXT NOT NULL,               -- owner/name
  pr             INTEGER NOT NULL,
  current_head   TEXT,
  pod_id         TEXT,                        -- null while the PR has no pod
  last_review_at TEXT,                        -- drives the 3-day idle cleanup
  state          TEXT NOT NULL DEFAULT 'open'
                 CHECK (state IN ('open', 'idle', 'merged', 'closed')),
  created_at     TEXT NOT NULL,
  UNIQUE (tenant, repo, pr)
);

-- One reviewer on one head. The scheduler starts a slot once all its conditions hold.
CREATE TABLE slots (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id           INTEGER NOT NULL REFERENCES prs(id),
  head_sha        TEXT NOT NULL,
  reviewer        TEXT NOT NULL,
  payer_user      TEXT,                       -- null until a payer is chosen; no payer -> blocked
  reason          TEXT NOT NULL CHECK (reason IN ('review_requested', 'app', 'command')),
  command_id      TEXT NOT NULL DEFAULT '',   -- '' when not a command: NULLs would be distinct in UNIQUE
  state           TEXT NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued', 'blocked', 'running', 'posted', 'failed', 'skipped', 'cancelled')),
  waiting_on      TEXT CHECK (waiting_on IN ('settle', 'ci', 'payer_cap', 'usage_window')),
  not_before      TEXT,                       -- end of the settle window, the CI deadline or the usage reset
  attempts        INTEGER NOT NULL DEFAULT 0,
  capability_hash TEXT,                       -- only the hash; the capability itself is never stored
  verdict         TEXT,                       -- the verdict JSON once done
  review_id       TEXT,                       -- the GitHub review it posted
  queued_at       TEXT NOT NULL,
  started_at      TEXT,
  finished_at     TEXT,
  UNIQUE (pr_id, head_sha, reviewer, command_id)
);
CREATE INDEX slots_by_state ON slots (state, not_before);

-- Doppelwerk's explicit @doppelwerk commands: the comment id is the idempotency key.
CREATE TABLE commands (
  tenant       TEXT NOT NULL,
  comment_id   TEXT NOT NULL,
  pr_id        INTEGER NOT NULL REFERENCES prs(id),
  author       TEXT,
  body         TEXT,
  targets      TEXT,
  created_at   TEXT,
  received_at  TEXT NOT NULL,
  state        TEXT NOT NULL DEFAULT 'received'
               CHECK (state IN ('received', 'running', 'responded', 'refused')),
  response     TEXT,
  responded_at TEXT,
  PRIMARY KEY (tenant, comment_id)
);
CREATE TABLE command_responses (
  tenant     TEXT NOT NULL,
  comment_id TEXT NOT NULL,
  reviewer   TEXT NOT NULL,
  response   TEXT,
  at         TEXT,
  PRIMARY KEY (tenant, comment_id, reviewer)
);

-- Findings, their anchors and the posted review, per slot (Doppelwerk keys these by job).
CREATE TABLE findings (
  slot_id  INTEGER NOT NULL REFERENCES slots(id),
  fid      TEXT NOT NULL,
  severity TEXT,
  category TEXT,
  file     TEXT,
  line     INTEGER,
  state    TEXT NOT NULL DEFAULT 'open',      -- open|hit|weak_hit|noise|unknown|excluded
  PRIMARY KEY (slot_id, fid)
);
CREATE TABLE finding_anchors (
  slot_id      INTEGER NOT NULL REFERENCES slots(id),
  fid          TEXT NOT NULL,
  comment_id   TEXT,
  file         TEXT,
  line         INTEGER,
  hunk         TEXT,
  line_context TEXT,
  PRIMARY KEY (slot_id, fid)
);
CREATE TABLE posts (
  slot_id    INTEGER PRIMARY KEY REFERENCES slots(id),
  review_id  TEXT,
  review_url TEXT,
  posted_at  TEXT
);
-- A person's disposition of a finding (a resolved thread, a reply): drop it next round.
CREATE TABLE dispositions (
  pr_id       INTEGER NOT NULL REFERENCES prs(id),
  reviewer    TEXT NOT NULL,
  fid         TEXT NOT NULL,
  disposition TEXT NOT NULL,                  -- resolved|replied|...
  by_login    TEXT,
  at          TEXT NOT NULL,
  PRIMARY KEY (pr_id, reviewer, fid)
);
"""

MIGRATIONS = {1: _SCHEMA_V1}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

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

    # ---- PRs and slots ---------------------------------------------------------------------------

    def upsert_pr(self, tenant: str, repo: str, pr: int, head: str | None = None) -> int:
        with self.tx() as db:
            db.execute(
                "INSERT INTO prs (tenant, repo, pr, current_head, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tenant, repo, pr) "
                "DO UPDATE SET current_head=coalesce(excluded.current_head, current_head)",
                (tenant, repo, pr, head, now()),
            )
            return db.execute("SELECT id FROM prs WHERE tenant=? AND repo=? AND pr=?", (tenant, repo, pr)).fetchone()[0]

    def queue_slot(
        self, pr_id: int, head_sha: str, reviewer: str, reason: str, command_id: str = "", payer_user: str | None = None
    ) -> tuple[int, bool]:
        """Queue a reviewer on a head. Queuing the same one twice is harmless: (id, False)."""
        with self.tx() as db:
            cur = db.execute(
                "INSERT INTO slots (pr_id, head_sha, reviewer, reason, command_id, payer_user, queued_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(pr_id, head_sha, reviewer, command_id) DO NOTHING",
                (pr_id, head_sha, reviewer, reason, command_id or "", payer_user, now()),
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
