"""Tests for the flight-data provider abstraction (no network).

Covers Travelpayouts normalization, deep_link/marker building, verify_price,
freshness_note, provider selection by FLIGHT_PROVIDER, and the clear error when
the required token is missing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Config, Route
from app.providers import build_provider
from app.providers.amadeus import AmadeusProvider
from app.providers.travelpayouts import (
    TravelpayoutsProvider,
    build_deep_link,
    cheapest,
    normalize_offers,
)
from app.providers import SearchQuery

FIXTURE = Path(__file__).parent / "fixtures" / "travelpayouts_sample.json"


def _load():
    return json.loads(FIXTURE.read_text())


def _config(**kw):
    base = dict(
        telegram_bot_token="x",
        telegram_chat_id="123",
        flight_provider="travelpayouts",
        travelpayouts_token="tok",
        routes=[Route("TLS", "ORY"), Route("TLS", "CDG")],
    )
    base.update(kw)
    return Config(**base)


class _FakeTPClient:
    """Stand-in for TravelpayoutsClient returning a fixed payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def prices_for_dates(self, query):
        self.calls += 1
        return self.payload

    def close(self):
        pass


# ----- normalization --------------------------------------------------------


def test_normalize_extracts_fields():
    payload = _load()
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", "2026-09-14")
    offers = normalize_offers(payload, query)
    assert len(offers) == 2

    first = offers[0]
    assert first.origin == "TLS"
    assert first.destination == "ORY"
    assert first.depart_date == "2026-09-12"
    assert first.return_date == "2026-09-14"
    assert first.carrier == "AF"
    assert first.price_eur == 54.0
    assert first.depart_time == "17:35"
    assert first.return_time == "19:10"
    assert first.duration_min == 75
    assert isinstance(first.raw_offer, dict)


def test_normalize_second_offer():
    payload = _load()
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", "2026-09-14")
    offers = normalize_offers(payload, query)
    second = offers[1]
    assert second.carrier == "U2"
    assert second.price_eur == 89.5
    assert second.depart_time == "07:05"


def test_normalize_skips_priceless_offer():
    payload = {"data": [{"origin": "TLS", "destination": "ORY"}]}  # no price
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", "2026-09-14")
    assert normalize_offers(payload, query) == []


def test_normalize_empty_payload():
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", None)
    assert normalize_offers({}, query) == []


# ----- deep_link / marker ---------------------------------------------------


def test_deep_link_prefixes_relative_fragment():
    url = build_deep_link("/search/TLS1209ORY1409?t=ABC", None)
    assert url == "https://www.aviasales.com/search/TLS1209ORY1409?t=ABC"


def test_deep_link_adds_leading_slash():
    url = build_deep_link("search/X", None)
    assert url == "https://www.aviasales.com/search/X"


def test_deep_link_appends_marker():
    url = build_deep_link("/search/X?t=1", "12345")
    assert url == "https://www.aviasales.com/search/X?t=1&marker=12345"


def test_deep_link_marker_on_fragment_without_query():
    url = build_deep_link("/search/X", "12345")
    assert url == "https://www.aviasales.com/search/X?marker=12345"


def test_deep_link_keeps_absolute_url():
    url = build_deep_link("https://www.aviasales.com/search/X", None)
    assert url == "https://www.aviasales.com/search/X"


def test_deep_link_none_for_empty():
    assert build_deep_link("", "m") is None
    assert build_deep_link(None, "m") is None


def test_normalize_applies_marker():
    payload = _load()
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", "2026-09-14")
    offers = normalize_offers(payload, query, marker="99")
    assert offers[0].deep_link.endswith("&marker=99")
    assert offers[0].deep_link.startswith("https://www.aviasales.com/search/")


def test_provider_search_uses_marker():
    cfg = _config(travelpayouts_marker="777")
    provider = TravelpayoutsProvider(cfg, client=_FakeTPClient(_load()))
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", "2026-09-14")
    offers = provider.search(query)
    assert offers[0].deep_link.endswith("&marker=777")
    assert provider.name == "travelpayouts"


