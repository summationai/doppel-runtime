# Build checkpoints

How [doppel-service.md](doppel-service.md) gets built, in checkpoints that each end in a run seen working. A checkpoint is done only when every "Done when" line has been shown by a run or a test, not by reading the code.

As of 2026-11-19 (Thursday). "Friday" in the spec is 2026-11-20.

## Stack

- Python 3.12+, managed with `uv`. Doppelwerk and steven-universe are both Python, so the ports stay close to their sources.
- SQLite (WAL) for the ledger, one file on the runtime's own disk.
- OpenBao for secrets; a dev server (`bao server -dev`) on the laptop, a real one on summini.
- Docker for pods. `runc` on the laptop; `runsc` (gVisor) on summini.
- Tests: `pytest`. A test that proves a rule has the rule's ID in its name, e.g. `test_dsv_10_new_head_stops_old_reviewers`.

## Overview

| # | Checkpoint | Where | Needs from people | Proves |
| --- | --- | --- | --- | --- |
| 0 | Spikes: the Codex unknowns | Laptop | — | Open questions 1, 2, 3, 5 |
| 1 | Skeleton and ledger | Laptop | — | DSV-14 (schema), restart |
| 2 | Scheduler and merger, with fakes | Laptop | — | DSV-4, 10, 11, 12 |
| 3 | Broker | Laptop | Steven and Troy each sign in to Codex once | DSV-3, 13 |
| 4 | Gateway | Laptop | — | DSV-8 |
| 5 | Sandbox: image, harness, pod per PR | Laptop (runc) | — | DSV-5, 6, 7, 9 |
| 6 | GitHub: App, polling, posting | Laptop | Someone with org admin creates doppelwerk-dev; a sandbox repo | DSV-2, 10, 11 |
| 7 | Summini | Summini | Steven approves each install | gVisor, open question 7 |
| 8 | The Friday demo | Summini | Troy: the BFF contract and UI | The demo list in the spec |
| 9 | After the demo | Summini | — | Webhooks, API keys, idle cleanup |

Checkpoints 0 to 2 need nobody and nothing installed. Start there.

## 0. Spikes: the Codex unknowns

**Done 2026-11-19.** Findings in [docs/spikes/codex-sdk.md](../spikes/codex-sdk.md): one Codex process per reviewer run; `interrupt()` stops one reviewer; `project_doc_max_bytes = 0` keeps `AGENTS.md` out; the access token lasts 10 days.

Answer the open questions that decide the harness's shape before writing it. Each spike is a throwaway script under `spikes/` plus a short note in `docs/spikes/<name>.md`; the spec is updated with the answer.

| Spike | Question | How | Done when |
| --- | --- | --- | --- |
| 0a | Several capabilities in one `codex app-server`? (OQ 3) | A fake gateway that logs the `Authorization` header; two SDK threads in one process with different capabilities | The note says yes or no, with the logged headers as evidence. No → the harness runs one Codex process per reviewer. |
| 0b | Stop one running thread? (OQ 2) | Start two threads, interrupt one mid-turn | The other thread finishes; the note names the call that stops a thread |
| 0c | Keep the head's `AGENTS.md` out (OQ 5) | A checkout with an `AGENTS.md` canary instruction; run with `project_doc_max_bytes = 0` | The canary doesn't show in the output, and a repo-level `.codex/` config is shown not to load |
| 0d | Refresh timing (OQ 1) | Read steven-universe's broker; decode only the `exp` claim of a local access token, never printing the token | The note gives the real access-token lifetime and the refresh rule the broker will use |

## 1. Skeleton and ledger

**Done 2026-11-19.** `doppel-runtime serve`, the ledger, crash recovery and CI; 14 tests.

- `uv` project, package `doppel_runtime`, one process started by `doppel-runtime serve`.
- Five role modules (`intake`, `scheduler`, `broker`, `gateway`, `sandbox`) wired in one asyncio process, with three listeners: the gateway's model route, an internal route (posting, broker, runtime API), and health.
- The ledger: the `prs` and `slots` tables from the spec, plus Doppelwerk's commands, findings, posts and dispositions tables, all keyed by tenant. Migrations in code.
- Config from a file plus env; no secrets in the file.

Done when:
- [x] `doppel-runtime serve` starts and answers health on every listener.
- [x] Inserting the same slot twice, including a non-command slot (`command_id = ''`), leaves one row.
- [x] Killing the process with a slot `running` and restarting puts it back to `queued`.
- [x] CI runs `pytest` and a linter on every push.

