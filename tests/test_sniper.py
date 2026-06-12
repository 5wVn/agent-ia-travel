"""Tests for the price sniper: arming, candidate gating, trigger, rebound,
re-verification, and quota priority. The Amadeus client is mocked — no network.
"""

from __future__ import annotations

import json

from app.collector import standard_scan_should_defer
from app.sniper import (
    evaluate_snipe,
    extract_priced_total,
    needs_boosted_watch,
    reverify_price,
    snipe_candidates,
)


def _insert_best(db, price, *, depart="2026-09-12", ret="2026-09-14", raw=None):
    return db.insert_observation(
        origin="TLS",
        destination="ORY",
        depart_date=depart,
        return_date=ret,
        carrier="AF",
        price_eur=float(price),
        deep_link=None,
        raw_offer=raw if raw is not None else {"id": "offer-1"},
    )


class _FakeClient:
    """Stand-in for AmadeusClient.price_offer returning a fixed confirmed price."""

    def __init__(self, confirmed):
        self.confirmed = confirmed
        self.calls = 0

    def price_offer(self, raw_offer):
        self.calls += 1
        if self.confirmed is None:
            raise RuntimeError("pricing down")
        return {"data": {"flightOffers": [{"price": {"grandTotal": str(self.confirmed)}}]}}


# ----- migration idempotence ------------------------------------------------


def test_migration_adds_snipe_columns(db):
    cols = db._column_names("tracked_dates")
    assert "snipe_price_eur" in cols
    assert "snipe_state" in cols


def test_migration_idempotent(db):
    # Running the migration again must not raise or duplicate columns.
    db._migrate()
    db._migrate()
    cols = db._column_names("tracked_dates")
    assert sum(c == "snipe_price_eur" for c in cols) == 1


# ----- arming / db helpers --------------------------------------------------


def test_arm_and_disarm_snipe(db):
    tid = db.insert_tracked_date(depart_date="2026-09-12", return_date="2026-09-14")
    db.arm_snipe(tid, 55.0)
    armed = db.armed_snipes()
    assert len(armed) == 1
    assert armed[0]["snipe_state"] == "armed"
    assert armed[0]["snipe_price_eur"] == 55.0
    db.disarm_snipe(tid)
    assert db.armed_snipes() == []
    row = db.get_tracked_date(tid)
    assert row["snipe_state"] is None
    assert row["snipe_price_eur"] is None


# ----- candidate gating (proximity) -----------------------------------------


def test_candidate_gating_by_proximity(config, db):
    config.snipe_proximity_ratio = 1.15
    tid = db.insert_tracked_date(depart_date="2026-09-12", return_date="2026-09-14")
    db.arm_snipe(tid, 50.0)  # threshold*1.15 = 57.5

    _insert_best(db, 70.0)  # far above -> not a candidate
    assert [r["id"] for r in snipe_candidates(config, db)] == []
    assert needs_boosted_watch(config, db) is False

    _insert_best(db, 56.0)  # within proximity window -> candidate
    cands = snipe_candidates(config, db)
    assert [r["id"] for r in cands] == [tid]
    assert needs_boosted_watch(config, db) is True


def test_candidate_when_no_history(config, db):
    # A snipe with no price observed yet is always a candidate (start watching).
    tid = db.insert_tracked_date(depart_date="2026-09-12", return_date="2026-09-14")
    db.arm_snipe(tid, 50.0)
    cands = snipe_candidates(config, db)
    assert [r["id"] for r in cands] == [tid]


# ----- evaluate: watching / triggered / rebounded ---------------------------


def test_evaluate_above_threshold_keeps_watching(config, db):
    tid = db.insert_tracked_date(depart_date="2026-09-12", return_date="2026-09-14")
    db.arm_snipe(tid, 55.0)
    _insert_best(db, 70.0)
    tr = db.get_tracked_date(tid)
    res = evaluate_snipe(config, db, tr, _FakeClient(confirmed=70.0))
    assert res.triggered is False
    assert res.status == "watching"


def test_evaluate_triggers_when_confirmed_below(config, db):
    tid = db.insert_tracked_date(depart_date="2026-09-12", return_date="2026-09-14")
    db.arm_snipe(tid, 55.0)
    _insert_best(db, 50.0, raw={"id": "live"})
    tr = db.get_tracked_date(tid)
    client = _FakeClient(confirmed=52.0)
    res = evaluate_snipe(config, db, tr, client)
    assert client.calls == 1  # live re-verification happened
    assert res.triggered is True
    assert res.status == "triggered"
    assert res.confirmed_price_eur == 52.0


