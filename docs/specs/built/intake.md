# Intake

Turns GitHub's state into scheduler events. It reads repo listings, compares them with the ledger, and calls the scheduler for what changed. A timer polls every repo; a webhook polls one repo at once. Both use the same code. Code: [intake.py](../../../src/control-plane/doppel_control_plane/intake.py), [webhooks.py](../../../src/control-plane/doppel_control_plane/webhooks.py).

## Supports

- **Three passes per cycle, in priority order across a tenant's repos:**
  1. **PRs:** the open-PR listing gives new PRs, new heads (commits), requested reviewers and the base branch. A PR that left the listing is looked up once, to tell merged from closed.
  2. **Commands:** the repo's PR comments since the last cycle.
  3. **CI:** check runs, only for heads the merger is waiting on CI for.
- **Diffed against the ledger:** a missed cycle is caught up by the next one, and reading the same state twice changes nothing.
- **Paging:** listings are read 100 at a time.
- **The comment cursor** survives restarts. The first poll of a repo reads comments only 1 hour back.
- **Webhooks:**
  - each event makes the runtime poll its repo at once;
  - a burst becomes one poll;
  - events that arrive during a poll trigger one more poll afterwards;
  - repos poll independently, and one repo never has two polls at once.

## Locked decisions

| Decision | Value | Why | Since | Proven by |
| --- | --- | --- | --- | --- |
| Repo listings, not per-person searches | One open-PR listing and one comment listing per repo per cycle | GitHub's search limit is about 30 a minute (RC's review #8) | 2026-11-20 (#12) | test_intake.py::test_listings_are_paged |
| A call budget per installation | 2,000 calls per sliding hour per tenant (`[intake] calls_per_hour`) | Leave room under GitHub's 5,000 for posting | 2026-11-20 (#12) | test_intake.py::test_budget_is_a_sliding_hour_per_installation |
| A spent budget drops CI first, then commands, never PRs | Passes run in priority order; the warning is logged once per spell | New PRs and pushes matter most | 2026-11-20 (#12) | test_intake.py::test_a_spent_budget_drops_ci_then_commands_never_prs |
| Poll interval | 60 s (`[intake] poll_seconds`) | Doppelwerk's cadence | 2026-11-20 (#12) | not tested (config) |
| CI is read only while the merger waits for it | Only heads whose merge decision is `waiting_ci` | Saves calls; reviewers never wait for CI | 2026-11-20 (#12) | test_intake.py::test_ci_is_read_only_while_the_merger_waits_for_it_and_then_it_approves |
| Drafts wait | A draft PR isn't recorded until it's marked ready | Reviewing drafts spends a plan on unfinished work | 2026-11-20 (#12) | test_intake.py::test_drafts_wait_until_they_are_ready |
| A person's own review keeps their doppel | GitHub drops a requested person who reviews. Intake reads the PR's reviews once to tell a review from an un-request, and only an un-request removes the doppel | The doppel posts as the App, not as the person, so their review doesn't conflict with it | 2026-11-20 (#12) | test_intake.py::test_a_persons_own_review_leaves_their_doppel_on_the_pr; test_intake.py::test_requested_again_after_reviewing_then_un_requested_removes_the_doppel |
| Outsiders' PRs run nothing unasked | Only OWNER, MEMBER and COLLABORATOR authors get the app's reviewers automatically (`[intake] auto_review = "insiders"`). An outsider's PR runs a reviewer only when one is requested or an insider's command asks | Anyone can open a PR on a public repo; a stranger mustn't spend a member's plan | 2026-11-20 (#13) | test_intake.py::test_an_outsiders_pr_runs_nothing_unasked; test_intake.py::test_an_outsiders_pr_runs_what_an_insider_asks_for |
| One repo's failure never stops the others | Logged per repo and pass | — | 2026-11-20 (#12) | test_intake.py::test_one_repo_failing_never_stops_the_others |
| A webhook only triggers a poll | Its contents are never acted on directly | A lost, repeated or reordered event can't put the ledger in a wrong state; a forged one can at most cause a read | 2026-11-20 (#13) | test_webhooks.py::test_an_event_polls_its_repo_and_a_redelivery_is_ignored |
| Webhook signature checked by the runtime | `X-Hub-Signature-256` with the secret file the runtime holds; unsigned or wrong → 401 | The BFF only forwards; the runtime trusts GitHub, not the forward (RC's review #7) | 2026-11-20 (#13) | test_webhooks.py::test_rc_7_an_unsigned_or_wrongly_signed_forward_is_refused |
| A burst of events becomes one poll | Wait 0.5 s, poll once; events during a poll cause exactly one more | A push fires several events | 2026-11-20 (#13) | test_webhooks.py::test_a_burst_becomes_one_poll_and_events_during_a_poll_one_more |

## Not yet

- Reading review threads and replies as dispositions: a person resolving a finding (RC's review #4).
- Repos added or removed through `installation_repositories` events. Today the config lists them.
- Commit statuses (the older CI API) alongside check runs; more than 100 check runs on one head.
- A person who reviewed in an earlier round, was re-requested, then truly un-requested still counts as having reviewed, so their doppel stays.
