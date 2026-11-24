# Scheduler

> **Scheduling moved to Doppelwerk** (doppel-runtime BFF checkpoint 7, 2026-11-24): slots, settle, payer and host caps, leases and retries in `server/slots/` (checkpoints 3 and 4); this runner only runs the jobs it is given (`jobs.py`). The runtime's Python code and the tests named below are gone; this page is kept as the record of the decisions that were ported, now held by server/slots/slots.test.ts, server/slots/jobs.test.ts in agentcloud-apps/doppelwerk. See [bff-split.md](../bff-split.md).

Every reviewer on a commit is a **slot**. Events (a PR opened, a push, a request, a command, a verdict) queue or stop slots. Every few seconds a tick starts each queued slot whose conditions all hold, in the PR's one pod. There is no fixed order of steps, only conditions. Code: [scheduler.py](../../../src/control-plane/doppel_control_plane/scheduler.py), [core/matching.py](../../../src/control-plane/doppel_control_plane/core/matching.py), [core/payer.py](../../../src/control-plane/doppel_control_plane/core/payer.py).

## Supports

- **Events:** `pr_opened`, `review_requested`, `review_request_removed`, `synchronize` (a push), `closed`, `ci`, `command`, `verdict`, `slot_failed`, `disposition`, `usage_limit`, and `tick(now)`.
- **Slot states:** `queued`, `blocked`, `running`, `posted`, `failed`, `cancelled`. A queued slot records what it waits on (`settle`, `usage_window`, `payer_cap`, `host_cap`) and until when.
- **Slot sources:** app reviewers (on every insider PR), requested people's doppels, and commands.
- **Starting a slot:**
  - the PR gets its pod if it has none;
  - a capability is minted and only its hash is stored;
  - the reviewer starts in the pod with the PR, its base branch and a command's focus.
- **Posting:** a verdict is checked against the schema again, posted as one COMMENT review with inline comments, and its findings are recorded. The merger then runs.

## Locked decisions

| Decision | Value | Why | Since | Proven by |
| --- | --- | --- | --- | --- |
| Settle window | 90 s after a PR opens or each push before automatic reviews start; each push resets it. Commands skip it | Pushes come in bursts; Doppelwerk wasted 27 of 239 reviews on moved heads | 2026-11-19 | test_scheduler.py::test_reviews_wait_out_the_settle_window_then_start_in_one_pod; test_scheduler.py::test_a_second_push_resets_the_settle_window |
| Reviewers never wait for CI | No start condition is CI; only the merger waits | Reviews and CI run in parallel | 2026-11-19 | test_matching.py::test_no_condition_is_ci; test_scheduler.py::test_reviewers_start_without_waiting_for_ci |
| Payer cap | At most 2 running slots per payer | One person's plan can't take every slot, and a usage window drains slower | 2026-11-19 | test_scheduler.py::test_dsv_12_a_payers_third_slot_waits_for_the_cap |
| Host cap | At most 10 running slots on the host | Summini's capacity | 2026-11-19 | test_scheduler.py::test_host_cap |
| Usage window | A payer the gateway saw hit a usage limit waits until the reset; the PR is told once | Retrying into a usage limit wastes the window (DSV-12) | 2026-11-19 | test_scheduler.py::test_dsv_12_usage_reset_holds_until_the_reset_and_says_so_once |
| No sign-in: blocked | A slot whose payer has no working sign-in is `blocked`; the PR says "reconnect Codex" once; it starts when they connect | Never run on someone else's plan instead | 2026-11-19 | test_scheduler.py::test_no_sign_in_blocks_says_so_once_and_starts_once_connected |
| The payer never changes | A personal doppel always runs on its owner; an app reviewer on its configured payer; no fallback, even when the owner is capped or out of usage | DSV-4 | 2026-11-19 | test_payer.py; test_scheduler.py::test_dsv_4_payer_never_changes_when_the_owner_is_capped_or_out_of_usage |
| A push stops the old commit's reviewers | Running ones are stopped in the pod, queued ones cancelled; every reviewer on the PR is queued at the new head in the same pod; command slots follow the head | DSV-10; Doppelwerk let runs finish and threw the verdict away | 2026-11-19 | test_scheduler.py::test_dsv_10_a_push_stops_old_reviewers_and_queues_the_new_head_in_the_same_pod; test_scheduler.py::test_command_slots_follow_the_head |
| Never post on a moved head | A verdict for a commit that's no longer the PR's head is dropped | Doppelwerk's head-moved guard | 2026-11-19 | test_scheduler.py::test_a_verdict_for_a_stale_head_is_never_posted |
| Reviewers only comment | A reviewer's verdict always posts as COMMENT, never APPROVE or REQUEST_CHANGES | Only the merger approves (DSV-11) | 2026-11-19 | test_scheduler.py::test_reviewers_only_comment |
| Retries | A failed run is retried up to 3 attempts, then `failed`; a command's review says so on the PR | A flaky run shouldn't need a person; a command always gets an answer | 2026-11-19 | test_scheduler.py::test_a_failing_command_review_retries_then_replies |
| The verdict is checked before posting | The full verdict schema again (DSV-6); invalid counts as a failed attempt and nothing is posted | The pod is untrusted | 2026-11-19 | test_scheduler.py::test_dsv_6_an_invalid_verdict_is_never_posted_and_counts_as_a_failed_attempt |
| Only the capability's hash is stored | Cleared when the slot posts | The ledger never holds a secret | 2026-11-19 | test_scheduler.py::test_only_the_capability_hash_is_stored |
| Idle pods | A PR with no review for 3 days and nothing queued loses its pod (state `idle`); a command or new work brings it back | One pod per PR is cheap only if idle ones go | 2026-11-19 | test_scheduler.py::test_a_pr_idle_for_3_days_loses_its_pod_and_a_command_brings_it_back |
| Closing deletes the pod, keeps the history | Merged or closed: reviewers stopped, pod and checkouts deleted, ledger rows kept | DSV-14 | 2026-11-19 | test_scheduler.py::test_dsv_10_closing_deletes_the_pod_and_keeps_the_history |
| Outsiders' PRs | `pr_opened(auto=False)` records the PR without queuing the app's reviewers | See intake.md | 2026-11-20 (#13) | test_intake.py::test_an_outsiders_pr_runs_nothing_unasked |

## Not yet

- Fairness between tenants beyond the payer cap.
- A per-slot token budget set from rules. Every run gets the gateway's default (see credentials.md).
- Specialist reviewers routed by paths or labels (Doppelwerk's auto-route).
