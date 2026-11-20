# Sandbox

One pod (a Docker container) per PR, living as long as the PR. Each reviewer run is one Codex process in it, under its own Unix user, with only a capability. The runtime fetches code into a mirror and hands the pod a read-only checkout per commit. The pod never touches GitHub. Code: [pods.py](../../../src/control-plane/doppel_control_plane/pods.py), [mirrors.py](../../../src/control-plane/doppel_control_plane/mirrors.py), [Dockerfile](../../../src/sandbox-runtime/Dockerfile), [review.py](../../../src/sandbox-runtime/doppel_sandbox/review.py).

## Supports

- **The image** (`doppel-sandbox`): Debian slim, Python 3.12, git, ripgrep, jq; the pinned Codex SDK (`openai-codex==0.167.2`) and its binary. Users `pod` (10000) and `reviewer1`–`reviewer16`.
- **A pod per PR:**
  - its own `--internal` Docker network, with no route out;
  - an egress forwarder joined to that network as `gateway`, the only way out, which forwards only to the runtime's model listener;
  - the pod idles as `pod`.
- **Per commit:**
  - the mirror fetches the PR's head;
  - `git archive` writes the commit's files to `/work/<sha>`, with no `.git`;
  - `/job/<slot>/` gets `job.json` and `diff.patch`, the diff against the PR's base branch.
- **Per reviewer run:** `docker exec --user <its own uid>` runs the harness. The harness starts Codex with the gateway as model provider, the capability in its environment, and a prompt asking for one JSON verdict. It checks the answer against the schema and prints it.
- **Several commits and reviewers in one pod:** a second commit appears under `/work` without restarting the pod; one reviewer can be stopped while others run.

## Locked decisions

| Decision | Value | Why | Since | Proven by |
| --- | --- | --- | --- | --- |
| One pod per PR | Created at the PR's first start, kept across pushes, deleted on merge/close or after 3 idle days | One place per PR; cheaper than a pod per commit | 2026-11-19 | test_scheduler.py::test_dsv_10_a_push_stops_old_reviewers_and_queues_the_new_head_in_the_same_pod; integration/test_sandbox.py::test_a_new_head_appears_without_restarting_the_pod |
| The pod reaches only the gateway | `--internal` network plus a one-target forwarder; github.com and raw IPs fail | DSV-6, DSV-8 | 2026-11-19 | integration/test_sandbox.py::test_dsv_6_7_the_pod_reaches_nothing_holds_nothing_and_runs_nothing; integration/test_dsv8_network.py |
| Hardened container | `--read-only` root, `--cap-drop ALL`, `no-new-privileges`, `--init`, 2 GB memory, 1 CPU, 512 processes, 512 MB `/tmp`; `/work` and `/job` read-only | Nothing from the PR can change the pod | 2026-11-20 (#10) | integration/test_sandbox.py::test_dsv_6_7_the_pod_reaches_nothing_holds_nothing_and_runs_nothing |
| Nothing from the PR runs | No compilers, package managers, `gh` or credentials in the image; the prompt forbids running anything | DSV-7 | 2026-11-20 (#10) | integration/test_sandbox.py::test_dsv_6_7_the_pod_reaches_nothing_holds_nothing_and_runs_nothing (a `postinstall` canary never fires) |
| Each run its own Unix user | From a pool of 16 per pod; the capability only in that process's environment, passed by name (never on a command line) | One reviewer can't read another's capability (RC's review #5) | 2026-11-20 (#10) | integration/test_sandbox.py::test_review_5_one_reviewer_cant_read_anothers_capability_and_can_be_stopped |
| Codex's own sandbox is off | `Sandbox.full_access` inside the pod | bwrap can't create namespaces in a pod with no capabilities; the pod is the boundary | 2026-11-20 (#10) | integration/test_sandbox.py::test_a_pod_reviews_a_head_and_returns_a_valid_verdict |
| The commit's `AGENTS.md` and `.codex` never reach the model | `project_doc_max_bytes = 0` | The PR author mustn't instruct the reviewer (DSV-9) | 2026-11-19 | integration/test_sandbox.py::test_a_pod_reviews_a_head_and_returns_a_valid_verdict (canary) |
| One correction, then fail | A verdict that fails the schema gets one "answer again" turn; still wrong → exit 2, and the scheduler counts a failed attempt | The model sometimes adds prose | 2026-11-20 (#10) | not tested (the scheduler's own check is: see scheduler.md) |
| The runner stamps who reviewed what | `doppel`, `pr`, `head_sha` and `engine` are set by the harness, never taken from the model | The model mustn't claim another reviewer or commit | 2026-11-20 (#10) | test_review.py::test_the_runner_stamps_who_reviewed_what_not_the_model |
| The job file holds no capability | Only in the process's environment | Another user could read the file | 2026-11-20 (#10) | test_review.py::test_a_job_file_holds_no_capability |
| One mirror per repo, one lock per repo | A bare clone (`--filter=blob:none`); everything touching it holds that repo's lock | Reviewers on one PR start together; concurrent fetches collided | 2026-11-20 (#11) | test_mirrors.py::test_reviewers_preparing_at_once_dont_collide |
| The diff is against the PR's base | GitHub's `base.ref`, stored by intake; the repo's default branch only when unknown | A PR into `release/*` must diff against that branch | 2026-11-20 (#13) | test_mirrors.py::test_the_diff_is_against_the_prs_base; test_intake.py::test_the_prs_base_reaches_the_reviewer_job |
| Runtime: runc on laptops, runsc on summini | `[sandbox] runtime = "runsc"` | gVisor for strangers' code | 2026-11-20 (#12) | not tested (checkpoint 7) |

## Not yet

- gVisor (`runsc`) on summini, and checkpoint 5's checks re-run there (checkpoint 7).
- A disk budget for mirrors and checkouts (RC's review #6).
- The reviewer's rules read from the PR's base branch (DSV-9 second half); `instructions` is empty today.
- Doppelwerk's poster formatting.
