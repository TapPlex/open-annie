# annie broker

open-annie's only server. It does two jobs:

1. **Relays Gemini Live voice.** The browser streams PCM audio over `WS /live`. The broker connects to
   Gemini through Cloudflare AI Gateway, keeps the API key server-side, applies session/IP/budget limits,
   and forwards audio and events in both directions.
2. **Proxies TypeSafe Jev.** Jev rejects browser CORS and the key must stay server-side. The broker keeps one warm
   HTTP/2 pool to `api.typesafe.ai`, applies a 1.5 s deadline (`ANNIE_DECIDE_TIMEOUT_S`; the browser enforces its own tighter budgets) with zero retries, and trips a circuit breaker after
   3 straight failures (open for 30 s). A token-bucket governor keeps it under Jev's 1,200 rpm. Every failure becomes
   a fast `503` so the browser drops to its local rule floor.

It also polices spend. Each session is capped at 180 s and closed after 30 s idle. Each IP gets 2 sessions a day
(hosted mode), and there is a daily budget (default $25). A session is admitted only if a full-length session still
fits the remaining budget and `ANNIE_MAX_SESSIONS` is not reached.

## Contract

Source of truth: [`docs/contracts.md`](../docs/contracts.md).

| Route | In | Out |
|---|---|---|
| `GET /health` | – | `{ok, version, providers: {voice, decide}}` |
| `WS /live` | First frame `{type:"annie.setup", instructions?}` then Gemini `realtimeInput` | Gemini Live events and broker session token |
| `POST /decide` | `{token, state, questions}` | Jev `{model, answers, usage}` + `latency_ms` |
| `WS /decide` | same JSON + `id` per message | same JSON + `id` per reply (replies may arrive out of order) |
| `GET /status` | – | `{spent_today_usd, budget_usd, active_sessions, max_sessions, voice_seconds_today}` |
| `POST /rosemate/{session,consent,action,close}`, `WS /rosemate/events` | – | optional Rosemate integration, off unless configured: [docs/rosemate.md](../docs/rosemate.md) |

- `expires_at` is the broker's hard cap (Unix seconds). The session is closed then.
- `token` is `session_id.expiry.hmac` and is valid until `expires_at + 30 s`.
- Pass the token on every `/decide` call.

Errors are always `{error: code}` and never contain upstream bodies:

| Status | `error` | When |
|---|---|---|
| 400 | `invalid_request` | bad JSON or fields |
| 401 | `invalid_token` | missing, forged or expired `/decide` token |
| 403 | `origin_not_allowed` | `Origin` not in `ANNIE_ALLOWED_ORIGINS` |
| 413 | `too_large` | body > 64 KB |
| 422 | `decide_invalid` | Jev rejected the question shape |
| 429 | `budget_exhausted` / `ip_limit` / `at_capacity` | admission control |
| 503 | `voice_unavailable` | Gemini API key or positive voice-cost estimate is missing |
| 503 | `decide_unavailable` (+ `reason`) | `no_key`, `disabled`, `breaker_open`, `timeout`, `rate_limited`, `session_rate_limited`, `upstream_error`, `too_many_inflight` |

The Gemini Live setup/event protocol, Cloudflare Gateway route, PCM formats and current verification limits
are documented in [UPSTREAM.md](UPSTREAM.md).

## Run locally

```bash
cd broker
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python local.py                   # http://127.0.0.1:8787  (PORT=... to change)
# or from the repo root: broker/.venv/bin/python -m broker.local
curl localhost:8787/health
.venv/bin/python -m pytest -q               # mocked upstreams; no keys needed
```

`local.py` reads `../.env` and `./.env`, and real environment variables win. With no keys it still starts and `/health`
reports both providers `false`, so the stage runs in mock/floor mode. Locally the per-IP limit is off and
`ANNIE_DEV=1`, so `GET /dev-token` returns a 15-minute `/decide` token without a voice session. That route is 404
unless `ANNIE_DEV=1` and does not exist on fal.

## Deploy on fal

```bash
fal secrets set ANNIE_GEMINI_API_KEY="$GEMINI_API_KEY" ANNIE_TYPESAFE_API_KEY="$TYPESAFE_API_KEY" \
    ANNIE_GEMINI_BASE_URL=https://gateway.ai.cloudflare.com/v1/ac4054842a60a05296897e5f7042b4cd/winbestsai/compat \
    ANNIE_HMAC_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
fal secrets set ANNIE_ALLOWED_ORIGINS=https://your-stage.example.com
fal deploy broker/fal_app.py::AnnieBroker --app-name annie-broker --auth public
curl https://fal.run/<your-username>/annie-broker/health
```

The app is a CPU `S` machine with `min_concurrency=1`, `max_concurrency=1` (budget state is in-process),
`keep_alive=300`, `max_multiplexing=32` and `request_timeout=300`. It must be `--auth public` because browsers call it
without a fal key. The budget, the per-IP limit, the origin allowlist and the HMAC tokens do the gating.

After the first deploy, check which `X-Forwarded-For` the fal gateway sends and adjust `ANNIE_IP_HEADER` if needed
(UPSTREAM.md §3).

## Configuration

