# Runtime

One process with five roles: intake, scheduler, broker, gateway, sandbox manager. It listens on three sockets, keeps its state in one SQLite ledger, and recovers from a crash on the next start. Code: [app.py](../../../src/control-plane/doppel_control_plane/app.py), [roles.py](../../../src/control-plane/doppel_control_plane/roles.py), [config.py](../../../src/control-plane/doppel_control_plane/config.py), [store/](../../../src/control-plane/doppel_control_plane/store/).

## Supports

- **`doppel-runtime serve [--config file.toml]`:** a TOML config file, with `DOPPEL_*` environment variables on top.
- **Three listeners:**

  | Listener | Default | Serves | Who reaches it |
  | --- | --- | --- | --- |
  | `model` | 127.0.0.1:8700 | The gateway's model route, `/healthz` | Pods (through the egress forwarder) |
  | `internal` | 127.0.0.1:8701 | Broker sign-in routes, `/github/webhook`, `/healthz` | The BFF, over the tailnet |
  | `health` | 127.0.0.1:8702 | `/healthz` | Monitoring |

- **Roles:**
  - The broker and gateway always run.
  - Intake, the scheduler and the sandbox run when `github.mode = "app"`, or when a caller passes in the GitHub ports (tests, the local demo).
  - A role with work runs it on a timer: the scheduler every 5 s, intake every 60 s.
- **Health:** `/healthz` on every listener shows which roles are running.
- **The directory:** tenants, repos and reviewers come from the config file until the BFF's records arrive. See [directory.py](../../../src/control-plane/doppel_control_plane/directory.py).
- **The ledger:** SQLite in WAL mode at `<data_dir>/ledger.db`, migrated forward on start:

  | Version | Adds |
  | --- | --- |
  | 1 | PRs, reviewers, slots, merges, CI, usage resets, commands, findings, posts, dispositions |
  | 2 | Tokens used and token budget per slot |
  | 3 | The provider's error status, code and type per slot |
  | 4 | Intake's comment cursor per repo; `person_reviewed` on requested reviewers |
  | 5 | The PR's base branch |

## Locked decisions

| Decision | Value | Why | Since | Proven by |
| --- | --- | --- | --- | --- |
| Pods reach only the model listener | Broker and webhook routes are on `internal`; the model listener answers 404 for them | A pod must not reach sign-ins or GitHub (DSV-8) | 2026-11-19 | test_serve.py::test_dsv_8_sign_in_routes_are_on_the_internal_listener_only; test_runtime_reviews.py::test_app_mode_takes_a_signed_webhook_on_the_internal_listener_only; integration/test_dsv8_network.py |
| With GitHub off, only the broker and gateway run | `github.mode = "off"` is the default | Nothing reviews until GitHub is configured | 2026-11-20 (#12) | test_serve.py::test_serve_answers_health_on_every_listener |
| Running slots go back to the queue at start | Every slot left `running` becomes `queued` | A crash must not lose a review or leave it stuck | 2026-11-19 | test_serve.py::test_a_slot_left_running_by_a_crash_is_queued_again |
| Pods a dead process left are removed at start | Containers and networks labelled with this runtime's owner (a hash of its data dir) are swept; another runtime's are untouched | The new process can't drive them, and they hold resources | 2026-11-20 (#12) | integration/test_runtime_docker.py::test_a_restart_sweeps_the_pods_a_dead_process_left |
| A failed round never stops a role | Logged; the next round runs on time | One bad GitHub answer mustn't stop reviews | 2026-11-20 (#12) | not tested directly |
| No secret in the config file | Secrets are files the config points to (App key, webhook secret), each refused unless chmod 600 | The config is shared and copied | 2026-11-20 (#13) | test_runtime_reviews.py::test_app_mode_refuses_a_key_others_can_read_and_a_missing_installation |
| Components import only what they may | shared ← gateway, model-provider, sandbox-runtime ← control-plane; `core` and `store` stay pure | Each part can be split out or tested alone | 2026-11-19 | test_architecture.py |

## Not yet

- Records from the BFF instead of the config file (checkpoint 8).
- Running on summini under gVisor (checkpoint 7).
- Scheduler, intake and sandbox calls that block the event loop briefly (Docker, posting). Fine at today's scale.
