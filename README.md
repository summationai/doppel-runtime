# doppel-runtime

The runtime behind Doppel as a Service: it reviews a tenant's GitHub PRs with each user's own Codex sign-in, in gVisor sandboxes, and posts as the doppelwerk GitHub App.

Status: design only. Nothing is built or run yet.

## What it does

One process with five roles:

| Role | Job |
| --- | --- |
| Intake | GitHub events in (webhooks via the BFF, plus a reconcile poll) |
| Scheduler | Matches each reviewer slot to its PR's pod once its conditions hold (settle, payer cap, usage window, host cap); runs each PR's merger, the only thing that approves |
| Broker | Keeps a Codex home per user at `<data_dir>/tenants/<tenant>/<user>/codex/`; refreshes under a per-user lock |
| Gateway | Swaps the assigned user's token into model calls; the only GitHub writer |
| Sandbox manager | One gVisor pod per PR, with a read-only checkout per head from a mirror per repo |

Pods never hold a token or reach GitHub. Records (tenants, users, apps, rules) live in the BFF; secrets, sandboxes and GitHub writes live here.

## Layout

One repo, a uv workspace with one package per component. `src/` is the architecture:

```
src/
  control-plane/    doppel_control_plane    the runtime process: intake, scheduler, merger, ledger, app, cli
    doppel_control_plane/core/     pure decisions: command parsing, payer choice, slot matching, the merger
    doppel_control_plane/store/    the ledger and its schema
  gateway/          doppel_gateway          the one door out of a pod: the model route; later GitHub posting
  model-provider/   doppel_model_provider   the broker: each payer's sign-in and refresh
  sandbox-runtime/  doppel_sandbox          what runs inside a pod: harness and image (checkpoint 5)
  shared/           doppel_shared           contracts between them: types, ports, fakes for tests
tests/              cross-component: the architecture test, Docker integration tests
```

| Component | May import |
| --- | --- |
| `shared` | nothing of ours |
| `gateway`, `model-provider`, `sandbox-runtime` | `shared` |
| `control-plane` | `shared`, `gateway`, `model-provider` (it runs them in-process) |

Inside the control plane, `core` and `store` import only `shared`. Components talk through ports in `shared`: the gateway sees runs through `Runs` and sign-ins through `Credentials`, never the ledger or the broker's files. [tests/test_architecture.py](tests/test_architecture.py) enforces all of it, including that each component's pyproject declares what it imports. Each component keeps its own tests in `src/<component>/tests/`.

## Develop

```sh
uv sync                       # Python 3.12+, every component, dev tools
uv run pytest -q              # tests
uv run ruff check . && uv run ruff format --check src tests scripts
DOPPEL_DOCKER_TESTS=1 uv run pytest tests/integration   # needs Docker
uv run doppel-runtime serve   # listeners on 127.0.0.1:8700 (model), 8701 (internal), 8702 (health)
```

Config is a TOML file (`--config`) with `DOPPEL_*` environment overrides; see [src/control-plane/doppel_control_plane/config.py](src/control-plane/doppel_control_plane/config.py). Progress is tracked in [docs/specs/checkpoints.md](docs/specs/checkpoints.md).

## Docs

- [docs/specs/doppel-service.md](docs/specs/doppel-service.md): the spec. Decisions, scheduler, sandbox, credentials, rules (DSV-1 to DSV-14), the Friday demo and open questions.

Specs in `docs/specs/` are the source of truth. Shared docs elsewhere are handoff copies.

## Guardrails

- Never touch the production Doppelwerk on summini (user `rc`).
- Ask Steven before installing anything on summini.
- Never print, log or commit a token, `auth.json` or a private key.
- No Claude in the POC.
- Claim a step done only after a run shows it working.