| Env | Default | |
|---|---|---|
| `GEMINI_API_KEY` / `ANNIE_GEMINI_API_KEY` | – | server-side Gemini key; enables `WS /live` |
| `GEMINI_BASE_URL` / `ANNIE_GEMINI_BASE_URL` | Cloudflare AI Gateway compat endpoint | upstream Gateway base URL |
| `GEMINI_MODEL` / `ANNIE_GEMINI_MODEL` | `gemini-3.8-live` | Gemini Live model |
| `GEMINI_VOICE` / `ANNIE_GEMINI_VOICE` | `Kore` | Google Live voice name |
| `ANNIE_VOICE_USD_PER_SECOND` | required | positive spend estimate from current Gemini audio-token pricing; voice stays unavailable until set |
| `TYPESAFE_API_KEY` | – | enables `/decide` |
| `ANNIE_HMAC_SECRET` | random per process | set it, or tokens die on restart |
| `ANNIE_DAILY_BUDGET_USD` | 25 | estimated voice cost + Jev tokens × $0.042/M |
| `ANNIE_MAX_SESSIONS` | 20 | concurrent voice sessions |
| `ANNIE_SESSION_CAP_S` | 180 | hard per-session cap |
| `ANNIE_IDLE_TIMEOUT_S` | 30 | close after this long with no transcript or generated output |
| `ANNIE_PER_IP_DAILY` | 2 (0 locally) | sessions per IP per UTC day; 0 = off |
| `ANNIE_ALLOWED_ORIGINS` | `http://localhost:*,http://127.0.0.1:*` | comma list; `*` wildcards; `*` alone = any |
| `JEV_MODEL` | `jev-latest` | |
| `ANNIE_JEV_RPM` | 1000 | governor rate (Jev limit is 1,200) |
| `ANNIE_DECIDE_TIMEOUT_S` | 1.5 | server-side Jev deadline |
| `ANNIE_DEV` | 0 (1 locally, forced 0 on fal) | enables `GET /dev-token` → `{token, expires_at}` for /decide without a voice session |
| `ANNIE_INSTRUCTIONS` | empty | fallback system instructions when the browser sends none |
| `ANNIE_IP_HEADER` | – (fal: `x-forwarded-for`) | header holding the client IP (leftmost entry) |
| `ANNIE_ROSEMATE_*` | – (off) | optional Rosemate integration; full table in [docs/rosemate.md](../docs/rosemate.md#setup) |

## Cost notes

- **Voice.** Gemini Live bills audio by tokens; the broker has no provider-side usage stream and estimates spend
  from elapsed session seconds using `ANNIE_VOICE_USD_PER_SECOND`. Set that value from current model/audio pricing.
  Admission reserves each open session at its cap. This estimate is not a billing quote or live usage measurement.
- **Jev.** About $0.000017 per decision (about 400 input tokens). Measured p50 is 108 ms and p90 149 ms from a dev box
  (UPSTREAM.md §2.5).
- **fal runner.** One CPU `S` runner stays warm all day (`min_concurrency=1`). fal bills runner lifetime per second, but
  CPU machine prices are not published on fal.ai/pricing. **Runner cost is unmeasured.** Check the fal dashboard's
  billing page after a day of uptime.

## Privacy

- The broker keeps **no transcripts and no state text**. `/decide` bodies go straight to Jev and are never logged
  or stored. The live WebSocket forwards transcript and audio frames without recording them.
- IPs exist only as keyed hashes in memory, for the per-IP counter. They reset daily and on restart.
- Logs hold status codes, close reasons and error class names only.
- There are no analytics, cookies or third-party calls beyond Cloudflare AI Gateway, Gemini and TypeSafe, plus Rosemate only when the
  optional integration is configured ([docs/rosemate.md](../docs/rosemate.md)). Then the broker subscribes to
  that account's toy command stream and, after a user opts in, sends the toy actions they click. It forwards
  command ids, actions, intensities and timestamps to authorized stages, drops Rosemate's free-text `reason`, and
  logs only error classes and status codes. The Rosemate token never leaves the broker.
- If a Gemini API key was shared in chat or logs, rotate it before deployment.

## Layout

```
annie_broker/  config · tokens · voice · decide · budget · rosemate (optional) · app (Broker + create_app)
fal_app.py     fal App exposing the same Broker
local.py       uvicorn runner (port 8787)
tests/         pytest, mocked upstreams (httpx.MockTransport)
UPSTREAM.md    verified upstream API facts + live Jev measurements
```

## Deploy notes (fal 1.79, verified 2026-09-28)

- Deployed at `https://fal.run/<owner>/annie-broker` (routes `/health`, `/status`, WS `/live`, `/decide`).
  The optional `/rosemate/*` routes use the same endpoint mechanism but have **not** been deployed or verified on fal.
- Secrets use namespaced names (`ANNIE_GEMINI_API_KEY`, `ANNIE_TYPESAFE_API_KEY`, `ANNIE_HMAC_SECRET`,
  `ANNIE_ALLOWED_ORIGINS`), because fal secrets are shared by every app on the account.
- If `FAL_KEY` is set in your shell (e.g. an API-scoped key), the CLI uses it instead of your login and
  `fal secrets set` fails with `serverless:secrets:write`; run the CLI with `env -u FAL_KEY`.
- fal's runtime pins `pydantic==2.13.4`; `requirements` must match.
- `local_python_modules` ships code by pickling what the app references: import `annie_broker` at module
  level (not inside `setup()` or handlers), and don't use `from __future__ import annotations` in
  `fal_app.py`, or FastAPI can't resolve `request: Request` on the runner (every POST returns 422).
