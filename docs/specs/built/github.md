# GitHub App

> **GitHub moved to Doppelwerk** (doppel-runtime BFF checkpoint 7, 2026-11-24): the Doppelcloud App, in `server/github/` (checkpoints 5 and 6); a runner never holds the App key, and fetches with each job's one-repository read token. The runtime's Python code and the tests named below are gone; this page is kept as the record of the decisions that were ported, now held by server/reviews/publish.test.ts in agentcloud-apps/doppelwerk. See [bff-split.md](../bff-split.md).

The runtime acts on GitHub only as an App: never as a person. Reviews, comments and the merger's approval come from `<app-name>[bot]`. A personal doppel's review is labelled with its owner ("steven's doppel") but posted by the App, so it never mixes with the person's own reviews. Code: [doppel_gateway/github/](../../../src/gateway/doppel_gateway/github/). Setup: [docs/github-app.md](../../github-app.md).

## Supports

- **Tokens:** a JWT signed with the App's private key buys an hour-long token per installation, one installation per tenant.
- **Reads, for intake:**
  - open PRs (head, base, requested reviewers, draft, author association);
  - a PR's state (open, merged, closed);
  - PR comments since a time (issue comments left out);
  - who reviewed a PR;
  - CI from check runs.
- **Writes, for the scheduler and merger:**
  - a review (COMMENT or APPROVE) with inline comments, each finding mapped to its comment id;
  - dismissing a review;
  - a PR comment;
  - the status comment, edited in place.
- **Fetching:** the mirror fetches with the installation's token.
- **Config:** `[github] mode = "app"`, `app_id`, `private_key` (a file), optional `webhook_secret` (a file), and `installation_id` per tenant.
- **Live check:** `scripts/live_github_check.py` is read-only.

## Locked decisions

| Decision | Value | Why | Since | Proven by |
| --- | --- | --- | --- | --- |
| Least permissions | Pull requests read/write; contents, checks, commit statuses and metadata read. Never contents write | A reviewer never pushes | 2026-11-20 | docs/github-app.md (not testable in code) |
| The private key is a file only the runtime can read | Refused at start unless chmod 600; never logged, never in a repr | The one key pair in the system | 2026-11-20 (#13) | test_runtime_reviews.py::test_app_mode_refuses_a_key_others_can_read_and_a_missing_installation; test_github.py::test_no_token_or_key_in_reprs |
| Tokens reused until 5 minutes before expiry | One per installation, shared by reads, posts and fetches | Fewer token calls; never use one about to expire | 2026-11-20 (#13) | test_github.py::test_one_token_per_installation_reused_until_near_expiry; test_runtime_reviews.py::test_app_mode_reviews_a_pr_on_github_as_the_app |
| A 401 gets a new token, once | Then the error stands | A token can be revoked early | 2026-11-20 (#13) | test_github.py::test_a_revoked_token_is_replaced_once |
| A request uses the installation of its repo | A repo with no configured installation, or the wrong one, is refused; repo names are validated (`../etc` refused) | One tenant's token never touches another's repo | 2026-11-20 (#13) | test_github.py::test_a_repo_outside_the_installation_is_refused |
| A spent rate limit stops with its reset time | `RateLimited` carries when GitHub resets | Retrying into a limit makes it worse | 2026-11-20 (#13) | test_github.py::test_a_spent_rate_limit_says_when_it_resets |
| A finding is never lost to an off-diff line | If GitHub refuses inline comments (422), they move into the review body | GitHub only allows comments on lines in the diff | 2026-11-20 (#13) | test_github.py::test_a_comment_outside_the_diff_moves_into_the_body_instead_of_failing |
| A deleted status comment is posted again | Editing a missing comment (404) creates a new one | Someone may delete it | 2026-11-20 (#13) | test_github.py::test_approve_dismiss_reply_and_the_status_comment |
| The token never reaches disk or argv | Git gets it per fetch as environment config (`http.https://github.com/.extraheader`), never in a URL or the mirror's config | Other users can read argv; mirrors persist | 2026-11-20 (#13) | test_mirrors.py::test_fetch_credentials_reach_git_per_call_and_are_never_stored; test_github.py::test_git_gets_the_token_through_its_environment_only |
| CI from check runs | Any not completed → pending; any failure, timed_out, cancelled, action_required, startup_failure or stale → failure; neutral and skipped pass | Matches GitHub's merge box | 2026-11-20 (#13) | test_github.py::test_ci_from_check_runs |
| Installable only on this org, for now | "Only on this account" until the conditions in docs/github-app.md section 5 hold | An install alone must never run anything | 2026-11-20 | docs/github-app.md |

## Not yet

- The App itself: `doppelwerk-dev` created and installed on a sandbox repo, and the live run (checkpoint 6b).
- Reading review threads (dispositions); `installation` and `installation_repositories` events.
- GitHub Enterprise hosts other than github.com for fetching (the git header is scoped to github.com).
