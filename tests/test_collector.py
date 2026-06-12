"""Tests for Amadeus offer normalization (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from app.collector import (
    _iso_duration_to_minutes,
    build_queries,
    normalize_offers,
    quota_allows,
    SearchQuery,
)
from app.config import Route

FIXTURE = Path(__file__).parent / "fixtures" / "amadeus_sample.json"


def _load():
    return json.loads(FIXTURE.read_text())


def test_iso_duration_parsing():
    assert _iso_duration_to_minutes("PT1H15M") == 75
    assert _iso_duration_to_minutes("PT2H") == 120
    assert _iso_duration_to_minutes("PT45M") == 45
    assert _iso_duration_to_minutes(None) is None


def test_normalize_offers_extracts_fields():
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


def test_normalize_offers_second_carrier_and_price():
    payload = _load()
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", "2026-09-14")
    offers = normalize_offers(payload, query)
    second = offers[1]
    assert second.carrier == "U2"
    assert second.price_eur == 89.50
    assert second.depart_time == "07:05"


def test_normalize_offers_transfers_from_fixture():
    """Direct fixture itineraries (1 segment each) yield 0 stops on both legs."""
    payload = _load()
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", "2026-09-14")
    offers = normalize_offers(payload, query)
    assert offers[0].transfers == 0
    assert offers[0].return_transfers == 0


def test_normalize_offers_transfers_segments_minus_one():
    """Amadeus stops = number of segments - 1, computed per itinerary."""
    payload = {
        "data": [
            {
                "price": {"grandTotal": "150.00"},
                "validatingAirlineCodes": ["IB"],
                "itineraries": [
                    {
                        "duration": "PT5H",
                        "segments": [
                            {"departure": {"at": "2026-09-12T09:15:00"},
                             "carrierCode": "IB"},
                            {"departure": {"at": "2026-09-12T12:30:00"},
                             "carrierCode": "IB"},
                        ],
                    },
                    {
                        "duration": "PT2H",
                        "segments": [
                            {"departure": {"at": "2026-09-14T18:00:00"},
                             "carrierCode": "IB"},
                        ],
                    },
                ],
            }
        ]
    }
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", "2026-09-14")
    offers = normalize_offers(payload, query)
    assert len(offers) == 1
    assert offers[0].transfers == 1  # 2 outbound segments - 1
    assert offers[0].return_transfers == 0  # 1 inbound segment - 1


def test_normalize_skips_malformed_offer():
    payload = {"data": [{"id": "broken"}]}  # no price, no itineraries
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", "2026-09-14")
    offers = normalize_offers(payload, query)
    assert offers == []


def test_normalize_empty_payload():
    query = SearchQuery(Route("TLS", "ORY"), "2026-09-12", None)
    assert normalize_offers({}, query) == []


def test_build_queries_covers_routes_and_dedups(config, db):
    queries = build_queries(config, db)
    # 2 routes x 8 weekends = 16 (no tracked dates yet).
    assert len(queries) == 2 * config.weekend_count
    labels = {(q.route.label(), q.depart_date, q.return_date) for q in queries}
    assert len(labels) == len(queries)  # no duplicates


def test_build_queries_includes_tracked_dates(config, db):
    db.insert_tracked_date(depart_date="2030-01-04", return_date="2030-01-06")
    queries = build_queries(config, db)
    tracked = [q for q in queries if q.depart_date == "2030-01-04"]
    assert len(tracked) == len(config.routes)


def test_quota_allows_stops_at_ceiling(config, db):
    config.travelpayouts_monthly_quota = 10  # fixture provider is travelpayouts
    config.quota_safety_ratio = 0.80  # ceiling = 8
    for _ in range(7):
        db.record_api_call()
    assert quota_allows(config, db) is True
    db.record_api_call()  # now 8
    assert quota_allows(config, db) is False
