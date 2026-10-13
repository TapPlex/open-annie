import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from annie_broker import Broker, Settings, create_app

OFFER = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\n"
ANSWER = "v=0\r\no=- 9 9 IN IP4 10.0.0.1\r\ns=answer\r\n"
LEAK = "UPSTREAM_SECRET_DETAIL_do_not_forward"

JEV_OK = {
    "model": "jev-1.13.0",
    "answers": {
        "face": {
            "type": "choice",
            "choice": "joyful",
            "probabilities": {"joyful": 0.9, "neutral": 0.1},
            "confidence": 0.85,
        },
        "wants_dance": {"type": "noul", "noul": 0.97},
    },
    "usage": {"input_tokens": 300, "output_tokens": 20},
}

QUESTIONS = {
    "face": {
        "type": "choice",
        "instructions": "Which face fits?",
        "criteria": {"joyful": "very happy", "neutral": None},
    }
}


class Upstream:
    """Scriptable httpx.MockTransport recording every request."""

    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self.handler = handler or (lambda req: httpx.Response(200, json=JEV_OK))
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def json(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)

    def calls(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


BASE = Settings(
    gemini_api_key="test-gemini-key",
    typesafe_api_key="ts-test",
    hmac_secret=b"test-secret",
    voice_usd_per_second=0.001,
    per_ip_daily=0,
)


@pytest.fixture
def make_client():
    clients = []

    def _make(settings: Settings | None = None, typesafe=None, rosemate=None, gemini_connect=None, **overrides):
        s = replace(settings or BASE, **overrides)
        broker = Broker(
            s,
            gemini_connect=gemini_connect,
            typesafe_transport=(typesafe or Upstream()).transport,
            rosemate_transport=rosemate.transport if rosemate else None,
        )
        client = TestClient(create_app(broker=broker))
        client.__enter__()
        clients.append((client, broker))
        return client

    yield _make
    for client, broker in clients:
        client.portal.call(broker.aclose)
        client.__exit__(None, None, None)
