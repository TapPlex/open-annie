"""Server-side WebSocket connection to Gemini Live through Cloudflare AI Gateway."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .config import Settings

LIVE_METHOD = "/google-ai-studio/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"


def live_url(base_url: str) -> str:
    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path + LIVE_METHOD, "", ""))


def setup_message(model: str, voice: str, instructions: str) -> dict:
    setup: dict[str, Any] = {
        "model": f"models/{model}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
            },
        },
        "outputAudioTranscription": {},
        "inputAudioTranscription": {},
    }
    if instructions:
        setup["systemInstruction"] = {"parts": [{"text": instructions}]}
    return {"setup": setup}


class GeminiLiveClient:
    def __init__(self, settings: Settings, connect: Callable | None = None):
        self.s = settings
        self._connect = connect

    @property
    def available(self) -> bool:
        return bool(
            self.s.gemini_api_key
            and self.s.voice_usd_per_second is not None
            and self.s.voice_usd_per_second > 0
        )

    @asynccontextmanager
    async def connect(self):
        if self._connect:
            async with self._connect(
                live_url(self.s.gemini_base_url),
                {"x-goog-api-key": self.s.gemini_api_key},
            ) as websocket:
                yield websocket
            return

        from websockets.asyncio.client import connect

        async with connect(
            live_url(self.s.gemini_base_url),
            additional_headers={"x-goog-api-key": self.s.gemini_api_key},
            open_timeout=10,
            max_size=2**20,
            max_queue=16,
        ) as websocket:
            yield websocket
