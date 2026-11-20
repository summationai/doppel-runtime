"""The ledger's schema, by version. A version, once a ledger exists anywhere, is never edited:
changes go in a new version. (Version 1 was edited during checkpoint 2, before any ledger existed.)
"""

V1 = """
-- One row per PR the runtime has seen.
CREATE TABLE prs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant            TEXT NOT NULL,
  repo              TEXT NOT NULL,               -- owner/name
  pr                INTEGER NOT NULL,
  current_head      TEXT,
  settle_until      TEXT,                        -- automatic reviews wait until then after a push
  pod_id            TEXT,                        -- null while the PR has no pod
  last_review_at    TEXT,                        -- drives the 3-day idle cleanup
  status_comment_id TEXT,                        -- the merger's status comment, edited in place
  status_body       TEXT,                        -- what it says now, so unchanged text isn't re-sent
  state             TEXT NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open', 'idle', 'merged', 'closed')),
  created_at        TEXT NOT NULL,
  UNIQUE (tenant, repo, pr)
);

-- The reviewers on a PR: the app's, and each personal reviewer whose owner was requested.
CREATE TABLE pr_reviewers (
  pr_id    INTEGER NOT NULL REFERENCES prs(id),
  reviewer TEXT NOT NULL,
  source   TEXT NOT NULL CHECK (source IN ('app', 'requested')),
  added_at TEXT NOT NULL,
  PRIMARY KEY (pr_id, reviewer)
);

-- One reviewer on one head: the unit of model work.
CREATE TABLE slots (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  pr_id           INTEGER NOT NULL REFERENCES prs(id),
  head_sha        TEXT NOT NULL,
  reviewer        TEXT NOT NULL,
  payer_user      TEXT,                          -- null: nobody can pay, so the slot is blocked
  reason          TEXT NOT NULL CHECK (reason IN ('review_requested', 'app', 'command')),
  command_id      TEXT NOT NULL DEFAULT '',      -- '' when not a command: NULLs would be distinct in UNIQUE
  state           TEXT NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued', 'blocked', 'running', 'posted', 'failed', 'skipped', 'cancelled')),
  waiting_on      TEXT CHECK (waiting_on IN ('settle', 'payer_cap', 'usage_window', 'host_cap')),
  not_before      TEXT,                          -- when the wait ends, if known
  attempts        INTEGER NOT NULL DEFAULT 0,
  capability_hash TEXT,                          -- only the hash; the capability itself is never stored
  verdict         TEXT,                          -- the verdict JSON
  queued_at       TEXT NOT NULL,
  started_at      TEXT,
  finished_at     TEXT,
  UNIQUE (pr_id, head_sha, reviewer, command_id)
);
CREATE INDEX slots_by_state ON slots (state, queued_at);

-- The merger's decision per (PR, head).
CREATE TABLE merges (
  pr_id      INTEGER NOT NULL REFERENCES prs(id),
  head_sha   TEXT NOT NULL,
  decision   TEXT NOT NULL
             CHECK (decision IN ('waiting_reviews', 'waiting_ci', 'blocked', 'approved', 'dismissed')),
  reason     TEXT,
  review_id  TEXT,                               -- the merger's last formal review on this head
  decided_at TEXT NOT NULL,
  PRIMARY KEY (pr_id, head_sha)
);

-- CI's overall result per head, as intake reports it.
CREATE TABLE ci_status (
  pr_id      INTEGER NOT NULL REFERENCES prs(id),
  head_sha   TEXT NOT NULL,
  state      TEXT NOT NULL CHECK (state IN ('pending', 'success', 'failure')),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (pr_id, head_sha)
);

-- A payer's usage window: their slots wait until it resets (the gateway records it, checkpoint 4).
CREATE TABLE usage_resets (
  payer    TEXT PRIMARY KEY,
  reset_at TEXT NOT NULL
);

-- @doppelwerk commands: the comment id is the idempotency key.
CREATE TABLE commands (
  tenant       TEXT NOT NULL,
  comment_id   TEXT NOT NULL,
  pr_id        INTEGER NOT NULL REFERENCES prs(id),
  author       TEXT,
  body         TEXT,
  command      TEXT,                             -- review | help | whatever was asked (refused)
  targets      TEXT,                             -- comma-separated reviewer handles
  description  TEXT,
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

-- Findings, their anchors on GitHub, and the posted review, per slot.
CREATE TABLE findings (
  slot_id  INTEGER NOT NULL REFERENCES slots(id),
  fid      TEXT NOT NULL,
  severity TEXT,
  category TEXT,
  file     TEXT,
  line     INTEGER,
  state    TEXT NOT NULL DEFAULT 'open',         -- open|hit|weak_hit|noise|unknown|excluded (after merge)
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
-- A person's response to a finding (resolved thread, dismissive reply): closes it for the merger.
CREATE TABLE dispositions (
  pr_id       INTEGER NOT NULL REFERENCES prs(id),
  reviewer    TEXT NOT NULL,
  fid         TEXT NOT NULL,
  disposition TEXT NOT NULL,
  by_login    TEXT,
  at          TEXT NOT NULL,
  PRIMARY KEY (pr_id, reviewer, fid)
);
"""

# Checkpoint 4: the gateway counts each run's tokens against its budget.
V2 = """
ALTER TABLE slots ADD COLUMN tokens_used INTEGER NOT NULL DEFAULT 0;
ALTER TABLE slots ADD COLUMN token_budget INTEGER;  -- null: the gateway's default
"""

# Review #3: the provider's own answer, so a refused key is never mistaken for a usage limit.
V3 = """
ALTER TABLE slots ADD COLUMN provider_status INTEGER;
ALTER TABLE slots ADD COLUMN provider_code TEXT;
ALTER TABLE slots ADD COLUMN provider_type TEXT;
"""

# Checkpoint 6: intake's place in each repo's comment listing, so a restart doesn't re-read from the start;
# and whether a requested person has reviewed themselves (GitHub then drops them from the requested list,
# but their doppel stays on the PR: it posts as doppelwerk, not as them).
V4 = """
ALTER TABLE pr_reviewers ADD COLUMN person_reviewed INTEGER NOT NULL DEFAULT 0;
CREATE TABLE intake_cursors (
  tenant         TEXT NOT NULL,
  repo           TEXT NOT NULL,
  comments_since TEXT NOT NULL,
  PRIMARY KEY (tenant, repo)
);
"""

MIGRATIONS = {1: V1, 2: V2, 3: V3, 4: V4}
SCHEMA_VERSION = max(MIGRATIONS)
