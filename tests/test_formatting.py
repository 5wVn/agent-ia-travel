"""Tests for the shared stops (escales) formatting helper."""

from __future__ import annotations

from app.formatting import format_stops, format_stops_roundtrip


def test_format_stops_direct():
    assert format_stops(0) == "direct"


def test_format_stops_one():
    assert format_stops(1) == "1 escale"


def test_format_stops_many():
    assert format_stops(2) == "2 escales"
    assert format_stops(3) == "3 escales"


def test_format_stops_none_and_invalid():
    assert format_stops(None) is None
    assert format_stops("oops") is None  # type: ignore[arg-type]
    assert format_stops(-1) is None


def test_roundtrip_both_none():
    assert format_stops_roundtrip(None, None) is None


def test_roundtrip_outbound_only():
    assert format_stops_roundtrip(0, None) == "direct"
    assert format_stops_roundtrip(1, None) == "1 escale"


def test_roundtrip_return_only():
    assert format_stops_roundtrip(None, 1) == "retour 1 escale"


def test_roundtrip_identical_legs_single_mention():
    assert format_stops_roundtrip(0, 0) == "direct"
    assert format_stops_roundtrip(1, 1) == "1 escale"


def test_roundtrip_different_legs():
    assert format_stops_roundtrip(0, 1) == "direct / retour 1 escale"
    assert format_stops_roundtrip(1, 0) == "1 escale / retour direct"
    assert format_stops_roundtrip(2, 1) == "2 escales / retour 1 escale"
