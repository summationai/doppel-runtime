# Credentials: sign-ins, capabilities, the model route

Each payer's Codex sign-in lives on the runtime's disk, owned by the **broker** (model-provider). A pod never holds a sign-in: each reviewer run gets a **capability**, a random secret naming that one run. The **gateway's** model route swaps the capability for the payer's credential on the way to OpenAI, and counts the tokens. Code: [broker.py](../../../src/model-provider/doppel_model_provider/broker.py), [codex_homes.py](../../../src/model-provider/doppel_model_provider/codex_homes.py), [codex_account.py](../../../src/model-provider/doppel_model_provider/codex_account.py), [api.py](../../../src/model-provider/doppel_model_provider/api.py), [model_route.py](../../../src/gateway/doppel_gateway/model_route.py), [capability.py](../../../src/shared/doppel_shared/capability.py).

## Supports

- **Payer ids:** `<tenant>/<user>`, e.g. `summation/u-steven`.
- **Sign-in:** device code through the Codex SDK. The BFF shows the code; the person approves it on OpenAI's page.
- **Broker routes**, on the internal listener only:
  - `POST /broker/{tenant}/{user}/login` starts a sign-in and returns the URL and code;
  - `GET …/status` returns `connected`, `signing_in`, `signed_out` or `expired`, with the last refresh and any error;
  - `DELETE …` signs out.
- **The model route** `/model/{path}`:
  1. finds the run by the capability's hash;
  2. sends the request to the payer's endpoint (ChatGPT's Codex backend for a sign-in, OpenAI's API for a key) with their credential;
  3. streams the answer back and counts tokens from the final event.
- **ChatGPT sign-ins and API keys** both work as credentials (a key in `auth.json`).

## Locked decisions

| Decision | Value | Why | Since | Proven by |
| --- | --- | --- | --- | --- |
| Sign-ins live on the runtime's disk, **temporarily** | `<data_dir>/tenants/<tenant>/<user>/codex/auth.json`, folder 0700, file 0600. To move to a secret manager | Simplest for the POC; this disk is the only copy, so it needs a backup (checkpoint 7) | 2026-11-19 | test_codex_homes.py::test_the_layout_is_tenant_user_codex; test_codex_homes.py::test_homes_are_owner_only |
| One Codex home per payer | Codex keeps one sign-in per home, so each payer gets their own | Switching one home between people would mix sign-ins | 2026-11-19 | test_codex_homes.py::test_the_layout_is_tenant_user_codex |
| A payer id can't escape its folder | Each segment matches `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` | DSV-3 | 2026-11-19 | test_broker.py::test_bad_payer_ids_are_refused |
| Codex refreshes, never our code | The broker asks Codex to refresh; it never calls OpenAI's token endpoint | We follow whatever Codex does | 2026-11-19 | test_broker.py::test_credential_reads_the_payers_home |
| One refresh at a time per payer | A lock per payer; callers waiting behind a refresh reuse its result | Refresh tokens rotate: two refreshes at once break the sign-in | 2026-11-19 | test_broker.py::test_two_refreshes_at_once_refresh_once (and live, scripts/live_broker_check.py) |
| Keep sign-ins fresh | Every 10 minutes, refresh any sign-in whose last refresh is 6 hours old | Keeps the refresh token in use whatever restarts happen | 2026-11-19 | test_broker.py::test_keep_fresh_refreshes_only_sign_ins_6_hours_old |
| A refused sign-in is dead until the person signs in again | After the gateway's retry fails, the payer is `expired`; their slots block with "reconnect Codex" | A dead sign-in is not a usage limit (RC's review #3) | 2026-11-20 (#9) | test_broker.py::test_a_sign_in_the_provider_refused_is_expired_until_signed_in_again |
| An upstream 401: refresh once, retry once | Then 401 `credential_expired`, "reconnect Codex" | A token can expire mid-run | 2026-11-19 | test_model_route.py::test_an_upstream_401_refreshes_once_and_retries; test_model_route.py::test_a_401_after_the_refresh_marks_the_sign_in_dead |
| An upstream 403 is a dead sign-in | No refresh; marked expired | A refusal, not an expired token | 2026-11-20 (#9) | test_model_route.py::test_a_403_marks_the_sign_in_dead_without_a_refresh |
| A usage limit only by its error type | Only a 429 of type `usage_limit_reached` records the payer's reset; an ordinary 429 records nothing | Never confuse a rate limit, a refusal and a used-up plan (RC's review #3) | 2026-11-20 (#9) | test_model_route.py::test_a_usage_limit_is_recorded_and_not_a_dead_sign_in; test_model_route.py::test_an_ordinary_429_records_nothing |
| Usage reset when OpenAI gives no time | 1 hour | A safe guess; not yet seen live | 2026-11-19 | test_model_route.py::test_usage_reset_forms |
| Capabilities: random, stored as a hash, one run | 32 random bytes; only the SHA-256 is stored; valid only while its slot runs, and at most 2 hours | A leaked capability is worth one run of one reviewer (DSV-8) | 2026-11-19 | test_model_route.py::test_a_finished_runs_capability_is_refused; test_model_route.py::test_a_capability_expires_with_its_maximum_run_time; test_jobs.py::test_a_job_starts_in_its_prs_pod_with_the_doppels_rules_and_its_run_pays_its_payer |
| Token budget per run | 2,000,000 tokens unless the slot sets its own; refused before anything goes upstream | Bounds a runaway or hijacked run | 2026-11-19 | test_model_route.py::test_over_budget_is_refused_before_anything_goes_upstream |
| Tokens counted from OpenAI's own figure | The `usage` of the final event, recorded before that event reaches Codex | Never the pod's numbers; Codex hangs up right after the final event | 2026-11-19 | test_model_route.py::test_tokens_are_counted_from_the_final_event; test_model_route.py::test_tokens_are_recorded_before_the_final_event_reaches_codex |
| Refusals are 403, not 429 | In OpenAI's error shape | Codex retries a 429 forever | 2026-11-19 | test_model_route.py::test_over_budget_is_refused_before_anything_goes_upstream |
| Nothing logs a token | Logs name the slot and payer only | — | 2026-11-19 | test_model_route.py::test_logs_name_the_payer_never_the_token_or_capability; test_broker.py::test_logs_never_hold_a_token |
| Max model request | 16 MB | One request carries the whole context | 2026-11-19 | not tested |

## Not yet

- A secret manager (OpenBao or similar) instead of the disk; a backup of `tenants/` meanwhile (checkpoint 7).
- API keys pasted through the UI.
- A real `usage_limit_reached` answer seen live.
- Troy's live sign-in (scripts/live_broker_check.py --payers summation/u-troy).
