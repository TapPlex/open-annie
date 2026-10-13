"""Optional Rosemate MCP integration: an outbound action client and an inbound event observer.

Off unless ANNIE_ROSEMATE_MCP_URL, ANNIE_ROSEMATE_MCP_TOKEN and ANNIE_ROSEMATE_ACCESS_KEY are
all set (docs/rosemate.md). Two separate upstream surfaces:

- ``POST <mcp url>``: MCP JSON-RPC 2.0 (initialize, notifications/initialized, tools/list,
  tools/call). Used only for allowlisted, user-consented toy actions and the always-available stop.
- ``GET <base>/api/mcp/bridge/events``: an SSE stream of ``haptic_command`` events. The broker
  reads it with the token in a header, rebuilds every event from allowlisted fields and fans the
  result out to authorized browsers over ``WS /rosemate/events``. Inbound commands only ever move
  the avatar; nothing here sends a command back upstream because one arrived.

The configured token is a single-account credential. Browsers never see it: they trade the
operator's access key for a short, scoped HMAC token (signed with a key derived for this
integration only, so it is useless on /decide and vice versa).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import math
import re
import secrets
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import tokens
from .config import VERSION, Settings
from .decide import TokenBucket

log = logging.getLogger("annie.rosemate")

PROTOCOL_VERSION = "2024-11-05"
HAPTIC_TOOL = "rosemate_haptic_trigger"
STOP_TOOL = "rosemate_toy_stop"
STATUS_TOOL = "rosemate_get_device_status"
KNOWN_TOOLS = frozenset({HAPTIC_TOOL, STOP_TOOL, STATUS_TOOL})
ACTIONS = ("vibrate", "pulse", "wave", "tease", "escalate", "stop")
PATTERNS = ("steady", "pulse", "wave", "tease", "escalate", "climax")
COMMAND_TYPES = ("haptic_trigger", "stop")

MAX_BODY_BYTES = 8 * 1024  # integration requests are tiny
MAX_RPC_BYTES = 256 * 1024
MAX_SSE_LINE = 16 * 1024
MAX_SSE_DATA = 16 * 1024
QUEUE_MAX = 32  # per browser socket; oldest dropped first (late avatar cues are worthless)
SOCKETS_PER_SESSION = 2
AUTH_WAIT_S = 5.0
FUTURE_SKEW_S = 60.0
READY_TTL_S = 600.0
STATUS_TEXT_MAX = 1000

ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,128}$")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Anything that looks like a credential assignment in upstream text.
SECRETISH_RE = re.compile(r"(?i)\b(token|key|secret|authorization|bearer)(\s*[\"'=: ]\s*)[^\s\"'&,;}]+")

# Error code -> HTTP status. Codes are fixed strings; upstream bodies are never forwarded.
STATUS = {
    "rosemate_unavailable": 503,
    "invalid_request": 400,
    "invalid_action": 400,
    "unknown_tool": 400,
    "invalid_token": 401,
    "invalid_access_key": 401,
    "toy_actions_disabled": 403,
    "consent_required": 403,
    "toy_busy": 409,
    "too_large": 413,
    "rate_limited": 429,
    "at_capacity": 429,
    "toy_upstream_error": 502,
    "toy_auth_failed": 502,
    "toy_rpc_error": 502,
    "toy_tool_error": 502,
    "toy_tool_unsupported": 502,
    "toy_protocol_mismatch": 502,
    "toy_timeout": 504,
}


class RosemateError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _err(code: str, **extra: Any) -> tuple[int, dict]:
    return STATUS.get(code, 502), {"error": code, **extra}


def check_url(url: Optional[str]) -> Optional[str]:
    """A configured upstream URL, or None. https only (http for localhost), and never with a
    query, fragment or userinfo: credentials go in the Authorization header, not the URL."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return None
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1"):
        return None
    if parts.query or parts.fragment or parts.username or parts.password:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def bridge_url(mcp_url: Optional[str], leaf: str) -> Optional[str]:
    """`https://host/rosemate/mcp` -> `https://host/rosemate/api/mcp/bridge/<leaf>`."""
    if not mcp_url:
        return None
    parts = urlsplit(mcp_url)
    path = parts.path.rstrip("/")
    if not path.endswith("/mcp"):
        return None
    return urlunsplit((parts.scheme, parts.netloc, f"{path[:-4]}/api/mcp/bridge/{leaf}", "", ""))


