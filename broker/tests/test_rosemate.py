"""Rosemate integration: mocked upstream only (no network, no hardware)."""

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from annie_broker import Settings, tokens
from annie_broker.rosemate import (
    HAPTIC_TOOL,
    STATUS_TOOL,
    STOP_TOOL,
    EventHub,
    Rosemate,
    RosemateError,
    SeenIds,
    bounded_lines,
    bridge_url,
    check_url,
    iter_sse,
    normalize_command,
    validate_haptic,
)
from conftest import BASE, LEAK

ORIGIN = {"Origin": "http://localhost:5173"}
FAKE_TOKEN = "fake-rosemate-token-for-tests-only"
ACCESS_KEY = "operator-access-key-for-tests"
MCP_URL = "https://rosemate.test/rosemate/mcp"
EVENTS_PATH = "/rosemate/api/mcp/bridge/events"
POLL_PATH = "/rosemate/api/mcp/bridge/poll"

ON = dict(rosemate_mcp_url=MCP_URL, rosemate_mcp_token=FAKE_TOKEN, rosemate_access_key=ACCESS_KEY)
TOYS = dict(ON, rosemate_toy_actions=True)
HAPTIC_ARGS = {"action": "pulse", "intensity": 40, "duration_sec": 3, "pattern": "pulse"}


def now_ms() -> int:
    return int(time.time() * 1000)


def command(cid="cmd_1", **kw) -> dict:
    return {"id": cid, "timestamp": now_ms(), "type": "haptic_trigger", "action": "vibrate",
            "intensity": 50, "duration_sec": 5, "pattern": "steady", "reason": "ignore me", **kw}


def sse(*cmds: dict, handshake: bool = True) -> str:
    out = ""
    if handshake:  # upstream may echo the credential in its handshake
        out += f'event: connected\ndata: {{"ok":true,"token":"{FAKE_TOKEN}"}}\n\n'
    out += ": heartbeat\n\n"
    for c in cmds:
        out += f"event: haptic_command\ndata: {json.dumps(c)}\n\n"
    return out


class FakeRosemate:
    """Scriptable Rosemate upstream: MCP JSON-RPC on POST, bridge events on GET."""

    def __init__(self, tools=(HAPTIC_TOOL, STOP_TOOL, STATUS_TOOL), events=None, protocol="2024-11-05",
                 call=None, mcp_status=200, poll=None):
        self.tools, self.protocol, self.mcp_status = tools, protocol, mcp_status
        self.events = events or (lambda n: sse())
        self.call = call or (lambda params: {"content": [{"type": "text", "text": "queued"}], "isError": False})
        self.poll = poll
        self.requests: list[httpx.Request] = []
        self.methods: list[str] = []
        self.calls: list[dict] = []
        self.streams = 0
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.path == "/rosemate/mcp" and req.method == "POST":
            msg = json.loads(req.content)
            self.methods.append(msg["method"])
            if self.mcp_status != 200:
                return httpx.Response(self.mcp_status, text=LEAK)
            if "id" not in msg:
                return httpx.Response(202)
            if msg["method"] == "initialize":
                result = {"protocolVersion": self.protocol, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fake"}}
            elif msg["method"] == "tools/list":
                result = {"tools": [{"name": t, "inputSchema": {"type": "object"}} for t in self.tools]}
            elif msg["method"] == "tools/call":
                self.last_id = msg["id"]
                self.calls.append(msg["params"])
                result = self.call(msg["params"])
                if isinstance(result, httpx.Response):
                    return result
                if isinstance(result, Exception):
                    raise result
            else:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"],
                                                 "error": {"code": -32601, "message": LEAK}})
            if "error" in result:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "error": result["error"]})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result})
        if req.url.path == EVENTS_PATH:
            self.streams += 1
            body = self.events(self.streams)
            if isinstance(body, httpx.Response):
                return body
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        if req.url.path == POLL_PATH and self.poll:
            return httpx.Response(200, json=self.poll())
        return httpx.Response(404)


