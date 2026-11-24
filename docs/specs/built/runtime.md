# Runtime

A runner for Doppelwerk: one process with four roles (broker, gateway, sandbox manager, BFF link). It listens on three sockets, keeps the jobs in hand in one SQLite ledger, and recovers from a crash on the next start. Doppelwerk decides what to review and who pays, posts and approves; this process checks in, runs the jobs it's given, and returns the verdicts ([bff-split.md](../bff-split.md)). Code: [app.py](../../../src/control-plane/doppel_control_plane/app.py), [roles.py](../../../src/control-plane/doppel_control_plane/roles.py), [config.py](../../../src/control-plane/doppel_control_plane/config.py), [bff.py](../../../src/control-plane/doppel_control_plane/bff.py), [jobs.py](../../../src/control-plane/doppel_control_plane/jobs.py), [store/](../../../src/control-plane/doppel_control_plane/store/).

## Supports

- **`doppel-runtime serve [--config file.toml]`:** a TOML config file, with `DOPPEL_*` environment variables on top. Also `pair`, `seat add`, `seat sign-in` and `status` (see the README).
- **Three listeners:**

  | Listener | Default | Serves | Who reaches it |
  | --- | --- | --- | --- |
  | `model` | 127.0.0.1:8700 | The gateway's model route, `/healthz` | Pods (through the egress forwarder) |
  | `internal` | 127.0.0.1:8701 | Broker sign-in routes, seats and status for the CLI, `/healthz` | This machine's CLI |
  | `health` | 127.0.0.1:8702 | `/healthz` | Monitoring |

- **Roles:** all four always run. The BFF link checks in every 30 s (or as Doppelwerk says) and waits quietly until the runner is paired.
- **Health:** `/healthz` on every listener shows which roles are running.
- **The ledger:** SQLite in WAL mode at `<data_dir>/ledger.db`, migrated forward on start:

  | Version | Adds |
  | --- | --- |
  | 1 | PRs, reviewers, slots, merges, CI, usage resets, commands, findings, posts, dispositions |
  | 2 | Tokens used and token budget per slot |
  | 3 | The provider's error status, code and type per slot |
  | 4 | Intake's comment cursor per repo; `person_reviewed` on requested reviewers |
  | 5 | The PR's base branch |
  | 6 | A slot's Doppelwerk job id, and its result until Doppelwerk accepts it |
  | 7 | One path: drops what Doppelwerk now keeps (reviewers, merges, CI, commands, findings, posts, dispositions, intake cursors) |

## Locked decisions

| Decision | Value | Why | Since | Proven by |
| --- | --- | --- | --- | --- |
| Pods reach only the model listener | Broker and seat routes are on `internal`; the model listener answers 404 for them | A pod must not reach sign-ins (DSV-8) | 2026-11-19 | test_serve.py::test_dsv_8_sign_in_routes_are_on_the_internal_listener_only; integration/test_dsv8_network.py |
| One path | The runtime only runs jobs Doppelwerk hands it; no intake, scheduler, merger, commands or GitHub App of its own | Doppelwerk orchestrates (decided 2026-11-23) | 2026-11-24 (BFF checkpoint 7) | test_serve.py::test_serve_answers_health_on_every_listener |
| A job left running by a crash is dropped | Marked failed at start; Doppelwerk sees it missing from the next check-in and tries it again | Its pod died with the process | 2026-11-24 | test_serve.py::test_a_job_left_running_by_a_crash_is_dropped_for_doppelwerk_to_retry |
| Pods a dead process left are removed at start | A paired runner prepares its sandbox at start; containers and networks labelled with this runtime's owner are swept, another runtime's untouched | The new process can't drive them, and they hold resources | 2026-11-20 (#12) | integration/test_runtime_docker.py::test_a_restart_sweeps_the_pods_a_dead_process_left |
| A failed round never stops a role | Logged; the next round runs on time | One bad answer from Doppelwerk mustn't stop the runner | 2026-11-20 (#12) | not tested directly |
| No secret in the config file | The pairing credential and sign-ins are owner-only files under `data_dir` | The config is shared and copied | 2026-11-24 | test_bff.py::test_pairing_keeps_only_an_owner_only_credential_this_mac_made |
| Components import only what they may | shared ← gateway, model-provider, sandbox-runtime ← control-plane; `store` stays pure | Each part can be split out or tested alone | 2026-11-19 | test_architecture.py |

## Not yet

- Running on summini under gVisor (Troy's Linux runner work, PR #19).
- Docker calls that block the event loop briefly. Fine at today's scale.