def test_evaluate_rebounds_when_live_price_climbs(config, db):
    tid = db.insert_tracked_date(depart_date="2026-09-12", return_date="2026-09-14")
    db.arm_snipe(tid, 55.0)
    _insert_best(db, 50.0)  # observed below threshold...
    tr = db.get_tracked_date(tid)
    client = _FakeClient(confirmed=60.0)  # ...but live price has climbed back
    res = evaluate_snipe(config, db, tr, client)
    assert res.triggered is False
    assert res.status == "rebounded"


def test_evaluate_falls_back_to_observed_on_pricing_error(config, db):
    tid = db.insert_tracked_date(depart_date="2026-09-12", return_date="2026-09-14")
    db.arm_snipe(tid, 55.0)
    _insert_best(db, 50.0)
    tr = db.get_tracked_date(tid)
    client = _FakeClient(confirmed=None)  # pricing endpoint down
    res = evaluate_snipe(config, db, tr, client)
    # A genuine observed drop must not be lost on a transient pricing error.
    assert res.triggered is True
    assert res.status == "triggered"


class _FakeProvider:
    """Provider-style stand-in exposing verify_price (Travelpayouts path)."""

    def __init__(self, price, note="prix observé il y a ~2 h, vérifie au clic"):
        self.price = price
        self.note = note

    def verify_price(self, observation):
        from app.providers import VerifiedPrice

        if self.price is None:
            return None
        return VerifiedPrice(
            price_eur=self.price,
            deep_link="https://www.aviasales.com/search/X?marker=1",
            freshness_note=self.note,
        )


def test_evaluate_uses_provider_verify_price_with_freshness(config, db):
    tid = db.insert_tracked_date(depart_date="2026-09-12", return_date="2026-09-14")
    db.arm_snipe(tid, 55.0)
    _insert_best(db, 50.0)
    tr = db.get_tracked_date(tid)
    res = evaluate_snipe(config, db, tr, _FakeProvider(price=52.0))
    assert res.triggered is True
    assert res.status == "triggered"
    assert res.confirmed_price_eur == 52.0
    assert res.freshness_note == "prix observé il y a ~2 h, vérifie au clic"
    assert res.deep_link.startswith("https://www.aviasales.com/")


def test_evaluate_provider_verify_none_falls_back_to_observed(config, db):
    tid = db.insert_tracked_date(depart_date="2026-09-12", return_date="2026-09-14")
    db.arm_snipe(tid, 55.0)
    _insert_best(db, 50.0)
    tr = db.get_tracked_date(tid)
    # verify_price returns None (provider re-check failed) -> use observed 50.
    res = evaluate_snipe(config, db, tr, _FakeProvider(price=None))
    assert res.triggered is True


def test_evaluate_no_data(config, db):
    tid = db.insert_tracked_date(depart_date="2026-09-12", return_date="2026-09-14")
    db.arm_snipe(tid, 55.0)
    tr = db.get_tracked_date(tid)
    res = evaluate_snipe(config, db, tr, _FakeClient(confirmed=50.0))
    assert res.status == "no_data"
    assert res.best_price_eur is None


# ----- re-verification helpers ----------------------------------------------


def test_reverify_price_parses_grand_total(config):
    client = _FakeClient(confirmed=49.9)
    assert reverify_price(config, client, {"id": "x"}) == 49.9


def test_reverify_price_returns_none_on_error(config):
    client = _FakeClient(confirmed=None)
    assert reverify_price(config, client, {"id": "x"}) is None


def test_extract_priced_total_falls_back_to_total():
    payload = {"data": {"flightOffers": [{"price": {"total": "61.0"}}]}}
    assert extract_priced_total(payload) == 61.0
    assert extract_priced_total({"data": {}}) is None


# ----- quota priority -------------------------------------------------------


def test_standard_scan_defers_when_quota_tight_and_snipe_close(config, db):
    config.travelpayouts_monthly_quota = 10  # fixture provider is travelpayouts
    config.quota_safety_ratio = 0.80  # ceiling = 8
    # routes = 2, one candidate -> reserve = 2.
    for _ in range(7):  # used = 7; 7 + 2 >= 8 -> defer
        db.record_api_call()
    assert standard_scan_should_defer(config, db, snipe_candidate_count=1) is True


def test_standard_scan_does_not_defer_without_snipes(config, db):
    config.travelpayouts_monthly_quota = 10
    config.quota_safety_ratio = 0.80
    for _ in range(7):
        db.record_api_call()
    assert standard_scan_should_defer(config, db, snipe_candidate_count=0) is False


def test_standard_scan_does_not_defer_when_quota_ample(config, db):
    config.travelpayouts_monthly_quota = 2000
    config.quota_safety_ratio = 0.80
    db.record_api_call()
    assert standard_scan_should_defer(config, db, snipe_candidate_count=1) is False
