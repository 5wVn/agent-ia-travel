"""Tests for the SQLite access layer: schema, baseline, dedup, decisions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import Database, make_deal_key


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


def test_transfers_columns_present(db):
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(price_observations)")}
    assert "transfers" in cols
    assert "return_transfers" in cols


def test_migration_idempotent_preserves_existing_db(tmp_path):
    """A pre-existing db without the transfers columns is migrated in place,
    its rows preserved, and re-running init_schema is a no-op."""
    path = str(tmp_path / "legacy.db")
    # Build a "legacy" price_observations table lacking the transfers columns.
    legacy = Database(path)
    legacy.conn.executescript(
        """
        CREATE TABLE price_observations (
            id          INTEGER PRIMARY KEY,
            observed_at TEXT NOT NULL,
            origin      TEXT NOT NULL,
            destination TEXT NOT NULL,
            depart_date TEXT NOT NULL,
            return_date TEXT,
            carrier     TEXT NOT NULL,
            price_eur   REAL NOT NULL,
            deep_link   TEXT,
            raw_offer   TEXT,
            source      TEXT NOT NULL DEFAULT 'amadeus'
        );
        INSERT INTO price_observations
            (observed_at, origin, destination, depart_date, carrier, price_eur)
        VALUES ('2026-01-01T00:00:00Z', 'TLS', 'ORY', '2026-09-12', 'AF', 54.0);
        """
    )
    legacy.conn.commit()
    legacy.close()

    # First init: should add the columns without dropping the existing row.
    migrated = Database(path)
    migrated.init_schema()
    cols = {r["name"] for r in migrated.conn.execute("PRAGMA table_info(price_observations)")}
    assert "transfers" in cols and "return_transfers" in cols
    row = migrated.conn.execute("SELECT * FROM price_observations").fetchone()
    assert row["price_eur"] == 54.0
    assert row["transfers"] is None  # nullable, unknown on legacy rows
    # Second init on the same db must be a harmless no-op.
    migrated.init_schema()
    n = migrated.conn.execute("SELECT COUNT(*) AS n FROM price_observations").fetchone()["n"]
    assert n == 1
    migrated.close()


def test_insert_observation_persists_transfers(db):
    obs_id = db.insert_observation(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", carrier="AF", price_eur=54.0,
        deep_link=None, raw_offer=None, transfers=0, return_transfers=1,
    )
    row = db.get_observation(obs_id)
    assert row["transfers"] == 0
    assert row["return_transfers"] == 1


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