def redact(text: str, secret: Optional[str]) -> str:
    if secret:
        text = text.replace(secret, "[redacted]")
    return SECRETISH_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)


def clean_text(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("text")
    value = CTRL_RE.sub(" ", value).strip()
    if len(value) > limit:
        raise ValueError("text too long")
    return value or None


def _number(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _integer(v: Any) -> Optional[int]:
    n = _number(v)
    return int(n) if n is not None and n.is_integer() else None


def validate_haptic(args: Any, max_intensity: int, max_duration_s: float) -> dict:
    """Arguments for rosemate_haptic_trigger, within the operator's caps. Raises ValueError."""
    if not isinstance(args, dict):
        raise ValueError("arguments")
    if set(args) - {"action", "intensity", "duration_sec", "pattern", "reason"}:
        raise ValueError("unknown argument")
    action = args.get("action")
    if action not in ACTIONS:
        raise ValueError("action")
    intensity = _integer(args.get("intensity"))
    if intensity is None or not 0 <= intensity <= min(100, max_intensity):
        raise ValueError("intensity")
    duration = _number(args.get("duration_sec"))
    if duration is None or not 1 <= duration <= min(120.0, max_duration_s):
        raise ValueError("duration_sec")
    out: dict = {"action": action, "intensity": intensity, "duration_sec": duration}
    pattern = args.get("pattern")
    if pattern is not None:
        if pattern not in PATTERNS:
            raise ValueError("pattern")
        out["pattern"] = pattern
    reason = clean_text(args.get("reason"), 120)
    if reason:
        out["reason"] = reason
    return out


def parse_timestamp(v: Any) -> Optional[float]:
    """Unix seconds from epoch seconds, epoch milliseconds or an ISO 8601 string."""
    n = _number(v)
    if n is not None:
        return n / 1000 if n > 1e11 else n
    if isinstance(v, str) and 0 < len(v) <= 64:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def normalize_command(raw: Any, now: float, max_age_s: float) -> tuple[Optional[dict], Optional[str]]:
    """Rebuild an upstream haptic_command from allowlisted fields: (command, None) or (None, reason).

    `reason` text from upstream is dropped: it is untrusted and the avatar never needs it.
    """
    if not isinstance(raw, dict):
        return None, "malformed"
    cid = raw.get("id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        return None, "malformed"
    ctype = raw.get("type")
    if ctype not in COMMAND_TYPES:
        return None, "unknown_type"
    ts = parse_timestamp(raw.get("timestamp"))
    if ts is None:
        return None, "malformed"
    if now - ts > max_age_s:
        return None, "stale"
    if ts - now > FUTURE_SKEW_S:
        return None, "malformed"
    out: dict = {"id": cid, "type": ctype, "timestamp": round(ts, 3)}
    action = raw.get("action")
    if ctype == "stop" or action == "stop":
        out.update(type="stop", action="stop")
        return out, None
    if action not in ACTIONS:
        return None, "unknown_action"
    intensity = _integer(raw.get("intensity"))
    duration = _number(raw.get("duration_sec"))
    if intensity is None or not 0 <= intensity <= 100 or duration is None or not 0 < duration <= 120:
        return None, "malformed"
    pattern = raw.get("pattern")
    if pattern is not None and pattern not in PATTERNS:
        return None, "malformed"
    out.update(action=action, intensity=intensity, duration_sec=duration, pattern=pattern)
    return out, None


class SeenIds:
    """Bounded, TTL'd set of command ids: dedupes across stream reconnects and transport switches."""

    def __init__(self, clock: Callable[[], float], ttl_s: float = 600.0, max_items: int = 1024):
        self.clock, self.ttl_s, self.max_items = clock, ttl_s, max_items
        self._seen: OrderedDict[str, float] = OrderedDict()

    def _prune(self) -> None:
        cutoff = self.clock() - self.ttl_s
        while self._seen and next(iter(self._seen.values())) < cutoff:
            self._seen.popitem(last=False)

    def __contains__(self, cid: str) -> bool:
        self._prune()
        return cid in self._seen

    def add(self, cid: str) -> bool:
        """True if new."""
        if cid in self:
            return False
        self._seen[cid] = self.clock()
        while len(self._seen) > self.max_items:
            self._seen.popitem(last=False)
        return True


async def bounded_lines(chunks: AsyncIterator[str]) -> AsyncIterator[str]:
    """Split a text stream into lines without ever buffering more than MAX_SSE_LINE."""
    buf = ""
    async for chunk in chunks:
        buf += chunk
        while True:
            i = buf.find("\n")
            if i < 0:
                break
            line, buf = buf[:i], buf[i + 1:]
            yield line.rstrip("\r")
        if len(buf) > MAX_SSE_LINE:
            raise RosemateError("toy_upstream_error")
    if buf:
        yield buf.rstrip("\r")


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """(event, data) pairs from SSE lines. Comment lines (heartbeats) are skipped."""
    event, data, size = "message", [], 0
    async for line in lines:
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data, size = "message", [], 0
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value[:64]
        elif name == "data":
            size += len(value)
            if size > MAX_SSE_DATA:
                raise RosemateError("toy_upstream_error")
            data.append(value)
    if data:
        yield event, "\n".join(data)


def _rpc_message(resp: httpx.Response, rpc_id: int) -> dict:
    """The JSON-RPC response for `rpc_id`, from a JSON body or an SSE-framed body."""
    if len(resp.content) > MAX_RPC_BYTES:
        raise RosemateError("toy_upstream_error")
    ctype = resp.headers.get("content-type", "")
    try:
        if "text/event-stream" in ctype:
            for block in resp.text.split("\n\n"):
                data = "\n".join(
                    ln[5:].lstrip() for ln in block.splitlines() if ln.startswith("data:")
                )
                if data:
                    msg = json.loads(data)
                    if isinstance(msg, dict) and msg.get("id") == rpc_id:
                        return msg
            raise ValueError("no response")
        msg = resp.json()
    except ValueError:
        raise RosemateError("toy_upstream_error") from None
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or msg.get("id") != rpc_id:
        raise RosemateError("toy_upstream_error")
    return msg


class McpClient:
    """Minimal MCP JSON-RPC client over HTTP POST. Never retries: a repeated tools/call could
    actuate hardware twice, so every failure is reported once and the caller decides."""

    def __init__(self, url: str, client: httpx.AsyncClient, clock: Callable[[], float]):
        self.url, self.client, self.clock = url, client, clock
        self.tools: Optional[frozenset] = None
        self.ready_until = 0.0
        self.session_header: Optional[str] = None
        self._ids = itertools.count(1)
        self._init_lock = asyncio.Lock()

    def reset(self) -> None:
        self.tools, self.ready_until, self.session_header = None, 0.0, None

    async def _post(self, payload: dict) -> httpx.Response:
        headers = {"Mcp-Session-Id": self.session_header} if self.session_header else None
        try:
            return await self.client.post(self.url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise RosemateError("toy_timeout") from None
        except httpx.HTTPError as exc:
            # Class name only: httpx messages can carry the URL.
            log.info("rosemate mcp transport error: %s", type(exc).__name__)
            raise RosemateError("toy_upstream_error") from None

    async def rpc(self, method: str, params: Optional[dict] = None) -> dict:
        rpc_id = next(self._ids)
        payload: dict = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = await self._post(payload)
        if resp.status_code != 200:
            log.warning("rosemate mcp %s status=%s", method, resp.status_code)
            raise RosemateError("toy_auth_failed" if resp.status_code in (401, 403) else "toy_upstream_error")
        sid = resp.headers.get("mcp-session-id")
        if sid and len(sid) <= 256 and sid.isprintable():
            self.session_header = sid
        msg = _rpc_message(resp, rpc_id)
        if "error" in msg:
            code = msg["error"].get("code") if isinstance(msg["error"], dict) else None
            log.info("rosemate mcp %s rpc error code=%s", method, code if isinstance(code, int) else "?")
            raise RosemateError("toy_rpc_error")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise RosemateError("toy_upstream_error")
        return result

    async def notify(self, method: str) -> None:
        resp = await self._post({"jsonrpc": "2.0", "method": method})
        if resp.status_code >= 300:
            raise RosemateError("toy_upstream_error")

    async def ensure_ready(self) -> frozenset:
        """initialize -> notifications/initialized -> tools/list; cached for READY_TTL_S."""
        if self.tools is not None and self.clock() < self.ready_until:
            return self.tools
        async with self._init_lock:
            if self.tools is not None and self.clock() < self.ready_until:
                return self.tools
            self.reset()
            result = await self.rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "open-annie", "version": VERSION},
            })
            if result.get("protocolVersion") != PROTOCOL_VERSION:
                raise RosemateError("toy_protocol_mismatch")
            await self.notify("notifications/initialized")
            found: set[str] = set()
            cursor = None
            for _ in range(3):
                listed = await self.rpc("tools/list", {"cursor": cursor} if cursor else {})
                for tool in listed.get("tools") or []:
                    if isinstance(tool, dict) and tool.get("name") in KNOWN_TOOLS:
                        found.add(tool["name"])
                cursor = listed.get("nextCursor")
                if not isinstance(cursor, str) or not cursor:
                    break
            self.tools = frozenset(found)
            self.ready_until = self.clock() + READY_TTL_S
            return self.tools

    async def call(self, name: str, arguments: dict) -> str:
        """tools/call once. Returns the first text content; raises on isError."""
        tools = await self.ensure_ready()
        if name not in tools:
            raise RosemateError("toy_tool_unsupported")
        try:
            result = await self.rpc("tools/call", {"name": name, "arguments": arguments})
        except RosemateError:
            self.reset()  # re-initialize next time; this call is NOT retried
            raise
        if result.get("isError") is True:
            raise RosemateError("toy_tool_error")
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                return item["text"]
        return ""


class EventHub:
    """One upstream bridge-events subscriber shared by every authorized browser socket.

    Runs only while at least one socket listens. Streams end about every 30 s upstream, so a
    normal end reconnects quickly; failures back off up to 30 s. With ANNIE_ROSEMATE_POLL=1
    (explicit only: the poll mailbox is consuming and competes with the Rosemate PWA) each
    failed stream attempt is followed by one poll. Ids are deduped across all of it.
    """

    def __init__(self, s: Settings, client: httpx.AsyncClient, events_url: str,
                 poll_url: Optional[str], clock: Callable[[], float],
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.s, self.client, self.events_url, self.poll_url = s, client, events_url, poll_url
        self.clock, self.sleep = clock, sleep
        self.seen = SeenIds(clock)
        self.subscribers: set[asyncio.Queue] = set()
        self.state, self.detail = "idle", None
        self.rejected: dict[str, int] = {}
        self.reconnect_delay = 0.5
        self._task: Optional[asyncio.Task] = None

    def status_message(self) -> dict:
        return {"type": "status", "state": self.state, "detail": self.detail}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        q.put_nowait(self.status_message())
        self.subscribers.add(q)
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
        if not self.subscribers and self._task is not None:
            self._task.cancel()
            self._task = None
            self.state, self.detail = "idle", None

    def publish(self, msg: dict) -> None:
        for q in list(self.subscribers):
            if q.full():
                with suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(msg)

    def _set_state(self, state: str, detail: Optional[str] = None) -> None:
        if (state, detail) != (self.state, self.detail):
            self.state, self.detail = state, detail
            self.publish(self.status_message())

    def ingest(self, raw: Any) -> Optional[dict]:
        cid = raw.get("id") if isinstance(raw, dict) else None
        if isinstance(cid, str) and cid in self.seen:
            return None  # already delivered (SSE and the mailbox can both carry it)
        cmd, why = normalize_command(raw, self.clock(), self.s.rosemate_event_max_age_s)
        if cmd is None:
            self.rejected[why] = self.rejected.get(why, 0) + 1
            self.publish({"type": "rejected", "reason": why})
            return None
        if not self.seen.add(cmd["id"]):
            return None
        self.publish({"type": "command", "command": cmd})
        return cmd

    async def _stream(self) -> None:
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        async with self.client.stream("GET", self.events_url, headers=headers) as resp:
            if resp.status_code != 200:
                log.warning("rosemate events status=%s", resp.status_code)
                raise RosemateError("upstream_unauthorized" if resp.status_code in (401, 403) else "upstream_http_error")
            self._set_state("connected")
            async for event, data in iter_sse(bounded_lines(resp.aiter_text())):
                # The `connected` handshake may echo the credential: it is never parsed or forwarded.
                if event != "haptic_command":
                    continue
                try:
                    raw = json.loads(data)
                except ValueError:
                    raw = None
                self.ingest(raw)

    async def poll_once(self) -> None:
        if not self.poll_url:
            return
        try:
            resp = await self.client.get(self.poll_url, headers={"Accept": "application/json"})
            body = resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as exc:
            log.info("rosemate poll failed: %s", type(exc).__name__)
            return
        if isinstance(body, dict) and body.get("success") is True and isinstance(body.get("command"), dict):
            self.ingest(body["command"])

    async def _run(self) -> None:
        failures = 0
        while self.subscribers:
            if failures == 0 and self.state != "connected":
                self._set_state("connecting")
            try:
                await self._stream()
                failures = 0
            except asyncio.CancelledError:
                raise
            except RosemateError as exc:
                failures += 1
                self._set_state("reconnecting", exc.code)
            except Exception as exc:  # transport errors, read timeouts
                failures += 1
                log.info("rosemate events error: %s", type(exc).__name__)
                self._set_state("reconnecting", "timeout" if isinstance(exc, httpx.TimeoutException) else "transport")
            if failures and self.s.rosemate_poll:
                await self.poll_once()
            await self.sleep(self.reconnect_delay if not failures else min(30.0, 2.0 ** min(failures, 5)))

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self.subscribers.clear()


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    access_key: str = Field(min_length=1, max_length=256)
    toy_actions: bool = False


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str = Field(max_length=512)
    toy_actions: bool


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str = Field(max_length=512)
    tool: str = Field(max_length=64)
    arguments: dict = Field(default_factory=dict)


@dataclass
class IntegrationSession:
    expires_at: int
    toy_consent: bool
    toy_bucket: TokenBucket
    safe_bucket: TokenBucket  # stop and status: generous, never gated by consent
    queues: set = field(default_factory=set)


class Rosemate:
    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        s = self.s = settings
        self.clock = clock
        self.mcp_url = check_url(s.rosemate_mcp_url)
        if s.rosemate_mcp_url and not self.mcp_url:
            log.error("ANNIE_ROSEMATE_MCP_URL rejected (https, no query or credentials); integration off")
        self.events_url = check_url(s.rosemate_events_url) if s.rosemate_events_url else bridge_url(self.mcp_url, "events")
        self.poll_url = bridge_url(self.mcp_url, "poll")
        key_ok = bool(s.rosemate_access_key) and len(s.rosemate_access_key or "") >= 16
        if s.rosemate_access_key and not key_ok:
            log.error("ANNIE_ROSEMATE_ACCESS_KEY is shorter than 16 characters; integration off")
        self.enabled = bool(self.mcp_url and self.events_url and s.rosemate_mcp_token and key_ok)
        # Tokens for this integration are signed with their own derived key (scope separation).
        self._key = hmac.new(s.hmac_secret, b"open-annie/rosemate/v1", hashlib.sha256).digest()
        self.sessions: dict[str, IntegrationSession] = {}
        self._attempts: dict[str, TokenBucket] = {}
        self._toy_lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None
        self.mcp: Optional[McpClient] = None
        self.hub: Optional[EventHub] = None
        if self.enabled:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {s.rosemate_mcp_token}",
                         "User-Agent": f"open-annie/{VERSION}"},
                timeout=httpx.Timeout(s.rosemate_timeout_s, connect=min(5.0, s.rosemate_timeout_s), read=45.0),
                follow_redirects=False,  # never carry the Authorization header anywhere else
                transport=transport,
            )
            self.mcp = McpClient(self.mcp_url, self._client, clock)
            self.hub = EventHub(s, self._client, self.events_url, self.poll_url if s.rosemate_poll else None, clock, sleep)

    # -- sessions --------------------------------------------------------------

    def _prune(self) -> None:
        now = self.clock()
        for sid in [k for k, v in self.sessions.items() if v.expires_at <= now]:
            self._drop(sid)

    def _drop(self, sid: str) -> None:
        sess = self.sessions.pop(sid, None)
        if sess:
            for q in list(sess.queues):
                with suppress(asyncio.QueueFull):
                    q.put_nowait({"type": "status", "state": "closed", "detail": "session_ended"})

    def authorize(self, token: Any) -> Optional[str]:
        try:
            sid = tokens.verify(self._key, token, self.clock())
        except tokens.TokenError:
            return None
        sess = self.sessions.get(sid)
        return sid if sess and sess.expires_at > self.clock() else None

    def _features(self) -> dict:
        return {"events": True, "toy_actions": self.s.rosemate_toy_actions, "poll": self.s.rosemate_poll}

    def open_session(self, payload: Any, ip: str) -> tuple[int, dict]:
        if len(self._attempts) > 4096:
            self._attempts.clear()
        bucket = self._attempts.setdefault(ip, TokenBucket(5 / 60, 5, self.clock))
        if not bucket.take():
            return _err("rate_limited")
        try:
            req = SessionRequest.model_validate(payload)
        except ValidationError:
            return _err("invalid_request")
        if not hmac.compare_digest(req.access_key.encode(), (self.s.rosemate_access_key or "").encode()):
            log.info("rosemate session: wrong access key")
            return _err("invalid_access_key")
        self._prune()
        if len(self.sessions) >= self.s.rosemate_max_sessions:
            return _err("at_capacity")
        sid = f"rms_{secrets.token_hex(8)}"
        expires_at = int(self.clock()) + self.s.rosemate_session_ttl_s
        per_s = max(1, self.s.rosemate_toy_per_min) / 60
        self.sessions[sid] = IntegrationSession(
            expires_at=expires_at,
            toy_consent=bool(req.toy_actions and self.s.rosemate_toy_actions),
            toy_bucket=TokenBucket(per_s, 2, self.clock),
            safe_bucket=TokenBucket(2.0, 4, self.clock),
        )
        return 200, {
            "token": tokens.mint(self._key, sid, expires_at),
            "expires_at": expires_at,
            "features": self._features(),
            "toy_consent": self.sessions[sid].toy_consent,
        }

    def consent(self, payload: Any) -> tuple[int, dict]:
        try:
            req = ConsentRequest.model_validate(payload)
        except ValidationError:
            return _err("invalid_request")
        sid = self.authorize(req.token)
        if sid is None:
            return _err("invalid_token")
        if req.toy_actions and not self.s.rosemate_toy_actions:
            return _err("toy_actions_disabled")
        self.sessions[sid].toy_consent = req.toy_actions
        return 200, {"toy_consent": req.toy_actions}

    def close(self, payload: Any) -> tuple[int, dict]:
        sid = self.authorize(payload.get("token") if isinstance(payload, dict) else None)
        if sid is None:
            return _err("invalid_token")
        self._drop(sid)
        return 200, {"closed": True}

    # -- outbound actions ------------------------------------------------------

    async def _call(self, name: str, arguments: dict) -> str:
        assert self.mcp is not None
        try:
            async with asyncio.timeout(self.s.rosemate_timeout_s):
                return await self.mcp.call(name, arguments)
        except TimeoutError:
            self.mcp.reset()
            raise RosemateError("toy_timeout") from None

    async def _stop(self) -> tuple[int, dict]:
        assert self.mcp is not None
        try:
            async with asyncio.timeout(self.s.rosemate_timeout_s):
                tools = await self.mcp.ensure_ready()
            if STOP_TOOL in tools:
                await self._call(STOP_TOOL, {"reason": "stop requested in open-annie"})
                tool = STOP_TOOL
            elif HAPTIC_TOOL in tools:
                await self._call(HAPTIC_TOOL, {"action": "stop", "intensity": 0, "duration_sec": 1})
                tool = HAPTIC_TOOL
            else:
                raise RosemateError("toy_tool_unsupported")
        except TimeoutError:
            self.mcp.reset()
            return _err("toy_timeout", outcome="unknown")
        except RosemateError as exc:
            return _err(exc.code, **({"outcome": "unknown"} if exc.code == "toy_timeout" else {}))
        # Accepted by the MCP queue; the Rosemate PWA does the actual hardware work.
        return 200, {"ok": True, "tool": tool, "delivery": "queued"}

    async def action(self, payload: Any) -> tuple[int, dict]:
        try:
            req = ActionRequest.model_validate(payload)
        except ValidationError:
            return _err("invalid_request")
        sid = self.authorize(req.token)
        if sid is None:
            return _err("invalid_token")
        sess = self.sessions[sid]
        tool, args = req.tool, req.arguments
        is_stop = tool == STOP_TOOL or (tool == HAPTIC_TOOL and args.get("action") == "stop")
        if is_stop:
            if not sess.safe_bucket.take():
                return _err("rate_limited")
            return await self._stop()
        if tool == STATUS_TOOL:
            if not sess.safe_bucket.take():
                return _err("rate_limited")
            try:
                text = await self._call(STATUS_TOOL, {})
            except RosemateError as exc:
                return _err(exc.code)
            return 200, {"ok": True, "tool": tool,
                         "text": redact(text, self.s.rosemate_mcp_token)[:STATUS_TEXT_MAX]}
        if tool != HAPTIC_TOOL:
            return _err("unknown_tool")
        if not self.s.rosemate_toy_actions:
            return _err("toy_actions_disabled")
        if not sess.toy_consent:
            return _err("consent_required")
        try:
            clean = validate_haptic(args, self.s.rosemate_max_intensity, self.s.rosemate_max_duration_s)
        except ValueError as exc:
            return _err("invalid_action", field=str(exc))
        if not sess.toy_bucket.take():
            return _err("rate_limited")
        if self._toy_lock.locked():
            return _err("toy_busy")
        async with self._toy_lock:
            try:
                await self._call(HAPTIC_TOOL, clean)
            except RosemateError as exc:
                return _err(exc.code, **({"outcome": "unknown"} if exc.code == "toy_timeout" else {}))
        return 200, {"ok": True, "tool": tool, "delivery": "queued"}

    # -- entry points ------------------------------------------------------------

    async def handle(self, route: str, raw: bytes, ip: str) -> tuple[int, dict]:
        if route not in ("session", "consent", "action", "close"):
            return 404, {"detail": "Not Found"}
        if not self.enabled:
            return _err("rosemate_unavailable")
        if len(raw) > MAX_BODY_BYTES:
            return _err("too_large")
        try:
            payload = json.loads(raw)
        except ValueError:
            return _err("invalid_request")
        if route == "session":
            return self.open_session(payload, ip)
        if route == "consent":
            return self.consent(payload)
        if route == "close":
            return self.close(payload)
        return await self.action(payload)

    async def events_socket(self, ws: Any, origin_ok: bool) -> None:
        """WS /rosemate/events. The first message must be {"token": ...} (never a URL parameter)."""
        if not origin_ok or not self.enabled or self.hub is None:
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            first = await asyncio.wait_for(ws.receive_text(), AUTH_WAIT_S)
            payload = json.loads(first) if len(first) <= 1024 else None
        except Exception:
            payload = None
        sid = self.authorize(payload.get("token") if isinstance(payload, dict) else None)
        sess = self.sessions.get(sid) if sid else None
        if sess is None or len(sess.queues) >= SOCKETS_PER_SESSION:
            with suppress(Exception):
                await ws.send_text(json.dumps({"type": "error", "error": "invalid_token" if sess is None else "at_capacity"}))
                await ws.close(code=4401 if sess is None else 4429)
            return
        q = self.hub.subscribe()
        sess.queues.add(q)

        async def reader() -> None:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    return

        async def pump() -> None:
            while self.authorize(payload["token"]) is not None:
                try:
                    msg = await asyncio.wait_for(q.get(), 5.0)
                except asyncio.TimeoutError:
                    continue
                await ws.send_text(json.dumps(msg))
            with suppress(Exception):
                await ws.send_text(json.dumps({"type": "status", "state": "closed", "detail": "session_ended"}))

        tasks = [asyncio.ensure_future(reader()), asyncio.ensure_future(pump())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except Exception:
            pass
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with suppress(BaseException):
                    await t
            sess.queues.discard(q)
            self.hub.unsubscribe(q)
            with suppress(Exception):
                await ws.close()

    async def aclose(self) -> None:
        if self.hub is not None:
            await self.hub.aclose()
        if self._client is not None:
            await self._client.aclose()
