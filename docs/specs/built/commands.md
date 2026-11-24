# Commands

> **Commands moved to Doppelwerk** (doppel-runtime BFF checkpoint 7, 2026-11-24): `server/intake/commands.ts` (checkpoint 6). The runtime's Python code and the tests named below are gone; this page is kept as the record of the decisions that were ported, now held by server/intake/commands.test.ts, server/reviews/publish.test.ts in agentcloud-apps/doppelwerk. See [bff-split.md](../bff-split.md).

`@doppelwerk <command> [reviewers…] [description]` in a PR comment. Each command is recorded once and acted on once, and always gets a visible answer: the reviews it asked for, or a reply saying why not. Code: [core/commands.py](../../../src/control-plane/doppel_control_plane/core/commands.py), `command` in [scheduler.py](../../../src/control-plane/doppel_control_plane/scheduler.py).

## Supports

- **`review`:**
  - `@doppelwerk review` runs every reviewer on the PR;
  - `@doppelwerk review steven repo-owner` runs the named ones;
  - words after the reviewer names are the description, given to the reviewer as what to look at, e.g. `@doppelwerk review steven the refund path`.
- **`help`:** replies with the syntax.
- **Doppelwerk's forms:** `@doppelwerk` alone and `@doppelwerk <reviewer>` mean `review`. `@doppel` works too.
- **State:** a command is `running` until every reviewer it named has answered, then `responded`. Refusals are `refused`.
- **It reaches the PR:** a command on a PR whose pod idled out brings the pod back.

## Locked decisions

| Decision | Value | Why | Since | Proven by |
| --- | --- | --- | --- | --- |
| Only insiders may command | Author association OWNER, MEMBER, COLLABORATOR or CONTRIBUTOR; never bots | A drive-by comment mustn't spend anyone's plan (Doppelwerk's rule) | 2026-11-19 | test_scheduler.py::test_outsiders_and_bots_cant_command |
| A personal doppel only where its owner was requested | `@doppelwerk review steven` is refused unless Steven was requested on the PR | It runs on Steven's plan: only for review he was asked for (DSV-4) | 2026-11-19 | test_scheduler.py::test_dsv_4_a_command_for_someone_not_requested_is_refused |
| The payer is the reviewer's owner, never the commenter | Troy's `@doppelwerk review steven` runs on Steven's sign-in | DSV-4 | 2026-11-19 | test_scheduler.py::test_dsv_4_troy_commanding_stevens_reviewer_runs_on_stevens_sign_in |
| Acted on once | Keyed by the comment's id; seen again (a poll, an edit) does nothing | GitHub returns comments more than once | 2026-11-19 | test_scheduler.py::test_a_command_is_acted_on_once; test_intake.py::test_a_command_is_acted_on_once_and_the_cursor_survives_a_restart |
| Every command gets a visible answer | Unknown command → refused with help; failed review → a reply after the last attempt | Doppelwerk's guarantee | 2026-11-19 | test_scheduler.py::test_an_unknown_command_is_refused_with_a_reply; test_scheduler.py::test_a_failing_command_review_retries_then_replies |
| Commands skip the settle window | Start at the next tick | Someone explicitly asked | 2026-11-19 | test_scheduler.py::test_a_review_command_records_and_queues_one_slot_that_skips_the_settle_window |
| A command follows the head | Pushed before it ran: it runs on the new commit | The asker still gets their answer | 2026-11-19 | test_scheduler.py::test_command_slots_follow_the_head |
| Description length | At most 500 characters | It goes into the prompt | 2026-11-19 | test_commands.py::test_description_is_capped |

## Not yet

- Commands beyond `review` and `help` (e.g. `stop`, `status`).
- On an outsider's PR, a bare `@doppelwerk review` finds no reviewer on the PR and is refused. Naming the reviewer works.
