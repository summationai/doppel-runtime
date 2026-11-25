# Next: CI rules, one kind of doppel, and the requester pays

Status: plan, 2026-11-25. Items 1–3 are Steven's; each says how it works today, the change, and what to settle before
building. Items 2 and 3 are **decided** (2026-11-25, below) and replace D3 and D4 in [bff-split.md](bff-split.md).
Item 1 is still open. "Also open" collects what else was left on 2026-11-24.

## 1. CI rules

**Today.** The merger approves only when CI is green, waits while it runs, and blocks when it fails
(`server/reviews/merger.ts`). It reads CI as the head commit's **check runs** only (`ciState` in
`server/github/reviews.ts`), and when it can't read them it says "waiting on CI".

**What's wrong** (found on liustev6/steven-universe #4):

- Doppelcloud has no Checks permission, so the read fails and every PR "waits on CI" forever.
- Vercel, like many integrations, reports **commit statuses**, not check runs. On #4's head a failed status
  (`Vercel – my-personal-website2-0`) was invisible; the merger would have called CI green.
- An unrelated integration can fail: `my-personal-website2-0` is another Vercel project on the same repo.

**Change.**

- Read both: check runs and the combined commit status. Any failure is red; anything unfinished is pending.
- Which checks count: the base branch's **required** checks when branch protection names some; otherwise all of them.
- Say why when CI can't be read ("can't read CI: Doppelcloud lacks the Checks permission"), never "waiting on CI".
- Hear about changes: the `check_suite` and `status` events (see the app settings under "Also open").

**Settle first.** Only required checks when there are some, all otherwise: agreed? Should a workspace be able to list
checks to ignore (an unrelated deploy)? How long does the merger wait for CI before saying it's stuck?

## 2. One kind of doppel

**Today.** Two kinds, decided by whether `doppel.yaml` names a GitHub login (D3, D4):

- **App doppel** (no login): reviews automatically on the workspace's events, for routed repos; a named member pays;
  the merger needs one before it can approve.
- **Personal doppel** (a login): reviews only when its person is requested; its person pays; routing doesn't apply;
  it can't review its person's own PRs (nobody can request themselves).

This confused its first user: `steven-doppel` had a login, so it was personal and never reviewed Steven's own PRs,
while routing pointed at it and the UI never said why.

**Change.** One kind. Every doppel:

- reviews automatically on the workspace's events, for the repos and paths routing gives it;
- may also name a person (the login), which adds: it reviews when that person is requested, in their voice;
- counts toward the merger like any other doppel.

**Settle first.** Is a doppel that names a person also automatic, or only when routing names it? Who pays is item 3.
Does the merger still need "at least one automatic review" before approving, or does any doppel's review count? The
data: drop `doppels.kind` (keep `login`), and a migration for existing personal doppels. The roster's 13 personal doppels
would start reviewing automatically wherever routing names them: check routing first.

**Decided (2026-11-25).** One kind. A doppel has no person: the GitHub username and "reviews when its person is
requested" are gone, and so are `pull_request_review` handling and un-requests. Every doppel reviews on the workspace's
events, in the repositories it belongs to (`any_repo` / `repos`), as routing narrows it, and on commands. The merger
waits for every doppel on the PR. Migration `20261125040000_one_kind_of_doppel`: former personal doppels come over
belonging to **no repository** (so nothing starts reviewing or spending until someone picks repositories), without their
login; `kind`, `login` and `payer_id` stay as unused columns for a later release to drop.

## 3. The requester pays, and any available doppel reviews

**Today.** Each doppel has one payer: an app doppel's named payer, a personal doppel's person (D3). A review waits
("Waiting: nobody can pay") when that one payer has no seat on any runner, and it runs only on a runner where that
payer is seated.

**Change.** Pay with the requester's own seat instead of a fixed payer per doppel, and let any doppel that fits run:

