# Optional Rosemate integration

open-annie can follow a [Rosemate](https://rosemate.winbests.net/) toy session. When the toy is triggered, Annie
says a short line, changes her expression and moves a little in the scene. An opted-in user can also send a small
set of toy actions through the broker. **The integration is off by default.** It changes nothing for the preview,
replay, capture or live modes unless the broker is configured and the stage is opened with `?rosemate`.

What it is and isn't:

- open-annie **observes** commands and acts them out on the avatar. It is **not** a hardware executor. The Rosemate
  PWA (web app) controls the toy, so keep it **open and paired with the toy** for anything physical to happen.
- An outbound toy action that succeeds returns `delivery: "queued"`. That means Rosemate's MCP server accepted the
  call. It does **not** prove the toy moved.
- open-annie never reports the toy as connected, and never writes avatar state into Rosemate's device telemetry.
- None of this code comes from the Rosemate app. It is an original client written against the wire protocol
  described below.

## How it fits together

```
 Rosemate MCP (JSON-RPC)  ◀── tools/call (opt-in, user click) ───┐
 POST /rosemate/mcp                                                │
                                                       open-annie broker  (holds the Rosemate token)
 Rosemate command stream  ── SSE haptic_command ──────▶  sanitize · dedupe · drop stale
 GET /rosemate/api/mcp/bridge/events                               │
                                                                   ▼  WS /rosemate/events (scoped token)
 Rosemate PWA (paired toy) ◀── same commands                    stage ?rosemate
   does the actual hardware work                                 talk · emotion · position (avatar only)
```

There are two separate upstream endpoints, and they are easy to confuse:

| | URL | Used for |
|---|---|---|
| MCP endpoint | `POST https://rosemate.winbests.net/rosemate/mcp` | JSON-RPC 2.0: `initialize` (protocol `2024-11-05`), `notifications/initialized`, `tools/list`, `tools/call` |
| Command stream | `GET https://rosemate.winbests.net/rosemate/api/mcp/bridge/events` | SSE `haptic_command` events (the same ones the Rosemate PWA executes) |
| Mailbox poll (fallback, off) | `GET …/rosemate/api/mcp/bridge/poll` | `{success, command}`. **Consuming**: see below |

The command stream is **not** `/rosemate/mcp/sse`. That URL is the MCP RPC transport (`endpoint`/`message` events)
and carries no toy commands.

The broker derives the stream and poll URLs from `ANNIE_ROSEMATE_MCP_URL` (`…/rosemate/mcp` →
`…/rosemate/api/mcp/bridge/events`). Set `ANNIE_ROSEMATE_EVENTS_URL` if your deployment differs. Every upstream URL
comes from server configuration, never from a client, and is refused if it carries a query, fragment or user info.

## Setup

1. **Rotate the Rosemate token first** if it has ever been pasted into a chat, an issue, a URL or a log. Treat the
   old one as leaked.
2. Configure the broker. Put these in `.env` for `local.py`, or in fal secrets:

   ```bash
   ANNIE_ROSEMATE_MCP_URL=https://rosemate.winbests.net/rosemate/mcp   # no ?token=… (refused)
   ANNIE_ROSEMATE_MCP_TOKEN=                                           # the Rosemate token, server-side only
   ANNIE_ROSEMATE_ACCESS_KEY=                                          # >= 16 chars; you give this to stage users
   ANNIE_ROSEMATE_TOY_ACTIONS=0                                        # 1 = allow outbound toy actions (users still opt in)
   ```

   Generate an access key with `python -c "import secrets;print(secrets.token_urlsafe(24))"`.

   On fal (secret names are namespaced because fal secrets are shared by every app on the account):

   ```bash
   fal secrets set ANNIE_ROSEMATE_MCP_URL=https://rosemate.winbests.net/rosemate/mcp \
       ANNIE_ROSEMATE_MCP_TOKEN=<rotated token> ANNIE_ROSEMATE_ACCESS_KEY=<access key>
   ```

   `ANNIE_HMAC_SECRET` must be set, as it already must be for voice.

3. Open the Rosemate PWA, pair the toy, and keep it open.
4. Open the stage with `?rosemate&broker=<broker URL>`. Enter the access key in the Rosemate panel and press
   **Connect**. Press **Enable voice** so the browser is allowed to speak (see "Speech" below).

The broker only turns the integration on when it has a valid URL, a token, and an access key of at least 16
characters. Otherwise the routes answer `503 rosemate_unavailable`. `/health` does not mention the integration and
never shows any part of the configuration.

| Env | Default | |
|---|---|---|
| `ANNIE_ROSEMATE_MCP_URL` | – | MCP endpoint (https; http only for localhost) |
| `ANNIE_ROSEMATE_MCP_TOKEN` | – | sent upstream only in the `Authorization` header (Bearer scheme), never to browsers |
| `ANNIE_ROSEMATE_ACCESS_KEY` | – | shared key that stage users exchange for a session |
| `ANNIE_ROSEMATE_EVENTS_URL` | derived | command stream URL |
| `ANNIE_ROSEMATE_TOY_ACTIONS` | 0 | allow outbound `rosemate_haptic_trigger` (stop and status are always allowed) |
| `ANNIE_ROSEMATE_POLL` | 0 | mailbox poll fallback; consuming, see below |
| `ANNIE_ROSEMATE_SESSION_TTL_S` | 1800 | integration session lifetime |
| `ANNIE_ROSEMATE_MAX_SESSIONS` | 4 | concurrent integration sessions |
| `ANNIE_ROSEMATE_TIMEOUT_S` | 8 | deadline per MCP call |
| `ANNIE_ROSEMATE_MAX_INTENSITY` | 60 | cap on outbound `intensity` (Rosemate accepts 0-100) |
| `ANNIE_ROSEMATE_MAX_DURATION_S` | 10 | cap on outbound `duration_sec` (Rosemate accepts 1-120) |
| `ANNIE_ROSEMATE_TOY_PER_MIN` | 12 | outbound haptic calls per session per minute (burst 2) |
| `ANNIE_ROSEMATE_EVENT_MAX_AGE_S` | 30 | older commands are dropped as stale |

## Authorization model (read this before exposing it)

- **Single tenant.** The broker holds **one** Rosemate account token. Everyone holding the access key observes
  the same command stream and, if enabled, drives the same account's toy. There is no per-user Rosemate
  binding. Give the access key only to people allowed to see and drive that account. Rotate it to revoke access.
- The access key is typed into the stage once. The browser sends it to `POST /rosemate/session` and does not keep
  it. That route is limited to 5 attempts per IP per minute and compares keys in constant time.
- The session token is an HMAC token signed with a key derived only for this integration. It does not work on
  `/decide`, and voice tokens do not work here. It expires after `ANNIE_ROSEMATE_SESSION_TTL_S`, lives only in
  page memory (never in the URL or `localStorage`), and is revoked on page close (`/rosemate/close`).
- The origin allowlist (`ANNIE_ALLOWED_ORIGINS`) also applies, but it is **not** authorization. Every route also
  needs the access key or a session token.
- Integration sessions cost no voice money. They don't need a microphone or a GPT-Live session.

## Broker routes

All are `POST` with JSON bodies (fal endpoints are POST or WebSocket only), and errors are `{error: code}` with no
upstream bodies.

| Route | In | Out |
|---|---|---|
| `POST /rosemate/session` | `{access_key, toy_actions?}` | `{token, expires_at, features: {events, toy_actions, poll}, toy_consent}` |
| `POST /rosemate/consent` | `{token, toy_actions}` | `{toy_consent}` |
| `POST /rosemate/action` | `{token, tool, arguments}` | `{ok, tool, delivery: "queued"}`, or `{ok, tool, text}` for device status |
| `POST /rosemate/close` | `{token}` | `{closed: true}` (also ends the session's sockets) |
| `WS /rosemate/events` | first message `{token}` | `{type: "status"\|"command"\|"rejected"\|"error", …}` |

| Status | `error` | When |
|---|---|---|
| 400 | `invalid_request` / `invalid_action` (+ `field`) / `unknown_tool` | bad body, arguments out of bounds, tool not allowlisted |
| 401 | `invalid_access_key` / `invalid_token` | wrong key; missing, forged, expired or revoked session |
| 403 | `origin_not_allowed` / `toy_actions_disabled` / `consent_required` | origin; operator flag off; user hasn't opted in |
| 409 | `toy_busy` | another haptic call is still in flight |
| 413 | `too_large` | body over 8 KB |
| 429 | `rate_limited` / `at_capacity` | per-IP, per-session or session-count limits |
| 502 | `toy_upstream_error` / `toy_auth_failed` / `toy_rpc_error` / `toy_tool_error` / `toy_tool_unsupported` / `toy_protocol_mismatch` | Rosemate failed. Redacted, with no upstream text |
| 503 | `rosemate_unavailable` | integration not configured |
| 504 | `toy_timeout` (+ `outcome: "unknown"`) | deadline passed; the call **may** have been queued |

The WebSocket closes with `1008` (origin refused or integration off), `4401` (bad token) or `4429` (more than 2
sockets for one session). The broker re-checks the session at least every 5 s and ends the socket when it's revoked
or expired.

### Outbound toy actions

Only the tools Rosemate advertises in `tools/list` and that open-annie knows are ever called:

| Tool | Arguments | Gate |
|---|---|---|
| `rosemate_haptic_trigger` | `action` ∈ `vibrate pulse wave tease escalate stop`, integer `intensity` 0..`MAX_INTENSITY`, `duration_sec` 1..`MAX_DURATION_S`, optional `pattern` ∈ `steady pulse wave tease escalate climax`, optional `reason` (≤ 120 chars, cleaned) | `ANNIE_ROSEMATE_TOY_ACTIONS=1` **and** the user's consent, rate limit, one call at a time |
| `rosemate_toy_stop` | optional `reason` | **always** available to an authorized session; own rate bucket (falls back to `rosemate_haptic_trigger` with `action: "stop"` if the stop tool isn't advertised) |
| `rosemate_get_device_status` | `{}` | authorized session; reply text redacted and capped at 1000 chars |

Hardware calls are **never retried**. A retry could actuate twice, so a timeout is reported as `outcome: "unknown"`.
Received commands **never** cause an outbound call. Inbound commands only drive the avatar, so there is no feedback
loop.

### Inbound commands

The broker keeps one upstream subscription to the command stream, and only while at least one stage is
connected. Rosemate ends each stream after about 30 s, so the broker reconnects (0.5 s after a normal end, with
backoff up to 30 s on errors). For each event:

- The `connected` handshake is never parsed or forwarded, because it may echo the secret. SSE comments
  (heartbeats) are skipped.
- `haptic_command` data is rebuilt from allowlisted fields only:
  `{id, type: "haptic_trigger"|"stop", action, intensity?, duration_sec?, pattern?, timestamp}`. Upstream `reason`
  text is dropped, because it is untrusted and the avatar never needs it.
- Malformed, unknown-type, unknown-action, stale (older than `EVENT_MAX_AGE_S`) and far-future commands are
  dropped. A `{type: "rejected", reason}` notice is sent instead.
- Commands are de-duplicated by `id` (10-minute TTL, 1024 ids) across reconnects and the poll fallback. The
  browser keeps its own second id set.
- Each browser socket has a 32-message queue. When it fills, the oldest message is dropped.

**Polling is off by default.** `GET …/bridge/poll` takes the command out of Rosemate's mailbox, so a poller can
**steal commands from the Rosemate PWA**, and the toy would then never get them. Enable `ANNIE_ROSEMATE_POLL=1` only
if you understand that. Even then the broker polls just once per failed stream attempt.

## Avatar actions

Defined in [`packages/core/src/actions.js`](../packages/core/src/actions.js). Every action is validated, and unknown
fields are rejected.

| Action | Fields |
|---|---|
| `talk` | `message`: string, 1-200 chars after trimming (control characters removed) |
| `emotion` | `name`: one of the faces in [contracts](contracts.md#vocabularies); `intensity` 0-1 (0.8); `hold_s` 0.5-10 (3) |
| `position` | `x` −0.6..0.6, `y` 0..0.15, `z` −0.6..0.6 (metres, scene world coordinates, home is the origin); `duration_s` 0-5 (0.8); `hold_s` 0-30 (0 = stay; otherwise drift home afterwards) |
| `toy_action` | `tool` (the three above), `arguments`. **Only from the user** (panel or console), never from a trigger or a trigger map |

- **Position** moves only the avatar root (`character.vrm.scene.position`). The skeleton and hips animation are not
  touched. A new move starts from wherever she is (no jump), cancel freezes her, and leaving the page puts her home.
- **Emotion** sets the face through `Character.setFace` and holds it for `hold_s`. During that time Jev's face
  picks are declined, and the decision log shows them as not acted. Barge-in still wins. If the face director is
  mid-dwell, the face applies shortly after, and the HUD tag says "queued".
- **Stop** (an inbound `stop` command, or `annieActions.stop()`) first cancels queued speech, a running move and the
  face hold. Then it runs the map's `stop` actions (by default: relieved, back home). It never replays a hardware
  action.
- Triggers are ignored while the scripted demo plays. `talk` is refused in live mode (`live_voice_active`), because
  GPT-Live owns her voice there and the open mic would hear the line.

### Default trigger map

| Command | Annie |
|---|---|
| `vibrate` | "Oh! I felt a little buzz." · surprised · small step forward-right, back after 2.5 s |
| `pulse` | "Ooh, pulses. Got it!" · amused · small step forward-left |
| `wave` | "Whoa, here comes a wave." · happy · small step right |
| `tease` | "Hey, cheeky!" · playful · small step left |
| `escalate` | "Okay, it's building up!" · surprised · small step forward |
| `stop` | cancel, then relieved · back home |

Override it with `?rosemateMap=path/to/map.json` (same origin only). It must pass `validateTriggerMap`: keys
`haptic_trigger.{vibrate,pulse,wave,tease,escalate,default}` and `stop`, at most 4 actions each, and no
`toy_action`. A map that fails validation is reported and the defaults stay. For example:

```json
{
  "haptic_trigger": {
    "pulse": [{ "type": "talk", "message": "Got it." }, { "type": "emotion", "name": "curious", "intensity": 0.6 }],
    "default": [{ "type": "emotion", "name": "surprised" }]
  },
  "stop": [{ "type": "emotion", "name": "relieved" }, { "type": "position", "x": 0, "y": 0, "z": 0 }]
}
```

## Speech

GPT-Live has **no "speak this text" command**. Its data channel accepts `session.close`, mute/unmute and the
instructions/thinking/commentary appends ([UPSTREAM.md §1.4](../broker/UPSTREAM.md)), and none of those speaks a given
line. GPT-Live is also not the Realtime API, so there is no `response.create` either. So `talk` uses a clearly
labelled **fallback: the browser's Web Speech synthesizer** (`packages/core/src/voice-speech.js`).

- Browsers only allow audio after a user gesture. Until **Enable voice** is pressed, `talk` returns
  `needs_activation`. A line is never reported as spoken unless the synthesizer actually started it.
- Results: `{ok: true}` once spoken; otherwise `needs_activation`, `speech_unavailable`, `busy` (3 lines queued),
  `cancelled` (stop, barge-in, page close), `no_audio` (didn't start within 3 s) or `timeout`.
- The line is captioned through `speech.start {text}` / `speech.end {text, ok}` bus events. They deliberately do not
  use `assistant.*`, which means a model turn, feeds Jev and the history, and must not be faked.
- The browser's synthesizer audio can't be analysed, so the mouth is approximated from word boundaries and a
  syllable rhythm. It is less exact than the GPT-Live or scripted lipsync.
- Voice quality depends on the OS and browser. Local voices are preferred. Some browsers only offer network voices,
  which send the text to the browser vendor; the panel then says "network voice".

## Avatar-only testing (no broker, no toy, no token)

Open `stage/?rosemate`. The panel offers **mock** triggers that run locally and never reach a broker or a toy:

```js
annieActions.trigger("pulse");                       // the full mapped response
annieActions.stop();
annieActions.dispatch({ type: "talk", message: "Hello!" });   // after "Enable voice"
annieActions.dispatch({ type: "emotion", name: "proud", intensity: 0.7, hold_s: 4 });
annieActions.dispatch({ type: "position", x: 0.2, y: 0, z: 0.1, duration_s: 1, hold_s: 3 });
annieActions.setMap({ haptic_trigger: { default: [{ type: "emotion", name: "happy" }] } });
```

Each call resolves `{ok, …}` or `{ok: false, error}`. The panel log shows the last few results.

Tests use mocks only: `cd broker && python -m pytest -q tests/test_rosemate.py`, and from the repo root
`node --test packages/core/test/*.test.mjs`.

## Deployment caveats (unverified)

- None of this has been run against the live Rosemate service or real hardware. The wire shapes come from observing
  the service. Timestamps are accepted as epoch seconds, epoch milliseconds or ISO 8601, because the exact upstream
  format is unconfirmed. An upstream `protocolVersion` other than `2024-11-05` is treated as a mismatch.
- On fal, the new routes reuse the same endpoint mechanism as `/decide`, but they have not been deployed.
  `request_timeout=300` ends long WebSockets, and the stage reconnects with backoff. The upstream SSE subscription is
  an outbound connection from the runner, so it is not affected by that timeout.
- `max_concurrency=1` keeps integration sessions in one process. A restart drops them, and users reconnect with
  the access key.
