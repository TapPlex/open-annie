"""open-annie broker on fal serverless (CPU, one always-warm runner).

Deploy (from the repo root, with fal >= 1.79 installed and `fal auth login` done):

    fal secrets set ANNIE_GEMINI_API_KEY="$GEMINI_API_KEY" ANNIE_TYPESAFE_API_KEY="$TYPESAFE_API_KEY" \\
        ANNIE_GEMINI_BASE_URL=https://gateway.ai.cloudflare.com/v1/ac4054842a60a05296897e5f7042b4cd/winbestsai/compat \\
        ANNIE_HMAC_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
    fal secrets set ANNIE_ALLOWED_ORIGINS=https://your-stage.example.com
    fal deploy broker/fal_app.py::AnnieBroker --app-name annie-broker --auth public
    curl https://fal.run/<your-username>/annie-broker/health

Routes then live at https://fal.run/<owner>/annie-broker/{session,decide,status,health}
and wss://fal.run/<owner>/annie-broker/decide. The optional Rosemate integration
(docs/rosemate.md) adds POST /rosemate/{session,consent,action,close} and WS
/rosemate/events when its ANNIE_ROSEMATE_* secrets are set (unverified on fal). `--auth public` is required because
browsers call the broker without a fal key; the broker's own budget, per-IP limit,
origin allowlist and HMAC tokens are the access control.

Every route delegates to annie_broker.app.Broker, the same object the local FastAPI
app uses. fal registers @fal.endpoint routes as POST (or WebSocket), so the two GET
routes are added through App._add_extra_routes, where fal itself mounts GET /health.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fal  # noqa: E402
# Imported at module level on purpose: fal ships local_python_modules by pickling the
# objects the app references, so an import inside setup() would look for an installed
# package on the runner and fail.
from annie_broker import Broker  # noqa: E402
from annie_broker.app import read_limited  # noqa: E402
from fastapi import FastAPI, Request, WebSocket  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

# Keep identical to requirements.txt (minus the local-only test tools).
REQUIREMENTS = [
    "fastapi==0.136.3",
    "uvicorn==0.54.0",
    "httpx[http2]==0.28.1",
    "pydantic==2.13.4",
    "websockets==17.1",
]


class AnnieBroker(fal.App, name="annie-broker", keep_alive=300):
    machine_type = "S"  # 1 vCPU / 1 GB: the broker only relays small JSON and SDP
    app_auth = "public"
    requirements = REQUIREMENTS
    local_python_modules = ["annie_broker"]
    # Budget and per-IP state are in-process, so exactly one runner serves all traffic.
    min_concurrency = 1
    max_concurrency = 1
    max_multiplexing = 32  # WS /decide holds a slot for a whole voice session
    request_timeout = 300  # longer than ANNIE_SESSION_CAP_S so WS /decide outlives a session
    secrets = [
        "ANNIE_GEMINI_API_KEY", "ANNIE_GEMINI_BASE_URL", "ANNIE_GEMINI_MODEL",
        "ANNIE_GEMINI_VOICE", "ANNIE_VOICE_USD_PER_SECOND",
        "ANNIE_TYPESAFE_API_KEY", "ANNIE_HMAC_SECRET", "ANNIE_ALLOWED_ORIGINS",
        # Optional Rosemate integration (docs/rosemate.md); unset = off.
        "ANNIE_ROSEMATE_MCP_URL", "ANNIE_ROSEMATE_MCP_TOKEN", "ANNIE_ROSEMATE_ACCESS_KEY",
        "ANNIE_ROSEMATE_TOY_ACTIONS",
    ]

    async def setup(self) -> None:

        # The fal gateway sits in front of the runner; the caller's address arrives
        # in X-Forwarded-For (leftmost entry). Verify after first deploy.
        os.environ.setdefault("ANNIE_IP_HEADER", "x-forwarded-for")
        os.environ["ANNIE_DEV"] = "0"  # /dev-token must not bypass live session admission control
        self.broker = Broker()
        await self.broker.start()

    async def teardown(self) -> None:
        await self.broker.aclose()

    def health(self) -> dict:
        return self.broker.health()

    def _add_extra_routes(self, app: FastAPI) -> None:
        @app.get("/health")
        async def health() -> dict:
            return self.broker.health()

        @app.get("/status")
        async def status() -> dict:
            return self.broker.status()

    @fal.endpoint("/decide")
    async def decide(self, request: Request) -> JSONResponse:
        raw = await read_limited(request)
        if raw is None:
            return JSONResponse({"error": "too_large"}, status_code=413)
        status, body = await self.broker.decide_raw(raw, request.headers.get("origin"))
        return JSONResponse(body, status_code=status)

    @fal.endpoint("/decide", is_websocket=True)
    async def decide_ws(self, websocket: WebSocket) -> None:
        await self.broker.decide_socket(websocket)

    @fal.endpoint("/live", is_websocket=True)
    async def live_ws(self, websocket: WebSocket) -> None:
        await self.broker.live_socket(websocket)

    async def _rosemate(self, route: str, request: Request) -> JSONResponse:
        raw = await read_limited(request)
        if raw is None:
            return JSONResponse({"error": "too_large"}, status_code=413)
        ip = self.broker.client_ip(request.headers, request.client.host if request.client else None)
        status, body = await self.broker.rosemate_raw(route, raw, ip, request.headers.get("origin"))
        return JSONResponse(body, status_code=status)

    @fal.endpoint("/rosemate/session")
    async def rosemate_session(self, request: Request) -> JSONResponse:
        return await self._rosemate("session", request)

    @fal.endpoint("/rosemate/consent")
    async def rosemate_consent(self, request: Request) -> JSONResponse:
        return await self._rosemate("consent", request)

    @fal.endpoint("/rosemate/action")
    async def rosemate_action(self, request: Request) -> JSONResponse:
        return await self._rosemate("action", request)

    @fal.endpoint("/rosemate/close")
    async def rosemate_close(self, request: Request) -> JSONResponse:
        return await self._rosemate("close", request)

    @fal.endpoint("/rosemate/events", is_websocket=True)
    async def rosemate_events(self, websocket: WebSocket) -> None:
        await self.broker.rosemate_socket(websocket)

    @fal.endpoint("/status")
    async def status_post(self) -> dict:
        """POST twin of GET /status, in case the gateway only forwards POST."""
        return self.broker.status()