## 2. Scheduler and merger, with fakes

**Done 2026-11-19.** `core/` (commands, payer, matching, merger), `roles/scheduler.py`, fakes for the ports; 107 tests including a layering test. The scheduler isn't wired into `serve` yet: it needs the real sandbox and GitHub ports (checkpoints 5 and 6).

The decisions are pure functions in `core/` (`matching.check`, `merger.decide`, `commands.parse`, `payer.payer_for`); `roles/scheduler.py` holds the event handlers and `tick(now)`. GitHub, Codex and Docker are faked.

- Events: PR opened, review requested, synchronize, closed, command, CI done.
- Commands: parse `@doppelwerk <command> [reviewers…] [description]` into `commands` (`commands` gains `command` and `description`); `review` queues one slot per target with `reason = command`; `help` and unknown commands reply.
- Conditions: settle (90 s, reset on each push), payer cap (2), usage window, host cap (10), payer has a sign-in (`blocked` if not). Reviewers don't wait for CI.
- The merger: a pure function of the PR's current head, its reviewers' verdicts, CI, findings and dispositions, giving `waiting_reviews`, `waiting_ci`, `blocked` or `approved`. The schema gains `merges`, `pr_reviewers`, `ci_status`, `usage_resets` and `prs.settle_until`/`status_comment_id`, drops `ci` from `slots.waiting_on`, and keeps the posted review id in `posts` only. (Edited into schema version 1: no ledger existed yet.)
- Head moves: running slots on the old head get a stop; queued ones move to the new head.
- Pod lifecycle: created on the first ready slot, idle after its last slot, deleted after 3 days idle or on close; a command brings it back.
- Who pays: personal → owner; app-wide → tenant key, else an opted-in owner; never anyone else.

Done when, as table-driven tests:
- [x] DSV-4: a slot never gets a payer other than its owner or an opted-in owner, even when that payer is capped or out of usage.
- [x] DSV-10: a push stops old-head slots and queues the new head in the same pod; close deletes the pod.
- [x] DSV-12: a third slot for one payer waits; a usage-limit reset holds that payer's slots until the reset.
- [x] Reviewers start without waiting for CI; CI only moves the merger.
- [x] DSV-11: the merger approves only with every required reviewer done on the current head, none stood down, CI green, and no open P0 or P1 from any reviewer; a personal reviewer's P1 blocks; a person resolving it unblocks with no model run; blocked posts a COMMENT; a push dismisses the approval.
- [x] The merger posts a formal review only when its decision changes.
- [x] Commands: `@doppelwerk review steven look at the retry cap` records command `review`, target `steven`, the description, and queues one command slot on the current head that skips the settle window; `@doppelwerk steven` and bare `@doppelwerk` mean review; an unknown command is refused with a reply.
- [x] DSV-4 with commands: Troy commanding Steven's reviewer runs on Steven's sign-in; it is refused when Steven wasn't requested on the PR.
- [x] A PR idle for 3 days loses its pod; a command recreates it on the current head.

## 3. Broker

steven-universe's broker, per user. Reuse its code where it fits.

- A `CODEX_HOME` per user under the runtime's data dir, mode `0700`.
- Device-code sign-in started through the internal route; the runtime returns the code and URL, the user finishes on OpenAI's page.
- `auth.json` copied to OpenBao at `kv/users/<user>/codex` with a per-user policy; restored from OpenBao at startup.
- Refresh by the broker, under a per-user lock, on the rule from spike 0d. Status (`connected / expired / revoked`) recorded and exposed for the BFF.

Done when:
- [ ] Steven and Troy each connect by device code, and the status shows `connected`.
- [ ] After deleting the local Codex homes and restarting, both come back from OpenBao.
- [ ] Two refreshes started at once for one user do one refresh, and the sign-in still works after.
- [ ] DSV-3 and DSV-13: one user's OpenBao policy can't read another's path; no token or `auth.json` content appears in any log (a test greps the logs).

## 4. Gateway

- Model route: capability → hash lookup → slot → payer → that payer's access token and account id; the request is passed to `chatgpt.com/backend-api/codex` with the response streamed back.
- Usage counted per slot; the capability's token budget and expiry enforced; a usage-limit answer records the payer's reset time.
- Posting route and broker on the internal listener only.

