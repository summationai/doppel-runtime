# Merger

> **The merger moved to Doppelwerk** (doppel-runtime BFF checkpoint 7, 2026-11-24): `server/reviews/merger.ts` and `publish.ts` (checkpoints 5 and 6). The runtime's Python code and the tests named below are gone; this page is kept as the record of the decisions that were ported, now held by server/reviews/merger.test.ts, server/reviews/publish.test.ts in agentcloud-apps/doppelwerk. See [bff-split.md](../bff-split.md).

One merger per PR, and it is the only thing that approves. Reviewers only comment. After every event it decides the PR's state for the current commit, from the reviewers' verdicts, CI, and open P0/P1 findings. It posts a formal review only when that decision changes, and keeps one status comment up to date. Code: [core/merger.py](../../../src/control-plane/doppel_control_plane/core/merger.py), `merge` and `merge_input` in [scheduler.py](../../../src/control-plane/doppel_control_plane/scheduler.py).

## Supports

- **States:** `waiting_reviews`, `waiting_ci`, `blocked`, `approved`, `dismissed`.
- **Decided in this order, for the current commit:**
  1. Is there an app reviewer? Otherwise `waiting_reviews`.
  2. Every required reviewer has a verdict? Otherwise `waiting_reviews`, naming who.
  3. Did an app reviewer stand down? Then `blocked`: a person has to review.
  4. CI not started or pending → `waiting_ci`; CI red → `blocked`.
  5. Any open P0/P1 → `blocked`, listing them.
  6. Otherwise `approved`.
- **Posts:** APPROVE when approved, COMMENT (listing the findings) when blocked, nothing for the waiting states.
- **The status comment:** one per PR, edited in place, e.g. `**waiting ci** · repo-owner ✓ · steven ✓ · CI not started`.
- **Dispositions:** a person resolving or answering a finding closes it for the merger.

## Locked decisions

| Decision | Value | Why | Since | Proven by |
| --- | --- | --- | --- | --- |
| Only the merger approves | Reviewers post COMMENT; the merger posts APPROVE | One approval per PR, after CI (DSV-11) | 2026-11-19 | test_scheduler.py::test_reviewers_only_comment; test_scheduler.py::test_dsv_11_approves_once_every_reviewer_is_done_and_ci_is_green |
| No app reviewer, no approval | A PR with only personal doppels is never approved | Nobody the app trusts has looked | 2026-11-19 | test_merger.py::test_decide |
| App reviewers are always required | The merger waits for every app reviewer's verdict | They are the repo's own review | 2026-11-19 | test_merger.py::test_decide |
| A requested person's doppel: required for 2 hours | Required for 2 hours from when it was first queued on the current commit, then not waited for. Each person has their own clock; a blocked or out-of-usage doppel counts the same. The status comment shows until when | A slow or offline reviewer shouldn't hold a PR forever (RC's review #1) | 2026-11-20 (#8) | test_scheduler.py::test_a_requested_review_is_waited_for_2_hours_then_not; test_scheduler.py::test_a_requested_reviewer_without_a_sign_in_holds_the_pr_at_most_2_hours; test_scheduler.py::test_each_requested_reviewer_has_its_own_clock |
| The clock running out needs no event | `tick` re-runs the merger for PRs waiting on reviews | Otherwise a quiet PR would wait forever | 2026-11-20 (#8) | test_scheduler.py::test_a_requested_review_is_waited_for_2_hours_then_not |
| Only P0/P1 block | P0 or P1 from any reviewer, personal ones included, that the diff introduced. P2/P3 never block | POC scope (RC's review #2): Doppelwerk's other guards come later | 2026-11-20 | test_scheduler.py::test_dsv_11_a_personal_reviewers_p1_blocks_with_a_comment |
| A P1 the diff didn't introduce is advisory | `introduced_by_diff: false` never blocks | The PR didn't cause it | 2026-11-19 | test_scheduler.py::test_a_pre_existing_p1_is_advisory |
| A late P0/P1 still blocks | A P1 posted after the 2-hour grace still blocks, and withdraws an approval already given | Not waiting for someone ≠ ignoring what they found | 2026-11-20 (#8) | test_scheduler.py::test_dsv_11_a_p1_after_the_grace_period_withdraws_the_approval |
| Removing a reviewer never clears its findings | A P0/P1 already posted on the current commit keeps blocking after its doppel leaves the PR (un-requested) | A quick human review or an un-request mustn't unblock a real bug | 2026-11-20 (#12) | test_intake.py::test_a_doppels_p1_keeps_blocking_after_the_person_reviews_or_is_un_requested |
| A person can clear a finding | A disposition (resolved or answered) closes it, with no model run; raised again in a later verdict, it's open again | People outrank the model | 2026-11-19 | test_scheduler.py::test_dsv_11_a_person_resolving_the_p1_unblocks_with_no_model_run; test_scheduler.py::test_a_finding_raised_again_after_its_disposition_is_open_again |
| Blocked posts a COMMENT | Never REQUEST_CHANGES | A bot's REQUEST_CHANGES sticks until dismissed by hand | 2026-11-19 | test_scheduler.py::test_dsv_11_a_personal_reviewers_p1_blocks_with_a_comment |
| CI must be green | Pending → wait; red → blocked | DSV-11 | 2026-11-19 | test_scheduler.py::test_dsv_11_red_ci_blocks |
| An approval is withdrawn on any move away from approved | Dismissed on a push, on red CI after approval, on a late P1; not only on a new commit | A COMMENT doesn't cancel an APPROVE on GitHub (RC's review #1) | 2026-11-20 | test_scheduler.py::test_dsv_11_a_push_dismisses_the_approval; test_scheduler.py::test_dsv_11_red_ci_after_approval_withdraws_it_on_the_same_head |
| Post only when the decision changes | One formal review per change; the status comment is edited, and only when its text changes | No spam on the PR | 2026-11-19 | test_scheduler.py::test_the_merger_posts_only_when_its_decision_changes |
| A stood-down app reviewer blocks | `stood_down` from an app reviewer → blocked | It couldn't review: a person must | 2026-11-19 | test_merger.py::test_decide |

## Not yet

- Doppelwerk's other approval guards: partial reads, truncated diffs, the file ceiling, critical and spec PRs, trust levels (RC's review #2: later).
- Who may clear a blocking finding (any person today), and matching a finding across commits by its anchor (RC's review #4).
- Reading dispositions from GitHub. The scheduler has the event; intake doesn't read review threads yet.
