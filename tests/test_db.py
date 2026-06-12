"""Tests for the SQLite access layer: schema, baseline, dedup, decisions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import make_deal_key


def test_schema_tables_exist(db):
    cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in cur.fetchall()}
    for expected in [
        "price_observations",
        "tracked_dates",
        "decisions",
        "flight_scores",
        "alerts_sent",
    ]:
        assert expected in names


def test_wal_mode_enabled(db):
    mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_insert_and_get_observation(db):
    obs_id = db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", carrier="AF", price_eur=54.0,
        deep_link=None, raw_offer={"x": 1},
    )
    row = db.get_observation(obs_id)
    assert row["price_eur"] == 54.0
    assert '"x":1' in row["raw_offer"]  # raw JSON preserved


def test_baseline_median_and_percentile(db):
    now = datetime.now(timezone.utc)
    for price in [50, 60, 70, 80, 90, 100]:
        db.insert_observation(
            origin="TLS", destination="ORY", depart_date="2026-09-12",
            return_date="2026-09-14", carrier="AF", price_eur=float(price),
            deep_link=None, raw_offer=None, observed_at=now.isoformat(),
        )
    median = db.baseline_median("TLS", "ORY", "2026-09-12", 30, now)
    assert median == 75.0  # mean of 70 and 80
    p10 = db.baseline_percentile("TLS", "ORY", "2026-09-12", 10.0, 30, now)
    assert 50.0 <= p10 <= 60.0


def test_baseline_excludes_old_observations(db):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", carrier="AF", price_eur=999.0,
        deep_link=None, raw_offer=None, observed_at=old,
    )
    db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", carrier="AF", price_eur=60.0,
        deep_link=None, raw_offer=None, observed_at=now.isoformat(),
    )
    median = db.baseline_median("TLS", "ORY", "2026-09-12", 30, now)
    assert median == 60.0  # the 40-day-old 999 is outside the window


def test_baseline_no_history_returns_none(db):
    assert db.baseline_median("TLS", "ORY", "2026-09-12") is None


def test_make_deal_key_buckets_price():
    k1 = make_deal_key("TLS", "ORY", "2026-09-12", "2026-09-14", "AF", 54.0)
    k2 = make_deal_key("TLS", "ORY", "2026-09-12", "2026-09-14", "AF", 57.0)
    k3 = make_deal_key("TLS", "ORY", "2026-09-12", "2026-09-14", "AF", 64.0)
    assert k1 == k2  # same 10-EUR bucket
    assert k1 != k3  # different bucket


def test_try_register_alert_dedup(db):
    obs_id = db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", carrier="AF", price_eur=54.0,
        deep_link=None, raw_offer=None,
    )
    key = make_deal_key("TLS", "ORY", "2026-09-12", "2026-09-14", "AF", 54.0)
    assert db.try_register_alert(obs_id, key) is True
    assert db.try_register_alert(obs_id, key) is False  # duplicate
    assert db.alert_exists(key) is True


def test_tracked_dates_lifecycle(db):
    tid = db.insert_tracked_date(
        depart_date="2026-09-12", return_date="2026-09-14",
        depart_time_from="17:00", depart_time_to="21:00",
    )
    active = db.active_tracked_dates()
    assert len(active) == 1
    match = db.matching_tracked_date("2026-09-12", "2026-09-14")
    assert match is not None
    db.deactivate_tracked_date(tid)
    assert len(db.active_tracked_dates()) == 0


def test_log_decision(db):
    obs_id = db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date=None, carrier="AF", price_eur=54.0,
        deep_link=None, raw_offer=None,
    )
    db.log_decision(obs_id, "book", note="ok")
    decisions = db.recent_decisions()
    assert len(decisions) == 1
    assert decisions[0]["action"] == "book"


def test_upsert_score_idempotent(db):
    obs_id = db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date=None, carrier="AF", price_eur=54.0,
        deep_link=None, raw_offer=None,
    )
    db.upsert_score(obs_id, 80.0, {"prix": 100.0})
    db.upsert_score(obs_id, 85.0, {"prix": 90.0})  # replace
    row = db.get_score(obs_id)
    assert row["score"] == 85.0


def test_api_call_counter(db):
    db.record_api_call("flight-offers")
    db.record_api_call("flight-offers")
    assert db.api_calls_this_month() == 2