Done when:
- [ ] `codex exec` on the laptop, set to use the gateway as provider with a capability, completes a prompt on Steven's sign-in; the same with Troy's capability goes out on Troy's (the gateway's log shows the payer id, never the token).
- [ ] An expired or unknown capability gets a 401; an over-budget one a refusal.
- [ ] DSV-8: from a container on an `--internal` network with only the gateway's model route reachable, the internal listener and github.com both fail.

## 5. Sandbox: image, harness, pod per PR

- The image from the spec's table: Debian slim, non-root, read-only root, git, ripgrep, jq, the pinned Codex SDK and binary.
- The harness: reads `/job`, starts or stops one reviewer at a time on request (one process or one thread per reviewer, from spike 0a), validates each verdict against `verdict.schema.json`, writes `/out/<reviewer>.json`, wipes the run's scratch folder.
- The mirror: one bare `--filter=blob:none` clone per repo; a `git archive` checkout per head under a parent folder mounted read-only at `/work`.
- Rules and guides from the base ref into `/job`.

Done when:
- [ ] A pod reviews a head of a local test repo and writes a verdict that passes the schema.
- [ ] DSV-6 and DSV-7: inside the pod, `curl github.com` fails, no `gh` or GitHub token exists, and nothing from the PR has run (the test repo's `postinstall` canary never fires).
- [ ] DSV-9: the head's `AGENTS.md` canary has no effect; the base's rules do.
- [ ] A second head appears under `/work` without restarting the pod, and one reviewer is stopped while another keeps running.

## 6. GitHub: App, polling, posting

- A new doppelwerk-dev GitHub App with only the permissions a reviewer needs (no `contents: write`), installed on one sandbox repo.
- Installation tokens minted by the runtime and cached until shortly before expiry.
- Intake: Doppelwerk's poller searches ported, run every 60 s against the installation.
- Posting: Doppelwerk's `verdict.schema.json`, `criticality.decide` and poster rules, through the gateway's posting route.

Done when, on the sandbox repo:
- [ ] DSV-2: opening a PR gets a review from doppelwerk-dev[bot] within two poll cycles.
- [ ] DSV-10: a push stops the old head's reviewers and the new head gets reviewed; stale approvals are dismissed.
- [ ] DSV-11: a P0 finding posts as COMMENT; a clean app-wide verdict with CI green posts as APPROVE; a personal reviewer only ever comments.
- [ ] Merging deletes the pod and its checkouts; the ledger keeps the posted reviews (DSV-14).

## 7. Summini

Every install on summini is asked of Steven first. Nothing touches user `rc` or Doppelwerk.

- A new macOS user for the runtime.
- A Linux VM (choice from open question 7), Docker, `runsc` registered as a runtime.
- OpenBao, with the unseal decision made (by hand or auto-unseal).
- The runtime deployed, running checkpoint 6's flow.

Done when:
- [ ] A pod runs under `runsc` (`docker inspect` shows the runtime) and checkpoint 5's checks pass there.
- [ ] Checkpoint 6's flow passes on summini.
- [ ] `rc`'s processes (`doppeld`, `doppelboard`, `engine-broker.py`) are the same before and after.

## 8. The Friday demo

The spec's demo list, end to end on summini: two users, one sandbox repo, one personal reviewer per user and one app-wide reviewer as three reviewers in the PR's one pod, each on its own payer's sign-in, posting as doppelwerk-dev[bot]; a push moves to the new head in the same pod; merging deletes the pod; the BFF shows each user's connection status and the run history.

Needs from Troy: the BFF contract (records in, credential status out) and the UI.

## 9. After the demo

- Webhooks through the BFF, with the poll kept as reconcile.
- API keys pasted through the UI into OpenBao.
- The OpenAI questions sent.
- Idle cleanup and reactivation run for a week on real PRs.

## What fits by Friday

Friday is one day out. A judgment, not a measurement:

- **Likely:** checkpoints 0 to 2, and 4 with a live call through the gateway on two people's sign-ins. That shows the core claim: the review runs without its pod holding any token, and the gateway pays as the right user.
- **Possible with a good day:** 3 and 5 on the laptop.
- **Not by Friday:** 6 to 8. They need the GitHub App created, installs on summini approved, and Troy's side.

So the Friday demo should be the laptop run of 0 to 4 (and 5 if it lands), with 6 to 8 shown as the plan.
