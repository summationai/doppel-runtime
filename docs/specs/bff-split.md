# The split: Doppelwerk's BFF orchestrates, the runtime runs reviews

Status: decided 2026-11-23, written against the BFF at v62 (`ef05a8c`). Checkpoints 0 to 6 are live in the BFF (v63, v64, v66, v67, v69, v70, v71); see section 5.

## Decided

| Decision | Choice |
|---|---|
| Where the rewrite lands | In place, in `agentcloud-apps/doppelwerk`. Codex crews are paused until cutover. |
| GitHub App | Doppelcloud (5837617). `doppelwerk-dev` is dropped. |
| One path | The Mac runtime is the only thing that runs reviews. The Rust laptop companion, its endpoints and the legacy settings compatibility are removed. |
| Live data | Keep users, orgs, members, GitHub accounts and installations, enrolled repos, doppels, review settings and ownership. Drop companion devices, review attempts, drafts and job history. |
| D2 unit of work | Ours: one review per (PR, head, doppel). |
| D3 who pays | Ours: a personal doppel's owner pays; an app doppel's named payer pays. |
| D4 triggers | App doppels follow the org's `event`. Personal doppels run when their owner is requested. Commands always run. |
| Force pushes | Changed 2026-11-24: the workspace chooses. **On new pushes** reviews a new head only when it extends the last one (GitHub's compare says `ahead`); **On all events** reviews any new head, rebases, amends and force pushes included. Both review a PR that opens, reopens or is marked ready. Any push still stops the old head's reviews, and a requested person's doppel reviews every new head. |
| D7 approval | Ours: the merger. |
| D8 trust | Ours: every doppel posts a COMMENT review. The trust ladder and private drafts are removed. Per-repo publishing on/off stays as an owner's kill switch. |
| Unclaimed work | A slot no runtime claims within 2 hours expires. |
| Who closes a finding (RC #4) | Decided 2026-11-24: **(b)** anyone with write access, the PR's author included, by resolving the finding's thread; **plus the doppels:** each later review by a doppel gets that doppel's own still-open findings on the PR (never another doppel's) and says which the new commit fixes, and Doppelwerk resolves those threads. An open P0/P1 carries forward across commits until fixed or resolved; a bare reply doesn't close it. |

Sections 2 to 4 below are the analysis behind these. Where a "Recommend" there differs from this table, the table wins.

Two repos are involved:

- **BFF**: `agentcloud-apps/doppelwerk`, cloned at `~/Desktop/Github/cloud-apps/doppelwerk`. It is a TypeScript app (Hono, React, Postgres) on agent.cloud, built as releases v1 to v51.
- **Runtime**: this repo, a Python service that runs on the Mac mini.

## 1. What each side owns

| | BFF (agent.cloud, Postgres) | Runtime (Mac mini) |
|---|---|---|
| People and orgs | sign-in, organizations, members, invitations, GitHub accounts | none: the `[[tenants]]` TOML goes away |
| Doppels and policy | doppels (personas, kind, payer), ownership, review settings, filters | none |
| GitHub App | the key, installation tokens, webhooks, reading PRs, posting reviews, checks | none: no App key on the box |
| Orchestration | what to review (intake), queueing, matching to a payer, leases, retries | its own host cap only |
| Decision | the merger: approve, dismiss, block, status | none |
| Model sign-ins | never sees them | the credential broker: each payer's `auth.json` stays on the box |
| Execution | none | gateway (model proxy, capability, token count), sandbox pods, mirrors, verdict check |
| Ledger | the system of record | a small local store: pods, jobs in flight, token counts |

The runtime becomes a daemon that only connects outbound, like their Rust companion. It polls the BFF, claims a job, runs it in a pod and returns the verdict. It has no listening port facing GitHub, and the internal webhook listener goes away.

### What moves out of this repo

| Runtime module | Where it goes |
|---|---|
| `intake.py` (the poller, budget, cursors, person-reviewed check) | A BFF webhook handler, plus a reconcile job as a backstop |
| `webhooks.py` | Already in the BFF (`server/intake/webhooks.ts`, PR metadata only); extend it |
| `scheduler.py`: events, slots, `_reap_idle` (decides when) | BFF |
| `matching.py`: payer signed in, settle, usage, payer cap | BFF claim query; host cap stays in the runtime |
| `merger.py`, status comment, approve and dismiss | BFF job |
| `doppel_gateway/github/rest.py`: posting | BFF, checkpoint 5. Their old posting code went with the companion in checkpoint 1; port ours, with its exact-head check and 422 fallback |
| `doppel_gateway/github/client.py`: App JWT, tokens | Already in the BFF (`server/github/app.ts`: `appJWT`, `installationToken`) |
| `commands` (`@doppelwerk review`) | BFF (needs the `issue_comment` event) |
| `config.py` `[[tenants]]`, `ConfigDirectory` | BFF tables |

**What stays:** the broker, the gateway, pods, mirrors, the verdict schema check, and the sandbox image.

The locked decisions in `docs/specs/built/` are the porting spec. Each row with a "Proven by" test must have a TypeScript test before the Python one is deleted.

## 2. The runtime's protocol

The runtime only ever calls out to the BFF; nothing calls in. Their companion protocol (pair code → device credential, a 30 s poll, start and finish per review) had the right shape, but checkpoint 1 removed it with the companion. Checkpoint 2 built the runtime's own protocol in its place (§4b; BFF `server/runtimes/`, runtime `doppel_control_plane/bff.py`):

```
POST /api/runtime/pair      {code: dwp_…, credential: dwr_… the Mac made} → which Mac, which workspace
POST /api/runtime/seats     {code: dws_…} → the seat: id, payer `<workspace id>/<member id>`, member and login
POST /api/runtime/check-in  {version, capacity, running: [job ids], seats: [{id, state, plan, quota}]}
                            → {paused, caps, seats, remove_seats, jobs, check_in_after_seconds} | {revoked: true}
POST /api/runtime/jobs/:id  the result: verdict (D6) or failure, with each engine tried (checkpoint 4)
```

Pair and seat codes last 30 minutes and work once; the BFF stores only hashes, salted with the deployment. Seat states come only from check-ins. A seat the Mac reports that the BFF doesn't have (removed, or its member left) comes back in `remove_seats`; a seat the BFF lists that the Mac lost is adopted. A removed Mac gets `{revoked: true}`, signs every seat out and forgets its credential; a credential the BFF refuses (401) is kept, so a wrong address can't wipe sign-ins.

From what we learned about the companion protocol, the new one keeps these rules:

1. **A job carries a repo token.** It covers one repo with `contents: read`, minted with `installationToken()`, which already narrows tokens to `repository_ids`. The runtime never holds the App key, and the token reaches only the mirror's `git fetch`, through `git_env` as it does today.
2. **Check-ins renew leases.** A check-in lists its running jobs, and the BFF extends their leases, so a large review can outlast a fixed lease.
3. **Many jobs at once.** The companion's limit was one active review per person. Ours is a payer cap (default 2) plus a host cap, and one Mac serves many payers.
4. **The result is our verdict.** See D6.

## 3. Merging the ledgers

> Written before checkpoint 1. The `companion_*` tables this section maps onto were dropped in v64, so where it says "theirs, extended" for them, read "new": `runtimes` and `runtime_seats` come in checkpoint 2 and the slot table in checkpoint 3, with the columns listed here. D1's one-device-per-payer idea is replaced by one runtime with many seats (§4b).

Postgres in the BFF is the system of record. For each of our SQLite tables:

| Ours | Theirs | Merge |
|---|---|---|
| `prs` | `pull_requests` | **Theirs.** Add `settle_until`, `status_comment_id`, `status_body`, `last_review_at`. `base_ref` exists already as `base_branch`. `pod_id` and the `idle` state are the runtime's own. |
| `pr_reviewers` | none | **New** `pull_request_doppels`: (pr, doppel, source `app\|requested`, person_reviewed, added_at) |
| `slots` | `companion_reviews` | **Theirs, extended.** Add `doppel`, `payer_id` (who must pay), `command_id`, `waiting_on`, `not_before`, `attempts`, and `verdict jsonb`. `tokens_used` and `provider_status/code` go into each entry of their `runner_usage`, which since v55 lists every engine tried. They already have `offered_at`, `connection_name` and `eligible_engines`. The source event gains `command`. Change the dedupe key (see D2). |
| none | `companion_review_expired_offers` | **Theirs.** It is the offer history; a lapsed offer is also a failed attempt for us (D12). |
| slot states | review states | queued→`queued`; blocked→`queued` with `waiting_on='no_runner'`; running→`offered`/`running`; posted→`complete` and published; failed→`failed`; skipped→`failed` with their skip reasons (`pr_merged`, `pr_head_changed`, …); cancelled→**new** `cancelled`. |
| `merges` | none | **New** `pull_request_decisions`: (pr, head, decision, reason, review_id, decided_at) |
| `ci_status` | none (publication asks GitHub Actions at post time) | **New** `ci_status`, fed by `workflow_run` and `check_suite` webhooks |
| `usage_resets` | `companion_devices.providers[].quotas[].resets_at` | **Theirs.** The broker reports each payer's quota on poll. |
| `commands`, `command_responses` | none | **New**, ported as they are |
| `findings` | `review_details.findings` (jsonb) | **Theirs.** Findings stay in the review's JSON. |
| `finding_anchors` | none | **New** `review_comments`: (review, finding id, GitHub comment id, path, line). Dispositions need the thread. |
| `posts` | `publication_status`, `github_review_id/url` | **Theirs** |
| `dispositions` | `finding_states` (jsonb, set in the UI) | **Unify** (see D10) |
| `intake_cursors` | none | **New**, for the reconcile job |
| `[[tenants]]` TOML | `organizations`, `organization_members`, `github_installations`, `config.repositories`, `review_configurations.doppels`, `reviewer_profiles`, `github_accounts` | **Theirs** |

How our names map to theirs:

- tenant → `organizations.id`
- payer `summation/u-steven` → `users.id`, plus the `companion_devices` row the runtime holds for that person
- reviewer handle → doppel `handle` in `review_configurations.doppels`
- a personal reviewer's `login` → `github_accounts.login`

What the runtime keeps locally (SQLite, never synced):

- pods: (repo, pr) → pod id and last used
- jobs in flight: review id, lease, capability hash
- the gateway's token counter

## 4. Where the two designs overlap or disagree

**D0. Which GitHub App.** We were waiting for an org owner to create `doppelwerk-dev`. **Doppelcloud** (App 5837617, owned by summationai) already exists, with `contents:read`, `pull_requests:write`, `checks:write` and `actions:read`, and with webhooks. *Recommend:* use Doppelcloud and drop `doppelwerk-dev`. Our checkpoint 6b App code (`client.py`, `rest.py`, `webhooks.py`, `intake.py`) has a TypeScript counterpart there already. What carries over from 6b is its locked decisions, not the Python. Add the events we need: `pull_request_review` (a person reviewed), `issue_comment` (commands) and `check_suite`.

**D1. What a "device" is when the Mac mini serves several payers.** Their `companion_devices` row is one device for one person. *Recommend:* each payer pairs the Mac mini as their own device, using a one-time code from Connections. The runtime holds one device credential per payer.

This reuses their device UI as it is:
- usage caps, provider order and pause;
- the activity chart;
- **Revoke**, which takes a payer off the host.

It also makes each payer's consent to pay explicit. The host cap (10) stays in the runtime: it stops claiming. The payer cap (2) becomes the device's concurrency.

**D2. The unit of work: one review per head, or one per doppel per head.** Theirs is one review per (org, repo, PR, head), whoever claims it. Ours is one slot per (PR, head, doppel), and several doppels on a PR is a core feature. Their parity doc lists it under the "original runtime" features still missing. *Recommend:* add `doppel` to `companion_reviews` and dedupe on (org, repo, PR, head, doppel, source/command). An org with one doppel behaves as it does today.

**D3. Who pays. Open.** In theirs, any eligible member's device claims any org review, so `reviewer_id` is whoever claimed it. Since v58 that pool also takes turns between people, so the work is spread across members. In ours, a personal doppel is paid for by its owner, and an app doppel by its named payer. *Recommend:* ours. The claim query only offers a review to devices whose user is that review's `payer_id`. Pooling could stay as an opt-in for app doppels with no named payer.

**D4. What starts a review.** As of v56–v62, theirs uses one org setting, `event`: `never | opened | push | all`.

- Saved `ci` now means `push`, and saved `requested` now means `never`. **The BFF dropped "review when requested".**
- A push counts only when GitHub's compare says the new head is `ahead`. A force push that rewrites history does not count.
- `workflow_run` no longer starts reviews. It only re-queues one that was skipped as `ci_pending` or `ci_failed`.
- Bots are excluded by an author filter, `*[bot]`, which replaces the old toggle.

Ours:
- app doppels review every new head, after a 90-second settle;
- personal doppels review when their owner is requested;
- a command starts one on demand.

*Recommend:* app doppels follow the org's `event` setting. Personal doppels always follow a review request; this brings back, for them only, what v62 removed. Commands always run.

**Open:** what to do about a force push. Ours reviews every new head. Theirs skips one that isn't `ahead`, although after a rebase the code being merged has changed. I'd review it: the merger has already dismissed its approval of the old head, so without a review the PR waits with nothing coming. `push` keeps our 90-second settle.

**D5. How a review runs.** Theirs runs on a laptop: it fetches the diff with `gh`, and the model sees only the diff, with no tools. Ours runs in a sandbox pod with a read-only checkout of the repo, network only through the gateway, and a verdict that must match the schema. *Recommend:* keep both. A `runner` type decides which. Reviews that need the code base (multi-doppel, specialist) go only to sandboxed runners.

**D6. The result format.** Their `ReviewDetails` has:
- summary
- confidence 1–5 with a reason
- risk
- up to 8 findings, P0–P2, each with a location of path, side, line and code

Our verdict has:
- `verdict`: clean, comments, changes_requested or stood_down
- findings P0–P3, each with an `id`, `introduced_by_diff`, `claim` and `evidence`

The merger needs `id` (to follow a finding across heads and for dispositions) and `introduced_by_diff` (the blocking rule). *Recommend:* make one schema by extending `ReviewDetails` with optional `id`, `introduced_by_diff`, `evidence`, severity `P3`, and a top-level `verdict`. Their UI, analytics and publication then keep working, and the runtime sends that one shape. The two lists of categories are merged.

**D7. When to approve. Open.** Theirs judges one review at a time (`approvalEvent`): approve when `autoApprove` is on, there are no findings, confidence and risk are within the caps, risk paths are checked, and every Actions run passed. Ours is the merger: approve when every required doppel has posted on this head, no open P0/P1 introduced by the diff remains (including from doppels since removed), and CI is green. Personal doppels are waited for up to 2 hours. A move away from approved dismisses the approval. *Recommend:* our merger as a BFF job that runs after each finish, CI result or PR event. Their confidence, risk and risk-path caps become extra gates in it. Doppel reviews post as COMMENT. Only the merger approves.

**D8. Trust and publishing.** Theirs has the Shadow / Commentator / Gatekeeper / Merge-authority ladder, private drafts by default, and publishing switched on per repo. Ours posts every review. *Recommend:* use the ladder as the merger's input.

| Trust level | What the doppel's review does |
|---|---|
| Shadow | Private draft only |
| Commentator | Posts, never blocks |
| Gatekeeper | Its P0/P1 findings block |
| Merge authority | Its clean verdict counts toward approval |

The per-repo publishing switch stays as a kill switch.

**D9. Commands.** Ours has `@doppelwerk review`. Theirs has no commands. *Recommend:* port them to the BFF, using the `issue_comment` webhook.

**D10. Who closes a finding.** Theirs records outcome badges set in the UI (`finding_states`). Ours reads GitHub: a resolved thread or a dismissive reply (RC review #4, not built yet). *Recommend:* one `finding_dispositions` table, keyed (PR, doppel, finding id), with `source ui|github` and who did it. The RC #4 question of who may clear a blocking finding then answers for both. Decided: (b) plus doppel auto-resolve (see the Decided table).

**D11. Hearing about GitHub changes.** Theirs is webhook only. Ours polls with a per-installation budget, plus webhook pokes, because the Mac mini has no public address. The BFF has one. *Recommend:* webhooks first, plus our poller as a reconcile job every 10 minutes for missed deliveries. Its budget and cursor rules carry over.

**D12. Leases.** Theirs has fixed leases: 30 seconds offered (5 minutes before v58), 15 minutes running, and expiry after 24 hours. Ours had no leases, because everything ran in one process. A runtime that polls over the network can die mid-review, so *recommend:* their leases, renewed while running (protocol change 2). A lapsed running lease counts as a failed attempt, and the review is offered again up to the attempt limit.

**D13. Usage limits.** Theirs uses quota percentages the CLI reports, against caps the person sets. Ours records `usage_resets` from the provider's 429s and counts tokens in the gateway. *Recommend:* the broker reports each payer's quotas in their format on every poll. That covers the reset time it saw and the cap. The gateway's token budget stays local. The tokens and the provider's answer for each run go back in the finish body's `runs`.

## 4b. Onboarding a Mac, and who pays on it

Nothing about a member's model sign-in reaches the BFF. The BFF stores who is ready where, and only the Mac tells it that.

| What | Where |
|---|---|
| A member and their GitHub login | BFF: `users`, `organization_members`, `github_accounts` |
| A doppel and its review rules (persona files, instructions, scope, filters, severity) | BFF: `doppels` (org, handle, kind, owner, payer, rules, revision) |
| Org-wide rules (event, filters, approval conditions) | BFF: `review_settings` |
| A Mac | BFF: `runtimes` (id, org, name, host owner, credential hash, last check-in). The Mac keeps its credential in `data_dir`, chmod 600. |
| A member who can pay on a Mac | BFF: `runtime_seats` (runtime, user, engine, signed in, plan, quota, reported at). Written only from the Mac's check-ins. |
| A member's model sign-in | The Mac only: the broker's `<seat>/codex/auth.json`, chmod 600 |
| GitHub access for a review | Neither stores it. The BFF mints a one-repo, read-only token per job. |

**Adding a Mac.** The host owner clicks **Add a Mac** in Connections and gets a one-time code. On the Mac, `doppel-runtime pair <code>` stores the runtime's credential. The BFF keeps only its hash.

**Adding a seat.** A member, signed in to the BFF, asks for a seat on that Mac and gets a one-time seat code tied to them and to it. Someone at the Mac runs `doppel-runtime seat add <code>`, which starts Codex's device sign-in. The member approves it in their own browser, then the runtime redeems the code. A runtime can only report seats the BFF issued, so it cannot claim a member who didn't ask.

**Check-in.** Each check-in POSTs the runtime's roster: every seat, whether it is signed in, its plan, its quota used, the jobs running, and free capacity. The BFF answers with what it wants, and the Mac applies it:

- jobs to start;
- seats to remove;
- pause;
- caps;
- a revoked runtime.

The member → runtime map lives in `runtime_seats` and is written only from these posts.

**Running a job.** The BFF assigns a slot to a runtime only when the slot's payer has a ready seat on it, within the payer cap and the host cap. The job carries:

- the repo, PR, head and base;
- the repo token;
- a snapshot of the doppel's rules and their revision;
- the seat to use.

The Mac keeps no review rules.

**Unclaimed work.** A slot not claimed within 2 hours becomes `expired`.

| Doppel | What happens then |
|---|---|
| Personal | The merger stops waiting for it. |
| App | The PR stays unapproved, with the reason on its status comment. A push or `@doppelwerk review` queues it again. |

**What a member should know.** Whoever administers a Mac can read files on it, including the seats' sign-ins. A seat on someone else's Mac means trusting its admin.

## 5. Order of work

Eight checkpoints. Each one is built in the clone on its own branch, then shipped from the agc folder with `agc ship`.

| # | Checkpoint | What it builds | Proof | Status |
|---|---|---|---|---|
| 0 | Regroup the BFF | The server is grouped as `platform/`, `github/`, `workspace/`, `intake/`, `reviews/` and `shared/`, and `tests/architecture.test.ts` enforces the imports. Behaviour is unchanged. | 75 tests pass, 2 are skipped. | **Live, v63** (2026-11-23) |
| 1 | One path, clean schema | Doppels become rows (details below). Removed: the companion, its Rust program, profiles, drafts, review history, analytics and the ChatGPT/Grok links. Settings lose their dead keys. | All 28 release checks pass on a fork of production, including the data checks. | **Live, v64**, plus v65 removing a leftover download (2026-11-24) |
| 2 | Macs and seats | **BFF:** `runtimes` and `runtime_seats` tables, the "Add a Mac" pair code, seat codes, and the check-in POST. The Mac sends its roster; the answer says which seats to remove, whether to pause, the caps, and whether the runtime is revoked (§4b). **Runtime:** `doppel-runtime pair`, `seat add` (Codex device sign-in), and the check-in loop as a daemon. | A Mac paired to the local BFF shows its seats, and revoking it from the BFF stops it. | **Live, v66** (2026-11-24); runtime side in its own PR |
| 3 | Intake and slots | The webhook makes one slot per (PR, head, doppel) (D2); `push` reviews heads that extend the last, `all` any new head. App doppels follow the workspace's event and personal doppels run when their person is requested (D4). Each slot gets its payer (D3). A slot not claimed within 2 h becomes `expired`. | A PR webhook makes the right slots, and stale ones expire. | **Live, v67** (2026-11-24) |
| 4 | Jobs to the Mac | The check-in answer carries jobs, assigned only where the payer has a ready seat, within the payer and host caps. A job carries a one-repo read token and a snapshot of the doppel's rules. The Mac runs it in the sandbox and reports the result. | A Mac reviews a PR on a sandbox repo end to end. | **Live, v69** (2026-11-24); runtime side in its own PR |
| 5 | Posting and the merger | The BFF posts each doppel's review as a COMMENT as Doppelcloud (D8), and the merger decides and approves (D7), with its status. When a slot expires, the merger stops waiting for a personal doppel, and an app doppel's PR stays unapproved with the reason. The merger's locked decisions and tests are ported. | A PR gets its doppels' comments and the merger's decision. | **Live, v70** (2026-11-24) |
| 6 | Commands and dispositions | `@doppelwerk review` and the other commands, dispositions on findings, and the reconcile poll (D9–D11). | — | **Live, v71** (2026-11-24) |
| 7 | Cutover | The runtime's own intake, scheduler, merger and GitHub posting are deleted, and its ledger shrinks to the local tables. The Mac mini moves over, and the Codex crews can resume. | — | Next |

Checkpoint 6 in detail (migration `20261124220000_commands_and_findings`; BFF `server/intake/commands.ts`, `server/slots/jobs.ts`, `server/reviews/publish.ts`):

- **Commands (D9)**, ported from `core/commands.py` with its locked decisions:
  - `@doppelwerk review [doppels…] [what to look at]` and `help`, through the `issue_comment` webhook;
  - insiders only (OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR), never bots;
  - a person's doppel only where they were requested, on its own payer;
  - acted on once (keyed by the comment);
  - no settle wait, and it follows the head;
  - always answered: help, refusals, and a review that failed or found no Mac.
  A command's reviews are their own slots (`review_slots.command_id`), so a command can ask again for a head a doppel already reviewed.
- **Findings until they're closed (D10, RC #4: (b) plus doppel auto-resolve):**
  - `review_findings` keeps each finding of a review that was on the current head, with its GitHub comment, and it stays open across commits.
  - A job's instructions list its doppel's own open findings with keys. The verdict's `fixed` closes those keys, only that doppel's, and Doppelcloud resolves their threads (GraphQL `resolveReviewThread`).
  - A person resolving a finding's thread closes it. GitHub allows only write access and the PR's author, which is (b). Unresolving reopens it.
  - The `pull_request_review_thread` webhook reports this, and the merger also reads the PR's threads when an open P0/P1 has one, catching missed deliveries.
  - The merger blocks on the PR's open introduced P0/P1s, whatever commit raised them.
- **Shown** on the PR's page: a Findings card (open and whether it blocks, fixed by which doppel, resolved by whom); command reviews are labelled.
- **Runtime** ([#20](https://github.com/summationai/doppel-runtime/pull/20)): the verdict schema allows `fixed`, the prompt asks for it, and a command's focus reaches the reviewer. Rebuild the sandbox image to pick it up.
- **Not yet:** closing a finding from Doppelwerk's own page (D10's UI source); commands beyond `review` and `help`.

Checkpoint 5 in detail (migration `20261124200000_posting_and_merger`; BFF `server/reviews/merger.ts`, `publish.ts`, `server/github/reviews.ts`):

- **Posting (D8).** A finished slot queues `review_publish`, which runs at once (JOB-5) with the worker as backstop. It posts one COMMENT review by Doppelcloud on the head it reviewed, never on a moved head. It honours the workspace's comment header, summary toggle, findings toggle and minimum severity. A personal doppel's review is labelled "(<person>'s doppel)". GitHub's 422 for a line outside the diff moves the findings into the body. The token is one repository's, with `pull_requests:write` and `checks:read`. Reviews post only where an owner turned publishing on (the repository, or one PR); otherwise `not_posted` says why. When this shipped, no repository had it on.
- **The merger (D7)**, ported from `core/merger.py` with its locked decisions:
  - no app doppel, no approval;
  - every app doppel is required, and a requested person's doppel for 2 hours from its first slot on the head, or until that slot expires or fails;
  - a stood-down app doppel blocks, and so does red CI;
  - any P0/P1 the diff introduced blocks, from any doppel, including removed ones;
  - it posts APPROVE, or a COMMENT when blocked (never REQUEST_CHANGES), only when its decision changes;
  - it dismisses an approval on a push, red CI or a late P1, and keeps one status comment (a deleted one is posted again).
- **Quiet until it has something to say.** The merger reads CI only once the reviews are in, and mints a token only when it calls GitHub. Its status comment appears only after a doppel's review, an approval or a block, so PRs no Mac reviewed get nothing.
- **When it runs:** after each posted or unposted verdict, after PR events, after a slot finally fails or expires, on `check_suite` completed, and every 10 minutes for PRs it is waiting on (the 2-hour clock, CI). An advisory lock per PR and per slot means nothing is posted twice.
- **Shown** on the PR's page: the merger's decision and reason, and each review's GitHub link or why it wasn't posted.
- **Not yet:** dispositions (a person closing a finding) and commands (checkpoint 6); Doppelwerk's other approval guards (RC's review #2).

Checkpoint 4 in detail (migration `20261124180000_slot_results`; BFF `server/slots/jobs.ts`; runtime `jobs.py`):

- **Claim.** At each check-in, the Mac's room is its reported capacity, capped by the BFF's `host_cap`, and 0 while paused. It takes queued slots that are settled, unexpired and on the PR's current head, whose payer has a seat on this Mac that reported `connected`, at most 2 running per payer across Macs. Slots whose doppel was removed or switched off are cancelled there.
- **The job** carries:
  - the repo, PR, head and base;
  - a one-repository `contents:read` token, or null where the App can't sign (mirrors, tests);
  - the payer `<workspace>/<member>`;
  - the doppel's files;
  - instructions made from its SKILL.md, its scopes.yaml and the workspace's instructions.
- **Leases.** Leases are 15 minutes, renewed by each check-in that lists the job. A job the Mac stops listing is lost (`mac_lost`), and a lease no check-in renews lapses (`lease_lapsed`, worker). Either way it's tried again after 30 s with a fresh 2 hours, up to 3 attempts, then `failed`. A removed Mac's jobs go back at once without spending an attempt. A job whose slot was cancelled (the PR moved on) comes back in `stop`.
- **Result.** `POST /api/runtime/jobs/:id` takes a verdict, which is checked against the parts of the schema the merger relies on and kept, or a failure, which retries. Only the Mac holding the job can finish it, and only once (otherwise 409).
- **Runtime.** It sends results before each check-in, so a finished job never looks lost. A job runs as a local ledger slot tied to its BFF id, so the gateway's payer lookup and token count are unchanged. The PR's pod is shared by its jobs and deleted after the last one. The doppel's files go to the job folder's `doppel/`. A dead process's jobs are dropped, and the BFF retries them.
- **Proof** (local, everything real but the model): signed webhook → slot → claim → Docker pod running the Codex harness through the gateway (1,100 tokens counted to the payer) → verdict accepted → the PR page shows it reviewed on that Mac, in 5 s.
- **Not yet:** posting verdicts to GitHub, and the merger (checkpoint 5); plan and quota in check-ins (sent as null).

Checkpoint 3 in detail (migration `20261124140000_review_slots`, BFF `server/slots/`):

- **Where events come from.** Signed `pull_request` webhooks, plus a sync of each enrolled repository's recently updated PRs every 10 minutes for deliveries GitHub never made (D11). Both go through one planner that compares the saved PR with GitHub's, under a row lock, so an event read twice changes nothing and a delivery older than the saved PR is ignored (except an un-request).
- **Slots.** `review_slots` holds one row per (PR, head, doppel) (D2), with its payer (D3): a personal doppel's person, found by their linked GitHub login, or an app doppel's named payer. No payer means `waiting_on = no_payer`. `pull_request_doppels` records which doppels are on each PR.
- **Triggers (D4).** App doppels: `opened`, `push` and `all` each review opened, reopened and marked-ready PRs. On a new head, `push` reviews it only when it extends the last head (GitHub's compare says `ahead`; asked only for `push` workspaces; not known counts as extending, a vanished old head as rewritten), and `all` reviews any new head, force pushes included (restored 2026-11-24, after v68 had merged the two). They run only on insiders' PRs (OWNER, MEMBER, COLLABORATOR) that pass the workspace's filters. Path filters wait until a PR's files are known. A repository's ownership map, when it names doppels, limits its app doppels to those. Personal doppels review when their person is requested, whatever the event setting, and every new head after that. A person's own review keeps their doppel (`pull_request_review`); only an un-request removes it.
- **Timing.** Every trigger sets a 90 s settle. A new head cancels the old head's slots (`head_moved`). Drafts wait unless the workspace reviews drafts. Closing or merging cancels everything. A sync meeting a PR opened over an hour ago records it without reviewing it, so connecting a repository doesn't review its backlog. The worker expires slots no Mac took within 2 hours (`unclaimed`), checking at least every 5 minutes.
- **Shown** on the PR's page: each doppel's review of each head, its state and who pays.
- **Not yet:** path-level ownership routing (needs the PR's files when the review is planned); cancelling queued slots when a doppel is removed from the workspace (checkpoint 4's claim skips them); commands (checkpoint 6).

Checkpoint 1 in detail (migration `20261124070000_runtime_only`):

- **Doppels are rows.** `doppels` holds organization, handle, kind, login, payer, enabled, config, files and position. A `gh_login` in doppel.yaml makes a doppel personal; a login names at most one doppel per workspace, checked at commit. The migration maps the 17 original doppels to 13 personal and 4 app doppels; a duplicate or invalid login becomes an app doppel with its files kept.
- **Settings lose dead keys.** The companion's and the single-review approval's keys go. `ci` → `push`, `requested` → `never`, and the old bot toggle becomes the `*[bot]` author filter.
- **Dropped:** the companion's tables, `reviewer_profiles` and the ChatGPT/Grok link tables. Pending publication jobs are marked done, not deleted, and the worker prunes them after 30 days.
- **New and changed routes:** `GET …/members` (members with their linked GitHub logins) and `GET …/pulls` (PR metadata). The webhook keeps PR metadata only.
- **Release gate:** the migration drops tables, so the drops are declared in its header and an approver approved the release in the console.

Members' GitHub logins come from the existing GitHub connection; nothing else is stored for them.

## 6. Running the BFF locally

`agc up` needs this machine's agent.cloud identity, which has expired. Reconnecting it is a person's step: run `agc init --reset-identity`, then connect it in the console. Until then, the BFF runs on its own:

```sh
var/doppelwerk-local/start.sh        # in this repo; var/ is gitignored
```

It starts a private Postgres on port 5544 and applies any of their migrations not yet applied (`migrate.sh`), so a `git pull` there only needs a restart. It sets edge sign-in to one dev user (`steven@local.test`, or `DEV_USER=…`). It serves at http://localhost:8792. GitHub is not connected: no App secret is set, so the UI shows that setup is required.