def open_session(c, **body):
    return c.post("/rosemate/session", json={"access_key": ACCESS_KEY, **body}, headers=ORIGIN)


def action(c, token, tool=HAPTIC_TOOL, arguments=None):
    return c.post("/rosemate/action", json={"token": token, "tool": tool, "arguments": arguments or {}},
                  headers=ORIGIN)


def all_text(fake: FakeRosemate) -> str:
    return " ".join(str(r.url) for r in fake.requests)


# -- configuration -------------------------------------------------------------


def test_disabled_by_default(make_client):
    c = make_client()
    r = c.post("/rosemate/session", json={"access_key": ACCESS_KEY}, headers=ORIGIN)
    assert r.status_code == 503 and r.json() == {"error": "rosemate_unavailable"}
    assert c.get("/health").json()["providers"] == {"voice": True, "decide": True}
    with pytest.raises(Exception):
        with c.websocket_connect("/rosemate/events", headers=ORIGIN) as ws:
            ws.receive_text()


def test_settings_repr_hides_secrets():
    s = replace(BASE, **ON)
    assert FAKE_TOKEN not in repr(s) and ACCESS_KEY not in repr(s)


def test_from_env_defaults_off(monkeypatch):
    for k in ("ANNIE_ROSEMATE_MCP_URL", "ANNIE_ROSEMATE_MCP_TOKEN", "ANNIE_ROSEMATE_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert not Rosemate(s).enabled and s.rosemate_toy_actions is False and s.rosemate_poll is False


def test_url_rules():
    assert check_url(MCP_URL) == MCP_URL
    assert check_url(MCP_URL + "?token=abc") is None  # never a credential in the URL
    assert check_url("https://" + "user" + ":" + "pw" + "@" + "rosemate.test/rosemate/mcp") is None
    assert check_url("http://rosemate.test/rosemate/mcp") is None
    assert check_url("http://localhost:9000/rosemate/mcp")
    assert bridge_url(MCP_URL, "events") == "https://rosemate.test" + EVENTS_PATH
    assert bridge_url(MCP_URL, "poll") == "https://rosemate.test" + POLL_PATH
    assert not Rosemate(replace(BASE, **dict(ON, rosemate_mcp_url=MCP_URL + "?token=x"))).enabled
    assert not Rosemate(replace(BASE, **dict(ON, rosemate_access_key="short"))).enabled
    assert not Rosemate(replace(BASE, **dict(ON, rosemate_mcp_token=None))).enabled
    assert Rosemate(replace(BASE, **ON)).enabled


# -- authorization -------------------------------------------------------------


def test_session_requires_access_key_not_just_origin(make_client):
    fake = FakeRosemate()
    c = make_client(rosemate=fake, **ON)
    assert c.post("/rosemate/session", json={}, headers=ORIGIN).status_code == 400
    r = c.post("/rosemate/session", json={"access_key": "wrong-key-wrong-key"}, headers=ORIGIN)
    assert r.status_code == 401 and r.json() == {"error": "invalid_access_key"}
    r = c.post("/rosemate/session", json={"access_key": ACCESS_KEY}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = open_session(c)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"token", "expires_at", "features", "toy_consent"}
    assert body["features"] == {"events": True, "toy_actions": False, "poll": False}
    assert body["toy_consent"] is False
    assert FAKE_TOKEN not in r.text and ACCESS_KEY not in r.text
    assert fake.requests == []  # minting touches nothing upstream


def test_session_attempts_are_rate_limited(make_client):
    c = make_client(rosemate=FakeRosemate(), **ON)
    codes = [c.post("/rosemate/session", json={"access_key": "x" * 20}, headers=ORIGIN).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_session_capacity(make_client):
    c = make_client(rosemate=FakeRosemate(), rosemate_max_sessions=1, **ON)
    assert open_session(c).status_code == 200
    assert open_session(c).json() == {"error": "at_capacity"}


def test_tokens_are_scoped(make_client):
    c = make_client(rosemate=FakeRosemate(), **TOYS)
    rm = open_session(c).json()["token"]
    # An integration token is not a /decide token...
    r = c.post("/decide", json={"token": rm, "state": "x", "questions": {"q": {"type": "noul", "instructions": "?"}}})
    assert r.status_code == 401
    # ...and a voice-session token is not an integration token.
    voice = tokens.mint(BASE.hmac_secret, "live_123", int(time.time()) + 60)
    assert action(c, voice, STOP_TOOL).json() == {"error": "invalid_token"}
    assert action(c, "garbage", STOP_TOOL).status_code == 401


def test_close_revokes(make_client):
    c = make_client(rosemate=FakeRosemate(), **ON)
    token = open_session(c).json()["token"]
    assert c.post("/rosemate/close", json={"token": token}, headers=ORIGIN).json() == {"closed": True}
    assert action(c, token, STOP_TOOL).status_code == 401


def test_expired_session_is_rejected(make_client):
    c = make_client(rosemate=FakeRosemate(), rosemate_session_ttl_s=-1, **ON)
    token = open_session(c).json()["token"]
    assert action(c, token, STOP_TOOL).status_code == 401


# -- outbound toy actions ----------------------------------------------------------


def test_toy_actions_off_by_operator(make_client):
    fake = FakeRosemate()
    c = make_client(rosemate=fake, **ON)
    token = open_session(c, toy_actions=True).json()["token"]
    assert action(c, token, arguments=HAPTIC_ARGS).json() == {"error": "toy_actions_disabled"}
    r = c.post("/rosemate/consent", json={"token": token, "toy_actions": True}, headers=ORIGIN)
    assert r.status_code == 403
    assert fake.requests == []


def test_toy_action_needs_user_consent(make_client):
    fake = FakeRosemate()
    c = make_client(rosemate=fake, **TOYS)
    token = open_session(c).json()["token"]
    assert action(c, token, arguments=HAPTIC_ARGS).json() == {"error": "consent_required"}
    assert fake.requests == []
    r = c.post("/rosemate/consent", json={"token": token, "toy_actions": True}, headers=ORIGIN)
    assert r.json() == {"toy_consent": True}
    r = action(c, token, arguments=HAPTIC_ARGS)
    assert r.status_code == 200 and r.json() == {"ok": True, "tool": HAPTIC_TOOL, "delivery": "queued"}
    assert fake.methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    init = json.loads(fake.requests[0].content)
    assert init["jsonrpc"] == "2.0" and init["params"]["protocolVersion"] == "2024-11-05"
    assert fake.calls == [{"name": HAPTIC_TOOL, "arguments": HAPTIC_ARGS}]
    for req in fake.requests:
        assert req.headers["authorization"].endswith(FAKE_TOKEN)
        assert FAKE_TOKEN not in str(req.url) and not req.url.query
    # Consent can be withdrawn.
    c.post("/rosemate/consent", json={"token": token, "toy_actions": False}, headers=ORIGIN)
    assert action(c, token, arguments=HAPTIC_ARGS).json() == {"error": "consent_required"}
    assert len(fake.calls) == 1


def test_consent_at_session_start(make_client):
    fake = FakeRosemate()
    c = make_client(rosemate=fake, **TOYS)
    body = open_session(c, toy_actions=True).json()
    assert body["toy_consent"] is True and body["features"]["toy_actions"] is True
    assert action(c, body["token"], arguments=HAPTIC_ARGS).status_code == 200


@pytest.mark.parametrize("args", [
    {**HAPTIC_ARGS, "intensity": 61},  # above the operator cap (60)
    {**HAPTIC_ARGS, "intensity": True},
    {**HAPTIC_ARGS, "intensity": 4.5},
    {**HAPTIC_ARGS, "duration_sec": 11},  # above the operator cap (10)
    {**HAPTIC_ARGS, "duration_sec": 0},
    {**HAPTIC_ARGS, "duration_sec": float("nan")},
    {**HAPTIC_ARGS, "action": "explode"},
    {**HAPTIC_ARGS, "pattern": "random"},
    {**HAPTIC_ARGS, "extra": 1},
    {**HAPTIC_ARGS, "reason": "x" * 121},
])
def test_invalid_toy_arguments_never_reach_upstream(make_client, args):
    fake = FakeRosemate()
    c = make_client(rosemate=fake, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    payload = json.dumps({"token": token, "tool": HAPTIC_TOOL, "arguments": args}, allow_nan=True)
    r = c.post("/rosemate/action", content=payload, headers=ORIGIN)
    assert r.status_code == 400
    assert fake.calls == []


def test_validate_haptic_cleans_reason():
    out = validate_haptic({**HAPTIC_ARGS, "reason": "  hi\x00there  "}, 60, 10)
    assert out["reason"] == "hi there"


def test_unknown_tool_rejected(make_client):
    fake = FakeRosemate()
    c = make_client(rosemate=fake, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    assert action(c, token, tool="talk").json() == {"error": "unknown_tool"}
    assert action(c, token, tool="rosemate_delete_account").json() == {"error": "unknown_tool"}
    assert fake.requests == []


def test_toy_rate_limit(make_client):
    fake = FakeRosemate()
    c = make_client(rosemate=fake, rosemate_toy_per_min=1, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    codes = [action(c, token, arguments=HAPTIC_ARGS).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert len(fake.calls) == 2
    # Stop is never blocked by the toy rate limit.
    assert action(c, token, STOP_TOOL).status_code == 200


def test_stop_needs_no_consent_and_no_operator_toggle(make_client):
    fake = FakeRosemate()
    c = make_client(rosemate=fake, **ON)  # toy actions off
    token = open_session(c).json()["token"]
    r = action(c, token, STOP_TOOL)
    assert r.json() == {"ok": True, "tool": STOP_TOOL, "delivery": "queued"}
    assert fake.calls == [{"name": STOP_TOOL, "arguments": {"reason": "stop requested in open-annie"}}]
    # haptic_trigger with action=stop is the same stop path.
    r = action(c, token, HAPTIC_TOOL, {"action": "stop"})
    assert r.status_code == 200 and len(fake.calls) == 2


def test_stop_falls_back_to_haptic_stop(make_client):
    fake = FakeRosemate(tools=(HAPTIC_TOOL,))
    c = make_client(rosemate=fake, **ON)
    token = open_session(c).json()["token"]
    assert action(c, token, STOP_TOOL).json()["tool"] == HAPTIC_TOOL
    assert fake.calls == [{"name": HAPTIC_TOOL, "arguments": {"action": "stop", "intensity": 0, "duration_sec": 1}}]


def test_undiscovered_tools_are_never_called(make_client):
    fake = FakeRosemate(tools=("something_else",))
    c = make_client(rosemate=fake, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    assert action(c, token, arguments=HAPTIC_ARGS).json() == {"error": "toy_tool_unsupported"}
    assert action(c, token, STOP_TOOL).json() == {"error": "toy_tool_unsupported"}
    assert fake.calls == []


def test_protocol_mismatch(make_client):
    fake = FakeRosemate(protocol="1999-01-01")
    c = make_client(rosemate=fake, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    assert action(c, token, arguments=HAPTIC_ARGS).json() == {"error": "toy_protocol_mismatch"}
    assert fake.calls == []


@pytest.mark.parametrize("status,code", [(500, "toy_upstream_error"), (401, "toy_auth_failed")])
def test_http_errors_redacted(make_client, status, code):
    fake = FakeRosemate(mcp_status=status)
    c = make_client(rosemate=fake, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    r = action(c, token, arguments=HAPTIC_ARGS)
    assert r.status_code == 502 and r.json() == {"error": code}
    assert LEAK not in r.text and FAKE_TOKEN not in r.text


def test_rpc_error_and_is_error(make_client):
    fake = FakeRosemate(call=lambda p: {"error": {"code": -32000, "message": LEAK + FAKE_TOKEN}})
    c = make_client(rosemate=fake, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    r = action(c, token, arguments=HAPTIC_ARGS)
    assert r.json() == {"error": "toy_rpc_error"} and LEAK not in r.text

    fake2 = FakeRosemate(call=lambda p: {"content": [{"type": "text", "text": LEAK}], "isError": True})
    c2 = make_client(rosemate=fake2, **TOYS)
    token2 = open_session(c2, toy_actions=True).json()["token"]
    r = action(c2, token2, arguments=HAPTIC_ARGS)
    assert r.json() == {"error": "toy_tool_error"} and LEAK not in r.text


def test_malformed_rpc_response(make_client):
    fake = FakeRosemate(call=lambda p: httpx.Response(200, text="<html>" + LEAK))
    c = make_client(rosemate=fake, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    r = action(c, token, arguments=HAPTIC_ARGS)
    assert r.json() == {"error": "toy_upstream_error"} and LEAK not in r.text


def test_sse_framed_rpc_response(make_client):
    def framed(p):
        body = json.dumps({"jsonrpc": "2.0", "id": fake.last_id, "result": {"content": [], "isError": False}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=f"event: message\ndata: {body}\n\n")

    fake = FakeRosemate(call=framed)
    c = make_client(rosemate=fake, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    assert action(c, token, arguments=HAPTIC_ARGS).status_code == 200


def test_timeout_is_not_retried(make_client):
    def slow(p):
        return httpx.ReadTimeout("slow")

    fake = FakeRosemate(call=slow)
    c = make_client(rosemate=fake, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    r = action(c, token, arguments=HAPTIC_ARGS)
    assert r.status_code == 504 and r.json() == {"error": "toy_timeout", "outcome": "unknown"}
    assert len(fake.calls) == 1  # one attempt only: a retry could actuate twice


def test_total_deadline(make_client):
    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200)

    fake = FakeRosemate()
    fake.transport = httpx.MockTransport(hang)
    c = make_client(rosemate=fake, rosemate_timeout_s=0.05, **TOYS)
    token = open_session(c, toy_actions=True).json()["token"]
    t0 = time.perf_counter()
    r = action(c, token, arguments=HAPTIC_ARGS)
    assert r.status_code == 504 and time.perf_counter() - t0 < 1.0


def test_device_status_redacted(make_client):
    text = f"connected: true, battery 80%, token={FAKE_TOKEN}, {FAKE_TOKEN}"
    fake = FakeRosemate(call=lambda p: {"content": [{"type": "text", "text": text}], "isError": False})
    c = make_client(rosemate=fake, **ON)
    token = open_session(c).json()["token"]
    r = action(c, token, STATUS_TOOL)
    assert r.status_code == 200 and FAKE_TOKEN not in r.text
    assert "battery 80%" in r.json()["text"]
    assert fake.calls == [{"name": STATUS_TOOL, "arguments": {}}]


def test_body_limits(make_client):
    c = make_client(rosemate=FakeRosemate(), **ON)
    assert c.post("/rosemate/session", content=b"x" * 9000, headers=ORIGIN).status_code == 413
    assert c.post("/rosemate/session", content=b"{nope", headers=ORIGIN).status_code == 400
    assert c.post("/rosemate/unknown", json={}, headers=ORIGIN).status_code == 404


# -- inbound events ------------------------------------------------------------------


def test_normalize_command():
    now = time.time()
    cmd, why = normalize_command(command(), now, 30)
    assert why is None
    assert cmd == {"id": "cmd_1", "type": "haptic_trigger", "timestamp": cmd["timestamp"], "action": "vibrate",
                   "intensity": 50, "duration_sec": 5.0, "pattern": "steady"}
    assert "reason" not in cmd  # untrusted text never travels on
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    assert normalize_command(command(timestamp=iso), now, 30)[1] is None
    assert normalize_command(command(timestamp=now), now, 30)[1] is None
    assert normalize_command(command(type="stop", action=None), now, 30)[0]["type"] == "stop"
    assert normalize_command(command(action="stop"), now, 30)[0]["type"] == "stop"
    assert normalize_command(command(timestamp=(now - 60) * 1000), now, 30)[1] == "stale"
    assert normalize_command(command(timestamp=(now + 600) * 1000), now, 30)[1] == "malformed"
    assert normalize_command(command(id="bad id!"), now, 30)[1] == "malformed"
    assert normalize_command(command(type="shell"), now, 30)[1] == "unknown_type"
    assert normalize_command(command(action="talk"), now, 30)[1] == "unknown_action"
    assert normalize_command(command(intensity=True), now, 30)[1] == "malformed"
    assert normalize_command(command(intensity=101), now, 30)[1] == "malformed"
    assert normalize_command(command(pattern="x"), now, 30)[1] == "malformed"
    assert normalize_command(command(timestamp="yesterday"), now, 30)[1] == "malformed"
    assert normalize_command("nope", now, 30)[1] == "malformed"


def test_seen_ids_bounded_with_ttl():
    t = [0.0]
    seen = SeenIds(lambda: t[0], ttl_s=10, max_items=3)
    assert seen.add("a") and not seen.add("a")
    for k in "bcd":
        seen.add(k)
    assert "a" not in seen and len(seen._seen) == 3
    t[0] = 11
    assert seen.add("b")  # expired, so it is new again


async def _lines(text):
    async def chunks():
        for i in range(0, len(text), 7):
            yield text[i:i + 7]
    return [x async for x in iter_sse(bounded_lines(chunks()))]


async def test_sse_parsing_skips_heartbeats():
    events = await _lines(sse(command()) + "data: a\r\ndata: b\r\n\r\n")
    assert [e for e, _ in events] == ["connected", "haptic_command", "message"]
    assert events[-1][1] == "a\nb"


async def test_sse_line_bound():
    async def huge():
        yield "data: " + "x" * 40_000

    with pytest.raises(RosemateError):
        [x async for x in iter_sse(bounded_lines(huge()))]


def make_hub(fake, **overrides):
    s = replace(BASE, **dict(ON, **overrides))
    client = httpx.AsyncClient(transport=fake.transport)
    hub = EventHub(s, client, "https://rosemate.test" + EVENTS_PATH,
                   "https://rosemate.test" + POLL_PATH if s.rosemate_poll else None,
                   time.time, sleep=lambda d: asyncio.sleep(0.001))
    hub.reconnect_delay = 0.001
    return hub, client


async def drain(q, until, limit=200):
    out = []
    for _ in range(limit):
        out.append(await asyncio.wait_for(q.get(), 2))
        if until(out):
            break
    return out


async def wait_for(cond, timeout=2.0):
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout
        await asyncio.sleep(0.005)


def take_all(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_hub_dedupes_across_reconnects_and_drops_secret():
    first = command("cmd_a")
    fake = FakeRosemate(events=lambda n: sse(first, command("cmd_b")) if n < 4 else sse(first))
    hub, client = make_hub(fake)
    q = hub.subscribe()
    await wait_for(lambda: fake.streams >= 5)
    msgs = take_all(q)
    hub.unsubscribe(q)
    await client.aclose()
    cmds = [m["command"]["id"] for m in msgs if m["type"] == "command"]
    assert cmds == ["cmd_a", "cmd_b"]  # each once, though streamed on every reconnect
    assert FAKE_TOKEN not in json.dumps(msgs)
    assert {"type": "status", "state": "connected", "detail": None} in msgs
    assert all(m["type"] in ("status", "command", "rejected") for m in msgs)
    assert hub._task is None and hub.state == "idle"  # disposed with the last subscriber
    assert all(r.method == "GET" for r in fake.requests)  # inbound commands never call tools upstream


async def test_hub_rejects_bad_commands_visibly():
    stale = command("cmd_old", timestamp=now_ms() - 120_000)
    fake = FakeRosemate(events=lambda n: sse(stale, command("cmd_x", type="exec")) + "event: haptic_command\ndata: {oops\n\n")
    hub, client = make_hub(fake)
    q = hub.subscribe()
    msgs = await drain(q, lambda out: sum(m["type"] == "rejected" for m in out) >= 3)
    hub.unsubscribe(q)
    await client.aclose()
    assert [m["reason"] for m in msgs if m["type"] == "rejected"] == ["stale", "unknown_type", "malformed"]
    assert not any(m["type"] == "command" for m in msgs)


async def test_hub_reports_upstream_failure_and_does_not_poll_by_default():
    fake = FakeRosemate(events=lambda n: httpx.Response(401, text=FAKE_TOKEN),
                        poll=lambda: {"success": True, "command": command("cmd_p")})
    hub, client = make_hub(fake)
    q = hub.subscribe()
    msgs = await drain(q, lambda out: any(m.get("detail") == "upstream_unauthorized" for m in out))
    hub.unsubscribe(q)
    await client.aclose()
    assert msgs[-1] == {"type": "status", "state": "reconnecting", "detail": "upstream_unauthorized"}
    assert not any(r.url.path == POLL_PATH for r in fake.requests)
    assert FAKE_TOKEN not in json.dumps(msgs)


async def test_hub_poll_fallback_when_enabled_and_deduped():
    same = command("cmd_same")
    fake = FakeRosemate(events=lambda n: sse(same) if n == 1 else httpx.Response(503),
                        poll=lambda: {"success": True, "command": same})
    hub, client = make_hub(fake, rosemate_poll=True)
    q = hub.subscribe()
    await wait_for(lambda: sum(r.url.path == POLL_PATH for r in fake.requests) >= 2)
    msgs = take_all(q)
    hub.unsubscribe(q)
    await client.aclose()
    # Delivered once, although both SSE and the mailbox carried it.
    assert [m["command"]["id"] for m in msgs if m["type"] == "command"] == ["cmd_same"]


async def test_hub_queue_is_bounded():
    hub, client = make_hub(FakeRosemate())
    q = asyncio.Queue(maxsize=2)
    hub.subscribers.add(q)
    for i in range(5):
        hub.publish({"n": i})
    assert [q.get_nowait()["n"], q.get_nowait()["n"]] == [3, 4]
    await client.aclose()


def test_ws_events_end_to_end(make_client):
    fake = FakeRosemate(events=lambda n: sse(command("cmd_ws")))
    c = make_client(rosemate=fake, **TOYS)
    c.app.state.broker.rosemate.hub.reconnect_delay = 0.01
    token = open_session(c).json()["token"]
    seen = []
    with c.websocket_connect("/rosemate/events", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"token": token}))
        for _ in range(50):
            msg = ws.receive_json()
            seen.append(msg)
            if msg["type"] == "command":
                break
    assert seen[-1]["command"]["id"] == "cmd_ws" and seen[-1]["command"]["action"] == "vibrate"
    assert FAKE_TOKEN not in json.dumps(seen) and "ignore me" not in json.dumps(seen)
    assert fake.calls == [] and "tools/call" not in fake.methods  # no feedback loop


def test_ws_requires_token_message(make_client):
    c = make_client(rosemate=FakeRosemate(), **ON)
    with c.websocket_connect("/rosemate/events", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"token": "nope"}))
        assert ws.receive_json() == {"type": "error", "error": "invalid_token"}
    with pytest.raises(Exception):
        with c.websocket_connect("/rosemate/events", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_text()
