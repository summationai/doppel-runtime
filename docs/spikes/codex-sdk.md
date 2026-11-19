# Spike: running several reviewers with the Codex SDK

Checkpoint 0, spikes 0a to 0d. Run on 2026-11-19 with `openai-codex==0.167.2` (the version steven-universe pins), against a fake gateway, with no calls to OpenAI.

Reproduce: `uv run --no-project --python 3.12 --with openai-codex==0.167.2 --with aiohttp python spikes/codex_sdk/spike.py`

## Answers

| Spike | Question | Answer |
| --- | --- | --- |
| 0a | Can threads in one Codex process send different capabilities? | **No.** One process has one current provider, and a thread on any other provider is refused at `turn/start`. Run **one Codex process per reviewer run**, each with its own capability in its environment. |
| 0b | Can one running reviewer be stopped while others carry on? | **Yes.** `AsyncTurnHandle.interrupt()` ends the turn (`interrupted`) at once, and the gateway sees the connection close. The other reviewer completes, in the same process or another. |
| 0c | Does the checkout's `AGENTS.md` or `.codex/config.toml` reach the model? | **No, with `project_doc_max_bytes = 0`.** The control run without it does send `AGENTS.md`. The repo's `.codex/config.toml` didn't load in either run. |
| 0d | How long does the sign-in's access token last? | **10 days.** The `id_token` lasts 1 hour; the spec had them swapped. A 6-hour proactive refresh, plus one refresh and retry on an upstream 401, keeps the refresh token in use. |

## Evidence

### 0a

| Variant | Result |
| --- | --- |
| Two providers set at start, one env var each, chosen per thread | `This chat uses gw_b, but your current provider is gw_a.` |
| A provider defined in `thread_start(config=)` with `experimental_bearer_token` | `This chat uses gw_c, but your current provider is openai.` (nested and dotted keys alike) |
| Two processes, one capability each, run at once | Both `completed`; the gateway saw `Bearer cap-alice` and `Bearer cap-bob` |

Codex also sends `thread-id`, `session-id` and `x-codex-turn-metadata` (with `thread_id` and `turn_id`) on every request. A gateway could tell threads apart by them, but a header the pod chooses is no better than the capability it already holds. One process per reviewer keeps a capability tied to one run.

### 0b

```json
{"slow_status": "interrupted", "slow_ended_after_s": 2.2, "fast_status": "completed",
 "gateway_saw_disconnect": ["Bearer cap-alice"]}
```

`interrupt()` was sent 2.2 s after the slow turn started, and the turn ended in that same tenth of a second. Same result in one process and across two.

### 0c

```json
{"0c_doc_off": {"agents_canary": false, "config_canary": false},
 "0c_control_doc_on": {"agents_canary": true, "config_canary": false}}
```

### 0d

From the local Codex sign-in, decoding only the JWTs' `iat` and `exp`: access token 240 h, `id_token` 1 h. steven-universe's broker refreshes once `last_refresh` is 6 h old and on an upstream 401 (`app/gateway/model.py`), with callers that waited behind a refresh reusing it.

## What changes in the design

- The harness starts **one Codex process per reviewer run** and stops it when the run ends. An idle pod then runs no Codex process, which answers open question 4 by construction. Each process's memory is still to be measured in checkpoint 5.
- Stopping a reviewer is `interrupt()`, then closing its process.
- Codex's own sandbox reported `seatbelt` on macOS. Inside a gVisor pod, check in checkpoint 5 whether Codex's Linux sandbox works there or must be off (steven-universe runs `Sandbox.full_access` and relies on the container).
- Every reviewer process in one pod runs as the same user, so one can read another's environment through `/proc`. The spec's risk ("reviewers in one pod can see each other's capabilities") stands. Running each reviewer as its own uid would close it; not needed for the POC.
