"""Tests for load_config: provider selection by env + clear credential errors."""

from __future__ import annotations

import pytest

from app.config import load_config


def _base_env(monkeypatch):
    """Set the always-required Telegram secrets, clear everything else."""
    for var in (
        "FLIGHT_PROVIDER",
        "TRAVELPAYOUTS_TOKEN",
        "TRAVELPAYOUTS_MARKER",
        "AMADEUS_CLIENT_ID",
        "AMADEUS_CLIENT_SECRET",
        "ANTHROPIC_API_KEY",
        "AMADEUS_MONTHLY_QUOTA",
        "TRAVELPAYOUTS_MONTHLY_QUOTA",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")


def test_default_provider_is_travelpayouts(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("TRAVELPAYOUTS_TOKEN", "tok")
    cfg = load_config()
    assert cfg.flight_provider == "travelpayouts"
    assert cfg.travelpayouts_token == "tok"
    # Amadeus keys are not required for the default provider.
    assert cfg.amadeus_client_id == ""


def test_travelpayouts_missing_token_raises(monkeypatch):
    _base_env(monkeypatch)  # no TRAVELPAYOUTS_TOKEN
    with pytest.raises(RuntimeError) as exc:
        load_config()
    assert "TRAVELPAYOUTS_TOKEN" in str(exc.value)


def test_amadeus_provider_requires_keys(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("FLIGHT_PROVIDER", "amadeus")
    with pytest.raises(RuntimeError) as exc:
        load_config()
    assert "AMADEUS_CLIENT_ID" in str(exc.value)


def test_amadeus_provider_with_keys_ok(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("FLIGHT_PROVIDER", "amadeus")
    monkeypatch.setenv("AMADEUS_CLIENT_ID", "id")
    monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "sec")
    cfg = load_config()
    assert cfg.flight_provider == "amadeus"
    assert cfg.provider_monthly_quota() == cfg.amadeus_monthly_quota


def test_unknown_provider_raises(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("FLIGHT_PROVIDER", "serpapi")
    with pytest.raises(RuntimeError) as exc:
        load_config()
    assert "FLIGHT_PROVIDER" in str(exc.value)


def test_marker_optional_and_quota_selection(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("TRAVELPAYOUTS_TOKEN", "tok")
    monkeypatch.setenv("TRAVELPAYOUTS_MARKER", "55555")
    cfg = load_config()
    assert cfg.travelpayouts_marker == "55555"
    assert cfg.provider_monthly_quota() == cfg.travelpayouts_monthly_quota
