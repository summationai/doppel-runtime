# doppel-runtime

The runtime behind Doppel as a Service: it reviews a tenant's GitHub PRs with each user's own Codex sign-in, in gVisor sandboxes, and posts as the doppelwerk GitHub App.

Status: design only. Nothing is built or run yet.

## What it does

One process with five roles:

| Role | Job |
| --- | --- |
| Intake | GitHub events in (webhooks via the BFF, plus a reconcile poll) |
| Scheduler | Matches each reviewer slot to its PR's pod once its conditions hold (settle, payer cap, usage window, host cap); runs each PR's merger, the only thing that approves |
| Broker | Keeps a Codex home per user; refreshes under a per-user lock; durable copy in OpenBao |
| Gateway | Swaps the assigned user's token into model calls; the only GitHub writer |
| Sandbox manager | One gVisor pod per PR, with a read-only checkout per head from a mirror per repo |

Pods never hold a token or reach GitHub. Records (tenants, users, apps, rules) live in the BFF; secrets, sandboxes and GitHub writes live here.

## Layout

```
src/doppel_runtime/
  types/   plain data shared by every layer: dataclasses and enums
  core/    pure decisions: command parsing, payer choice, slot matching, the merger
  ports/   interfaces to the outside (Protocols): directory (BFF), sandbox, GitHub
  store/   persistence: the ledger and its schema
  roles/   the five roles: wire core + store + ports together and handle events
  app.py, cli.py, config.py: the process
tests/     mirrors the layers; tests/fakes.py stands in for the ports
```

`types` imports nothing of ours; `core`, `ports` and `store` import only `types`; `roles` may import all four. [tests/test_layering.py](tests/test_layering.py) enforces it. Run the package with `python -m doppel_runtime` or `doppel-runtime`, never a file inside it: running from inside the package would put `types/` ahead of Python's own `types` module.

## Develop

```sh
uv sync                       # Python 3.12+, deps and dev tools
uv run pytest -q              # tests
uv run ruff check . && uv run ruff format --check src tests
uv run doppel-runtime serve   # listeners on 127.0.0.1:8700 (model), 8701 (internal), 8702 (health)
```

Config is a TOML file (`--config`) with `DOPPEL_*` environment overrides; see [src/doppel_runtime/config.py](src/doppel_runtime/config.py). Progress is tracked in [docs/specs/checkpoints.md](docs/specs/checkpoints.md).

## Docs

- [docs/specs/doppel-service.md](docs/specs/doppel-service.md): the spec. Decisions, scheduler, sandbox, credentials, rules (DSV-1 to DSV-14), the Friday demo and open questions.

Specs in `docs/specs/` are the source of truth. Shared docs elsewhere are handoff copies.

## Guardrails

- Never touch the production Doppelwerk on summini (user `rc`).
- Ask Steven before installing anything on summini.
- Never print, log or commit a token, `auth.json` or a private key.
- No Claude in the POC.
- Claim a step done only after a run shows it working.
