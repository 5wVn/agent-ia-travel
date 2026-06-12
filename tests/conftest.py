"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from app.config import Config, Route
from app.db import Database


@pytest.fixture
def db(tmp_path) -> Database:
    """A fresh on-disk SQLite database for each test."""
    database = Database(str(tmp_path / "test.db"))
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    """A Config with fixed values and dummy secrets (no env required)."""
    return Config(
        amadeus_client_id="x",
        amadeus_client_secret="x",
        anthropic_api_key="x",
        telegram_bot_token="x",
        telegram_chat_id="123",
        routes=[Route("TLS", "ORY"), Route("TLS", "CDG")],
        preferred_carriers=["AF"],
        avoided_carriers=["XX"],
    )
