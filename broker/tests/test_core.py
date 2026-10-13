import json
from dataclasses import replace

import pytest

from annie_broker import tokens
from annie_broker.budget import Budget, Rejected
from conftest import BASE

SECRET = b"k"


class Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_token_roundtrip_and_expiry():
    tok = tokens.mint(SECRET, "gemini_abc", 1000)
    assert tokens.verify(SECRET, tok, now=999) == "gemini_abc"
    with pytest.raises(tokens.TokenError, match="expired"):
        tokens.verify(SECRET, tok, now=1000)


def test_token_rejects_tampering():
    tok = tokens.mint(SECRET, "gemini_abc", 1000)
    sid, exp, sig = tok.rsplit(".", 2)
    for bad in (f"gemini_xyz.{exp}.{sig}", f"{sid}.2000.{sig}", tok + "x", "x", None, 5):
        with pytest.raises(tokens.TokenError):
            tokens.verify(SECRET, bad, now=0)
    with pytest.raises(tokens.TokenError, match="bad_signature"):
        tokens.verify(b"other", tok, now=0)


def test_token_allows_dotted_ids():
    assert tokens.verify(SECRET, tokens.mint(SECRET, "a.b.c", 10), now=0) == "a.b.c"


def test_budget_tracks_session_wall_clock_and_usage():
    clock = Clock()
    b = Budget(replace(BASE, daily_budget_usd=1.0), clock)
    b.open(b.admit("1.2.3.4"), "gemini_1")
    assert b.voice_seconds_today() == 0
    clock.t += 60
    assert b.voice_seconds_today() == 60
    b.usage("gemini_1", 42)
    assert b.voice_seconds_today() == 42
    b.close("gemini_1", final_seconds=50)
    assert b.voice_seconds_today() == 50
    assert b.spent_today() == pytest.approx(50 * BASE.voice_usd_per_second)
    assert b.status()["active_sessions"] == 0


def test_budget_admission_reserves_worst_case():
    b = Budget(replace(BASE, daily_budget_usd=0.37, session_cap_s=180), Clock())
    b.open(b.admit("a"), "gemini_1")
    b.open(b.admit("b"), "gemini_2")
    with pytest.raises(Rejected, match="budget_exhausted"):
        b.admit("c")


def test_budget_rejects_unconfigured_voice_estimate():
    b = Budget(replace(BASE, voice_usd_per_second=None), Clock())
    with pytest.raises(Rejected, match="voice_cost_unconfigured"):
        b.admit("ip")


def test_budget_resets_at_utc_midnight():
    clock = Clock(1_790_035_199.0)
    b = Budget(replace(BASE, per_ip_daily=1), clock)
    b.close("unknown")
    b.open(b.admit("ip"), "gemini_1")
    b.close("gemini_1", 100)
    with pytest.raises(Rejected, match="ip_limit"):
        b.admit("ip")
    clock.t += 2
    assert b.spent_today() == 0
    b.admit("ip")


def test_ips_are_hashed():
    b = Budget(replace(BASE, per_ip_daily=5), Clock())
    b.admit("203.0.113.7")
    assert "203.0.113.7" not in json.dumps(b._ip_counts)
