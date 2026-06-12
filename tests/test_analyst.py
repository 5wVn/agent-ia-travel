"""Tests for the analyst in 'mode sans LLM': no client, no network attempt."""

from __future__ import annotations

import pytest

from app.analyst import Analyst, _fallback_deal_message, _trend_label
from app.config import Config, Route


def _config(anthropic_api_key):
    return Config(
        amadeus_client_id="x",
        amadeus_client_secret="x",
        anthropic_api_key=anthropic_api_key,
        telegram_bot_token="x",
        telegram_chat_id="123",
        routes=[Route("TLS", "ORY")],
    )


def test_no_key_does_not_instantiate_client(monkeypatch):
    # If the client were created, importing/instantiating anthropic with a fake
    # would still happen; we assert the client attribute is None instead.
    cfg = _config(None)
    assert cfg.llm_enabled() is False
    analyst = Analyst(cfg)
    assert analyst._client is None


def test_empty_key_is_treated_as_disabled():
    cfg = _config("   ")
    assert cfg.llm_enabled() is False
    analyst = Analyst(cfg)
    assert analyst._client is None


def test_no_key_never_calls_network(monkeypatch):
    # Make any attempt to import/use anthropic explode; the analyst must not.
    import builtins

    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "anthropic":
            raise AssertionError("anthropic must not be imported in mode sans LLM")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    analyst = Analyst(_config(None))
    out = analyst.analyze_deal({"route": "TLS-ORY", "price_eur": 54, "median_30d": 89})
    assert "TLS-ORY" in out
    digest = analyst.daily_digest({"routes": [], "top_deals": []})
    assert "mode dégradé" in digest


def test_fallback_deal_message_is_complete():
    ctx = {
        "route": "TLS-ORY",
        "price_eur": 54,
        "median_30d": 89,
        "p10_30d": 50,
        "score": 82,
        "components": {"prix": 95.0, "horaire": 80.0, "composite": 82.0},
        "recent_prices": [54, 60, 70],
    }
    msg = _fallback_deal_message(ctx)
    assert "Médiane" in msg
    assert "p10" in msg
    assert "Score" in msg
    assert "Tendance" in msg
    assert "Reco" in msg


def test_trend_label():
    assert _trend_label([50, 60, 70]) == "en baisse"
    assert "hausse" in _trend_label([70, 60, 50])
    assert _trend_label([60, 60, 60]) == "stable"
    assert _trend_label([60]) is None