# ----- cheapest / verify_price ----------------------------------------------


def test_cheapest_picks_lowest():
    best = cheapest(_load())
    assert best["price"] == 54


def test_verify_price_refetches_cheapest(db):
    cfg = _config(travelpayouts_marker="m1")
    provider = TravelpayoutsProvider(cfg, client=_FakeTPClient(_load()))
    obs_id = db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", carrier="AF", price_eur=70.0,
        deep_link=None, raw_offer={"price": 70}, source="travelpayouts",
    )
    obs = db.get_observation(obs_id)
    verified = provider.verify_price(obs)
    assert verified is not None
    assert verified.price_eur == 54.0
    assert verified.deep_link.startswith("https://www.aviasales.com/")
    assert verified.deep_link.endswith("&marker=m1")


def test_verify_price_none_on_error(db):
    class _Boom:
        def prices_for_dates(self, q):
            raise RuntimeError("network down")

        def close(self):
            pass

    cfg = _config()
    provider = TravelpayoutsProvider(cfg, client=_Boom())
    obs_id = db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", carrier="AF", price_eur=70.0,
        deep_link=None, raw_offer={"price": 70}, source="travelpayouts",
    )
    obs = db.get_observation(obs_id)
    assert provider.verify_price(obs) is None


def test_verify_price_none_when_no_data(db):
    cfg = _config()
    provider = TravelpayoutsProvider(cfg, client=_FakeTPClient({"success": True, "data": []}))
    obs_id = db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", carrier="AF", price_eur=70.0,
        deep_link=None, raw_offer={"price": 70}, source="travelpayouts",
    )
    obs = db.get_observation(obs_id)
    assert provider.verify_price(obs) is None


# ----- freshness_note -------------------------------------------------------


def test_freshness_note_from_observed_at(db):
    cfg = _config()
    provider = TravelpayoutsProvider(cfg, client=_FakeTPClient(_load()))
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    obs_id = db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", carrier="AF", price_eur=54.0,
        deep_link=None, raw_offer={"price": 54}, source="travelpayouts",
        observed_at=two_hours_ago,
    )
    obs = db.get_observation(obs_id)
    note = provider.freshness_note(obs)
    assert note is not None
    assert "prix observé il y a ~2 h" in note
    assert "vérifie au clic" in note


def test_freshness_note_minutes(db):
    cfg = _config()
    provider = TravelpayoutsProvider(cfg, client=_FakeTPClient(_load()))
    recent = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    obs_id = db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", carrier="AF", price_eur=54.0,
        deep_link=None, raw_offer={"price": 54}, source="travelpayouts",
        observed_at=recent,
    )
    obs = db.get_observation(obs_id)
    note = provider.freshness_note(obs)
    assert "min" in note


def test_amadeus_freshness_note_is_none():
    cfg = Config(
        telegram_bot_token="x", telegram_chat_id="1",
        flight_provider="amadeus",
        amadeus_client_id="id", amadeus_client_secret="sec",
    )
    # Build without a real client by passing a dummy.
    provider = AmadeusProvider(cfg, client=object())
    assert provider.freshness_note({"observed_at": "2026-01-01T00:00:00Z"}) is None


# ----- provider selection ---------------------------------------------------


def test_build_provider_default_is_travelpayouts():
    cfg = _config()
    provider = build_provider(cfg)
    assert isinstance(provider, TravelpayoutsProvider)
    assert provider.name == "travelpayouts"
    provider.close()


def test_build_provider_amadeus():
    cfg = Config(
        telegram_bot_token="x", telegram_chat_id="1",
        flight_provider="amadeus",
        amadeus_client_id="id", amadeus_client_secret="sec",
    )
    provider = build_provider(cfg)
    assert isinstance(provider, AmadeusProvider)
    assert provider.name == "amadeus"
    provider.close()


def test_build_provider_unknown_raises():
    cfg = _config(flight_provider="serpapi")
    with pytest.raises(RuntimeError):
        build_provider(cfg)
