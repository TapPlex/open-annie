"""The broker contract (docs/contracts.md) as framework-light handlers plus a FastAPI app.

`Broker` holds all state and returns (status, body) pairs, so the FastAPI app
here and the fal App in fal_app.py share every line of logic.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, Callable, Optional, Union

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import tokens
from .budget import Budget, Rejected
from .config import VERSION, Settings
from .decide import DecideClient, DecideInvalid, DecideUnavailable
from .rosemate import Rosemate
from .voice import GeminiLiveClient, setup_message

log = logging.getLogger("annie")

MAX_BODY_BYTES = 64 * 1024
WS_MAX_INFLIGHT = 4


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str = Field(max_length=512)
    state: Union[str, dict, list]
    questions: dict[str, dict] = Field(min_length=1, max_length=64)


def _err(code: str, **extra: Any) -> dict:
    return {"error": code, **extra}


def origin_matcher(patterns: tuple[str, ...]) -> Optional[str]:
    """Regex for allowed origins; None means allow any. `*` inside a pattern matches a port/host part."""
    if not patterns or "*" in patterns:
        return None
    return "^(" + "|".join(re.escape(p).replace(r"\*", "[^/]*") for p in patterns) + ")$"


class Broker:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        gemini_connect: Optional[Callable] = None,
        typesafe_transport: Optional[httpx.AsyncBaseTransport] = None,
        rosemate_transport: Optional[httpx.AsyncBaseTransport] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.s = settings or Settings.from_env()
        self.clock = clock
        self.budget = Budget(self.s, clock)
        self.voice = GeminiLiveClient(self.s, gemini_connect)
        self.decider = DecideClient(self.s, typesafe_transport)
        self.rosemate = Rosemate(self.s, rosemate_transport, clock)
        self.origin_regex = origin_matcher(self.s.allowed_origins)
        self._origin_re = re.compile(self.origin_regex) if self.origin_regex else None

    async def start(self) -> None:
        self.decider.start_warm()

    async def aclose(self) -> None:
        await self.decider.aclose()
        await self.rosemate.aclose()

    # -- request context -----------------------------------------------------

    def origin_allowed(self, origin: Optional[str]) -> bool:
        # Non-browser callers send no Origin; the budget and per-IP limits still apply.
        if origin is None or self._origin_re is None:
            return True
        return bool(self._origin_re.match(origin))

    def client_ip(self, headers: Any, peer: Optional[str]) -> str:
        if self.s.ip_header and (value := headers.get(self.s.ip_header)):
            return value.split(",")[0].strip() or "unknown"
        return peer or "unknown"

    # -- routes ----------------------------------------------------------------

    def health(self) -> dict:
        return {
            "ok": True,
            "version": VERSION,
            "providers": {"voice": self.voice.available, "decide": self.decider.available},
        }

    def status(self) -> dict:
        return self.budget.status()

    def dev_token(self) -> tuple[int, dict]:
        if not self.s.dev:
            return 404, {"detail": "Not Found"}
        expires_at = int(self.clock()) + self.s.dev_token_ttl_s
        session_id = f"dev_{secrets.token_hex(6)}"
        return 200, {"token": tokens.mint(self.s.hmac_secret, session_id, expires_at),
                     "expires_at": expires_at}

    async def live_socket(self, ws: WebSocket) -> None:
        if not self.origin_allowed(ws.headers.get("origin")) or not self.voice.available:
            await ws.close(code=1008 if self.voice.available else 1013)
            return
        ip = self.client_ip(ws.headers, ws.client.host if ws.client else None)
        try:
            reservation = self.budget.admit(ip)
        except Rejected as rej:
            await ws.accept()
            await ws.send_text(json.dumps({"type": "annie.error", "error": rej.code}))
            await ws.close(code=1013)
            return

        session_id = f"gemini_{secrets.token_hex(12)}"
        try:
            async with self.voice.connect() as upstream:
                await ws.accept()
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
                    initial = json.loads(raw)
                    if (
                        not isinstance(initial, dict)
                        or initial.get("type") != "annie.setup"
                        or set(initial) - {"type", "instructions"}
                        or not isinstance(initial.get("instructions", ""), str)
                        or len(initial.get("instructions", "")) > 16_000
                    ):
                        raise ValueError("invalid setup")
                except (TimeoutError, ValueError, TypeError, json.JSONDecodeError):
                    self.budget.cancel(reservation)
                    await ws.send_text(json.dumps({"type": "annie.error", "error": "invalid_request"}))
                    await ws.close(code=1008)
                    return

                rec = self.budget.open(reservation, session_id)
                await upstream.send(json.dumps(setup_message(
                    self.s.gemini_model, self.s.gemini_voice,
                    initial.get("instructions") or self.s.default_instructions,
                )))
                expires_at = int(rec.deadline)
                await ws.send_text(json.dumps({
                    "type": "annie.session",
                    "session_id": session_id,
                    "token": tokens.mint(
                        self.s.hmac_secret, session_id, expires_at + self.s.token_grace_s
                    ),
                    "expires_at": expires_at,
                }))

                async def to_upstream() -> None:
                    while True:
                        message = await ws.receive_text()
                        if len(message) > 256 * 1024:
                            raise ValueError("message too large")
                        payload = json.loads(message)
                        if not isinstance(payload, dict) or set(payload) != {"realtimeInput"}:
                            raise ValueError("invalid live input")
                        realtime = payload["realtimeInput"]
                        audio = realtime.get("audio") if isinstance(realtime, dict) and set(realtime) == {"audio"} else None
                        if (
                            not isinstance(audio, dict)
                            or set(audio) != {"mimeType", "data"}
                            or audio.get("mimeType") != "audio/pcm;rate=16000"
                            or not isinstance(audio.get("data"), str)
                            or not audio["data"]
                        ):
                            raise ValueError("invalid live input")
                        try:
                            pcm = base64.b64decode(audio["data"], validate=True)
                        except (binascii.Error, ValueError):
                            raise ValueError("invalid live input") from None
                        if not pcm or len(pcm) % 2:
                            raise ValueError("invalid live input")
                        await upstream.send(message)

                async def to_browser() -> None:
                    while True:
                        message = await upstream.recv()
                        if isinstance(message, bytes):
                            message = message.decode("utf-8", "replace")
                        event = json.loads(message)
                        content = event.get("serverContent", {})
                        if (
                            content.get("inputTranscription")
                            or content.get("outputTranscription")
                            or content.get("modelTurn")
                        ):
                            self.budget.activity(session_id)
                        await ws.send_text(message)

                tasks = {
                    asyncio.create_task(to_upstream()),
                    asyncio.create_task(to_browser()),
                }
                reason = "connection_closed"
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            tasks, timeout=1, return_when=asyncio.FIRST_COMPLETED
                        )
                        if done:
                            break
                        current = self.budget.sessions.get(session_id)
                        if current is None:
                            break
                        if self.clock() >= current.deadline:
                            reason = "session_cap"
                            break
                        if self.clock() - current.last_activity >= self.s.idle_timeout_s:
                            reason = "idle_timeout"
                            break
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                with suppress(Exception):
                    await ws.send_text(json.dumps({
                        "type": "annie.closed",
                        "reason": reason,
                        "usage_seconds": max(0, self.clock() - rec.started_at),
                    }))
                with suppress(Exception):
                    await upstream.close()
        except Exception as exc:
            log.warning("gemini live connection failed: %s", type(exc).__name__)
            if not getattr(ws, "client_state", None) or ws.client_state.name != "DISCONNECTED":
                with suppress(Exception):
                    await ws.close(code=1011)
            if reservation and not reservation.done:
                self.budget.cancel(reservation)
        finally:
            self.budget.close(session_id)
            if not reservation.done:
                self.budget.cancel(reservation)

    async def decide(self, payload: Any) -> tuple[int, dict]:
        try:
            req = DecideRequest.model_validate(payload)
        except ValidationError:
            return 400, _err("invalid_request")
        try:
            session_id = tokens.verify(self.s.hmac_secret, req.token, self.clock())
        except tokens.TokenError:
            return 401, _err("invalid_token")
        try:
            body = await self.decider.decide(req.state, req.questions, session_key=session_id)
        except DecideUnavailable as exc:
            return 503, _err("decide_unavailable", reason=exc.reason)
        except DecideInvalid:
            return 422, _err("decide_invalid")
        usage = body.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int):
            self.budget.add_jev(usage["input_tokens"])
        return 200, body

    async def decide_raw(self, raw: Union[bytes, str], origin: Optional[str]) -> tuple[int, dict]:
        if not self.origin_allowed(origin):
            return 403, _err("origin_not_allowed")
        if len(raw) > MAX_BODY_BYTES:
            return 413, _err("too_large")
        try:
            payload = json.loads(raw)
        except ValueError:
            return 400, _err("invalid_request")
        return await self.decide(payload)

    async def decide_socket(self, ws: WebSocket) -> None:
        """WS /decide: one JSON request per message, replies echo `id`, answered out of order."""
        if not self.origin_allowed(ws.headers.get("origin")):
            await ws.close(code=1008)
            return
        await ws.accept()
        send_lock = asyncio.Lock()
        inflight = asyncio.Semaphore(WS_MAX_INFLIGHT)
        tasks: set[asyncio.Task] = set()

        async def reply(msg_id: Any, body: dict) -> None:
            async with send_lock:
                with suppress(Exception):
                    await ws.send_text(json.dumps({"id": msg_id, **body}))

        async def handle(msg_id: Any, payload: Any) -> None:
            try:
                _, body = await self.decide(payload)
                await reply(msg_id, body)
            finally:
                inflight.release()

        try:
            while True:
                raw = await ws.receive_text()
                msg_id = None
                if len(raw) > MAX_BODY_BYTES:
                    await reply(None, _err("too_large"))
                    continue
                try:
                    payload = json.loads(raw)
                    msg_id = payload.get("id") if isinstance(payload, dict) else None
                except ValueError:
                    await reply(None, _err("invalid_request"))
                    continue
                if inflight.locked():
                    await reply(msg_id, _err("decide_unavailable", reason="too_many_inflight"))
                    continue
                await inflight.acquire()
                task = asyncio.ensure_future(handle(msg_id, payload))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (WebSocketDisconnect, RuntimeError, KeyError):  # KeyError: binary frame
            pass
        finally:
            for task in tasks:
                task.cancel()


    # -- optional Rosemate integration (docs/rosemate.md) -------------------------

    async def rosemate_raw(self, route: str, raw: bytes, ip: str, origin: Optional[str]) -> tuple[int, dict]:
        # The origin check is a browser guard only; each route still requires its
        # own server-side admission or scoped integration token.
        if not self.origin_allowed(origin):
            return 403, _err("origin_not_allowed")
        return await self.rosemate.handle(route, raw, ip)

    async def rosemate_socket(self, ws: WebSocket) -> None:
        await self.rosemate.events_socket(ws, self.origin_allowed(ws.headers.get("origin")))


async def read_limited(request: Request, limit: int = MAX_BODY_BYTES) -> Optional[bytes]:
    """Read the body, giving up (None) past `limit` bytes instead of buffering it all."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(settings: Optional[Settings] = None, broker: Optional[Broker] = None) -> FastAPI:
    owned = broker is None
    broker = broker or Broker(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await broker.start()
        try:
            yield
        finally:
            if owned:
                await broker.aclose()

    app = FastAPI(title="annie-broker", version=VERSION, lifespan=lifespan)
    app.state.broker = broker
    if broker.origin_regex is None:
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"],
                           allow_headers=["content-type"], max_age=600)
    else:
        app.add_middleware(CORSMiddleware, allow_origin_regex=broker.origin_regex,
                           allow_methods=["GET", "POST"], allow_headers=["content-type"], max_age=600)

    def respond(result: tuple[int, dict]) -> JSONResponse:
        return JSONResponse(result[1], status_code=result[0])

    @app.get("/health")
    async def health() -> dict:
        return broker.health()

    @app.get("/status")
    async def status() -> dict:
        return broker.status()

    @app.get("/dev-token")
    async def dev_token() -> JSONResponse:
        return respond(broker.dev_token())

    @app.websocket("/live")
    async def live(websocket: WebSocket) -> None:
        await broker.live_socket(websocket)

    @app.post("/decide")
    async def decide(request: Request) -> JSONResponse:
        raw = await read_limited(request)
        if raw is None:
            return respond((413, _err("too_large")))
        return respond(await broker.decide_raw(raw, request.headers.get("origin")))

    @app.websocket("/decide")
    async def decide_ws(websocket: WebSocket) -> None:
        await broker.decide_socket(websocket)

    @app.post("/rosemate/{route}")
    async def rosemate(route: str, request: Request) -> JSONResponse:
        raw = await read_limited(request)
        if raw is None:
            return respond((413, _err("too_large")))
        ip = broker.client_ip(request.headers, request.client.host if request.client else None)
        return respond(await broker.rosemate_raw(route, raw, ip, request.headers.get("origin")))

    @app.websocket("/rosemate/events")
    async def rosemate_events(websocket: WebSocket) -> None:
        await broker.rosemate_socket(websocket)

    return app
