"""Broker settings, read once from the environment."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field

VERSION = "0.1.0"

# Jev: $0.042 per million input tokens, output free (docs.typesafe.ai/models).
JEV_USD_PER_INPUT_TOKEN = 0.042 / 1_000_000


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return (_env(name, "1" if default else "0") or "").lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in (_env(name, default) or "").split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str | None = None
    typesafe_api_key: str | None = None
    hmac_secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    daily_budget_usd: float = 25.0
    max_sessions: int = 20
    session_cap_s: int = 180
    idle_timeout_s: int = 30
    per_ip_daily: int = 2  # 0 disables the per-IP limit
    jev_model: str = "jev-latest"
    gemini_model: str = "gemini-3.8-live"
    gemini_base_url: str = "https://gateway.ai.cloudflare.com/v1/ac4054842a60a05296897e5f7042b4cd/winbestsai/compat"
    gemini_voice: str = "Kore"
    voice_usd_per_second: float | None = None
    allowed_origins: tuple[str, ...] = ("http://localhost:*", "http://127.0.0.1:*")
    # Jev allows 1,200 rpm per key; stay under it so bursts never hit 429.
    jev_rpm: int = 1000
    jev_burst: int = 20
    # The browser enforces its own 900 ms (reply) / 700 ms (listen) budgets; this
    # server deadline only bounds how long an upstream call may hold a slot.
    decide_timeout_s: float = 1.5
    decide_per_session_rps: float = 5.0
    token_grace_s: int = 30
    default_instructions: str = ""
    # Restrict what the browser data channel may send; () = upstream default (all).
    client_events: tuple[str, ...] = ()
    ip_header: str | None = None  # e.g. "x-forwarded-for" behind a trusted proxy
    typesafe_base_url: str = "https://api.typesafe.ai"
    # Dev only: GET /dev-token hands out /decide tokens without a voice session.
    dev: bool = False
    dev_token_ttl_s: int = 900
    # Optional Rosemate MCP integration (docs/rosemate.md). Off unless the URL, the token
    # and an operator access key are all set. The token never leaves the server.
    rosemate_mcp_url: str | None = None
    rosemate_mcp_token: str | None = field(default=None, repr=False)
    rosemate_access_key: str | None = field(default=None, repr=False)
    rosemate_events_url: str | None = None  # default: derived from the MCP URL
    rosemate_toy_actions: bool = False  # outbound hardware calls; users must also consent
    rosemate_poll: bool = False  # consuming mailbox fallback; competes with the Rosemate PWA
    rosemate_session_ttl_s: int = 1800
    rosemate_max_sessions: int = 4
    rosemate_timeout_s: float = 8.0
    rosemate_max_intensity: int = 60
    rosemate_max_duration_s: float = 10.0
    rosemate_toy_per_min: int = 12
    rosemate_event_max_age_s: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        secret = _env("ANNIE_HMAC_SECRET")
        return cls(
            # fal secrets are shared by every app on an account, so the fal deploy uses
            # namespaced ANNIE_* names; plain names still work locally.
            gemini_api_key=_env("ANNIE_GEMINI_API_KEY") or _env("GEMINI_API_KEY"),
            typesafe_api_key=_env("ANNIE_TYPESAFE_API_KEY") or _env("TYPESAFE_API_KEY"),
            hmac_secret=secret.encode() if secret else secrets.token_bytes(32),
            daily_budget_usd=_float("ANNIE_DAILY_BUDGET_USD", 25.0),
            max_sessions=_int("ANNIE_MAX_SESSIONS", 20),
            session_cap_s=_int("ANNIE_SESSION_CAP_S", 180),
            idle_timeout_s=_int("ANNIE_IDLE_TIMEOUT_S", 30),
            per_ip_daily=_int("ANNIE_PER_IP_DAILY", 2),
            jev_model=_env("JEV_MODEL", "jev-latest"),
            gemini_model=_env("ANNIE_GEMINI_MODEL") or _env("GEMINI_MODEL", "gemini-3.8-live"),
            gemini_base_url=_env("ANNIE_GEMINI_BASE_URL") or _env(
                "GEMINI_BASE_URL",
                "https://gateway.ai.cloudflare.com/v1/ac4054842a60a05296897e5f7042b4cd/winbestsai/compat",
            ),
            gemini_voice=_env("ANNIE_GEMINI_VOICE") or _env("GEMINI_VOICE", "Kore"),
            voice_usd_per_second=(
                float(rate) if (rate := _env("ANNIE_VOICE_USD_PER_SECOND")) else None
            ),
            allowed_origins=_list(
                "ANNIE_ALLOWED_ORIGINS", "http://localhost:*,http://127.0.0.1:*"
            ),
            jev_rpm=_int("ANNIE_JEV_RPM", 1000),
            decide_timeout_s=_float("ANNIE_DECIDE_TIMEOUT_S", 1.5),
            default_instructions=_env("ANNIE_INSTRUCTIONS", ""),
            client_events=_list("ANNIE_CLIENT_EVENTS", ""),
            ip_header=_env("ANNIE_IP_HEADER"),
            typesafe_base_url=_env("TYPESAFE_BASE_URL", "https://api.typesafe.ai"),
            dev=_bool("ANNIE_DEV", False),
            dev_token_ttl_s=_int("ANNIE_DEV_TOKEN_TTL_S", 900),
            rosemate_mcp_url=_env("ANNIE_ROSEMATE_MCP_URL"),
            rosemate_mcp_token=_env("ANNIE_ROSEMATE_MCP_TOKEN"),
            rosemate_access_key=_env("ANNIE_ROSEMATE_ACCESS_KEY"),
            rosemate_events_url=_env("ANNIE_ROSEMATE_EVENTS_URL"),
            rosemate_toy_actions=_bool("ANNIE_ROSEMATE_TOY_ACTIONS", False),
            rosemate_poll=_bool("ANNIE_ROSEMATE_POLL", False),
            rosemate_session_ttl_s=_int("ANNIE_ROSEMATE_SESSION_TTL_S", 1800),
            rosemate_max_sessions=_int("ANNIE_ROSEMATE_MAX_SESSIONS", 4),
            rosemate_timeout_s=_float("ANNIE_ROSEMATE_TIMEOUT_S", 8.0),
            rosemate_max_intensity=_int("ANNIE_ROSEMATE_MAX_INTENSITY", 60),
            rosemate_max_duration_s=_float("ANNIE_ROSEMATE_MAX_DURATION_S", 10.0),
            rosemate_toy_per_min=_int("ANNIE_ROSEMATE_TOY_PER_MIN", 12),
            rosemate_event_max_age_s=_float("ANNIE_ROSEMATE_EVENT_MAX_AGE_S", 30.0),
        )

    @property
    def session_cap_usd(self) -> float | None:
        return (
            self.session_cap_s * self.voice_usd_per_second
            if self.voice_usd_per_second is not None
            else None
        )