- **Who pays:** the PR's author, when they're a member with a seat; for an `@doppelwerk review` command, the commenter;
  for a person's requested review, that person. A doppel's own payer becomes a fallback (or goes away).
- **Which doppel:** any routed doppel can review on the requester's seat; there's no longer a doppel ↔ payer binding.
- **Which runner:** any runner where the requester has a seat (today's claim rule, keyed by the new payer).

**Settle first.** "Requester" for an automatic review: the PR's author? (Someone outside the workspace opens a PR:
who pays then: a fallback, or nobody?) The per-payer cap (2 at once) now applies to the requester: still 2? Spending:
does a member need to agree that their seat pays for every PR they open (a setting per member, on by default)? The
data: `review_slots.payer_id` chosen at queue time from the requester, not the doppel; the claim and cap code in
`server/slots/jobs.ts`; the Runners page's "who pays" wording.

**Decided (2026-11-25).** The requester pays: the PR's author for automatic reviews, the commenter for a command. They
can pay when their GitHub login is linked to a member of the workspace with a seat on one of its runners. When they
can't, a workspace setting (Settings → Who pays) decides:

- **Skip the review** (default): the slots are kept as `cancelled`, reason `requester_cannot_pay`, so the PR says why; a
  command is refused with a reply saying how to get a seat. The merger doesn't approve around a skipped review.
- **Use a member's seat**: one chosen member pays (they need a seat too, or the reviews wait and expire).

A doppel has no payer. The per-payer cap (2 at once) now applies to the requester. Known edges, left for later: a review
skipped on a head isn't re-queued when the author later gets a seat (the next push, or a command, reviews it); a
member's own automatic reviews can hold their command behind the cap.

## Also open (from 2026-11-24)

**Doppelcloud app settings** (GitHub → Developer settings → GitHub Apps → Doppelcloud; then each installation accepts):

- Permissions: add **Checks: Read** and **Commit statuses: Read** (it has pull requests write, contents read, actions
  read, members read, metadata read).
- Events: add **Issue comment**, **Pull request review**, **Pull request review thread**, **Check suite**, **Status**
  (it has only pull request and workflow run). Without them: commands, resolving a finding on GitHub, and CI-aware
  merging don't work in production.

**BFF (one release with items 1–3, or before them):**

- 👀 on an `@doppelwerk` command comment when it's recorded, and on the PR when a runner takes its first review, as
  the original Doppelwerk did (`poster.py`: `react_comment`, `react_watching`); only where the repo posts.
- A release check that compares the live app's events and permissions (`api.github.com/apps/doppelcloud`) with what
  the BFF needs; `github-app-manifest.json` brought up to date.
- A PR with no doppel attached says why: no automatic doppel in the workspace, routing names none, the PR opened before
  Doppelwerk was watching, the author isn't an insider, a filter excluded it.

**doppel-runtime (next release, 0.2.1):**

- `doctor`: "you're in the docker group but this shell isn't: open a new one or run `newgrp docker`".
- `pair` / `update`: a private image says so ("make the packages public, or `docker login ghcr.io`"), not "unauthorized".
- `--instance NAME` for two runners on one Docker (two workspaces on one machine).
- `doctor` warns when Docker won't start after a reboot without someone logging in (a Mac).

**Steven:**

- Make `doppel-runtime` and `doppel-sandbox` public on ghcr.io (needs an org owner to allow public packages).
- Fix `HOMEBREW_TAP_TOKEN` (Contents: Read and write on summationai/homebrew-tap; approved by the org if pending).
- The mini: pair the VM (`doppel-runtime pair <code> --bff https://doppelwerk.ac0.app`), and a system startup service
  so the VM comes back after a reboot (one `sudo`). Remove the old offline "Sum MacMini" in Runners.
- `steven-doppel`: after the one-kind release it belongs to no repository: pick its repositories, and take a seat on a
  runner (or set Settings → Who pays), then push to #4 or comment `@doppelwerk review`.
