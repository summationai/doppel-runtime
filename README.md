# doppel-runtime

A runner for Doppelwerk: install it on a Mac (or Linux), pair it with a Doppelwerk workspace, and each member adds a seat with their own Codex sign-in. Doppelwerk decides what gets reviewed, by which doppel and on whose plan, posts the reviews as its GitHub App, and approves. This runner checks in, runs each review in a sandbox pod on the payer's own sign-in, and returns the verdict. Sign-ins never leave it. See [docs/specs/bff-split.md](docs/specs/bff-split.md).

## What it does

One process with four roles:

| Role | Job |
| --- | --- |
| Broker | Keeps a Codex home per seat at `<data_dir>/tenants/<workspace>/<member>/codex/`; refreshes under a per-seat lock |
| Gateway | The model route pods call: swaps a run's capability for its payer's sign-in, and counts tokens |
| Sandbox manager | One pod per PR (gVisor where installed), with a read-only checkout per head from a mirror per repo |
| BFF link | Check-ins with Doppelwerk: seats' states and the jobs in hand out; jobs, stops and control in; results back |

Pods never hold a token or reach GitHub. A job brings a one-repository, read-only token for the mirror's fetch only. Doppelwerk keeps everything else: workspaces, doppels and their rules, PRs, reviews, findings, the merger, the GitHub App.

## Layout

One repo, a uv workspace with one package per component. `src/` is the architecture:

```
src/
  control-plane/    doppel_control_plane    the runtime process: the BFF link, jobs, pods, mirrors, ledger, app, cli
    doppel_control_plane/store/    the ledger and its schema: the jobs in hand
  gateway/          doppel_gateway          the one door out of a pod: the model route
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

Inside the control plane, `store` imports only `shared`. Components talk through ports in `shared`: the gateway sees runs through `Runs` and sign-ins through `Credentials`, never the ledger or the broker's files. [tests/test_architecture.py](tests/test_architecture.py) enforces all of it, including that each component's pyproject declares what it imports. Each component keeps its own tests in `src/<component>/tests/`.

## Develop

```sh
uv sync                       # Python 3.12+, every component, dev tools
uv run pytest -q              # tests
uv run ruff check . && uv run ruff format --check src tests scripts
DOPPEL_DOCKER_TESTS=1 uv run pytest tests/integration   # needs Docker
uv run doppel-runtime serve   # listeners on 127.0.0.1:8700 (model), 8701 (internal), 8702 (health)
```

Config is a TOML file (`--config`) with `DOPPEL_*` environment overrides; see [src/control-plane/doppel_control_plane/config.py](src/control-plane/doppel_control_plane/config.py). It holds only what this machine needs (data folder, listeners, sandbox, host cap). Progress is tracked in [docs/specs/bff-split.md](docs/specs/bff-split.md) §5.

## Onboarding a Mac

Doppelwerk's BFF orchestrates; this Mac runs reviews and keeps every sign-in ([docs/specs/bff-split.md](docs/specs/bff-split.md) §4b). The runtime only calls out to the BFF.

```sh
doppel-runtime pair dwp_… --bff https://doppelwerk.ac0.app   # code: Doppelwerk → Settings → Macs → Add a Mac
doppel-runtime serve                                         # checks in every 30 s
doppel-runtime seat add dws_…    # a member's code (Settings → Macs → Add my seat); they approve Codex's sign-in in their browser
doppel-runtime status            # which workspace, and each seat's state
```

Reviews arrive with check-ins: each job (the PR, a one-repository read token, and the doppel's persona, scope and files) runs in a Docker pod (`[sandbox] image`, gVisor where `runtime = "runsc"`), on the payer's own sign-in through the gateway, and its verdict goes back to Doppelwerk. This Mac keeps no review rules.

The pairing lives in `<data_dir>/bff/` (owner-only): the Mac's own credential, which Mac it is, and its seats. Each seat's Codex sign-in lives in the broker's home for payer `<workspace id>/<member id>`. A seat removed in Doppelwerk is signed out at the next check-in; a removed Mac signs every seat out and forgets its credential.

## Docs

- [docs/specs/built/](docs/specs/built/): what the runtime does today, one file per feature, with its locked decisions and the tests that hold them.
- [docs/specs/doppel-service.md](docs/specs/doppel-service.md): the spec. Decisions, scheduler, sandbox, credentials, rules (DSV-1 to DSV-14), the Friday demo and open questions.
- [docs/reviews/](docs/reviews/): review findings and what we decided to do with each.
- [docs/github-app.md](docs/github-app.md): creating the GitHub App, connecting the runtime to it, and checking it.

Specs in `docs/specs/` are the source of truth. Shared docs elsewhere are handoff copies.

## Guardrails

- Never touch the production Doppelwerk on summini (user `rc`).
- Ask Steven before installing anything on summini.
- Never print, log or commit a token, `auth.json` or a private key.
- No Claude in the POC.
- Claim a step done only after a run shows it working.
