# doppel-runtime

The runtime behind Doppel as a Service: it reviews a tenant's GitHub PRs with each user's own Codex sign-in, in gVisor sandboxes, and posts as the doppelwerk GitHub App.

Status: design only. Nothing is built or run yet.

## What it does

One process with five roles:

| Role | Job |
| --- | --- |
| Intake | GitHub events in (webhooks via the BFF, plus a reconcile poll) |
| Scheduler | Matches each reviewer slot to its PR's pod once its conditions hold: CI, payer cap, usage window, host cap |
| Broker | Keeps a Codex home per user; refreshes under a per-user lock; durable copy in OpenBao |
| Gateway | Swaps the assigned user's token into model calls; the only GitHub writer |
| Sandbox manager | One gVisor pod per PR, with a read-only checkout per head from a mirror per repo |

Pods never hold a token or reach GitHub. Records (tenants, users, apps, rules) live in the BFF; secrets, sandboxes and GitHub writes live here.

## Docs

- [docs/specs/doppel-service.md](docs/specs/doppel-service.md): the spec. Decisions, scheduler, sandbox, credentials, rules (DSV-1 to DSV-14), the Friday demo and open questions.

Specs in `docs/specs/` are the source of truth. Shared docs elsewhere are handoff copies.

## Guardrails

- Never touch the production Doppelwerk on summini (user `rc`).
- Ask Steven before installing anything on summini.
- Never print, log or commit a token, `auth.json` or a private key.
- No Claude in the POC.
- Claim a step done only after a run shows it working.
