"""Tests for routes-as-data: seeding idempotency and collector iteration."""

from __future__ import annotations

from app.collector import active_routes, build_queries, build_snipe_queries
from app.config import Route


SEED = [("TLS", "ORY"), ("ORY", "TLS"), ("TLS", "CDG"), ("CDG", "TLS")]


def test_routes_table_exists(db):
    cur = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in cur.fetchall()}
    assert "routes" in names


def test_route_id_column_present(db):
    cols = db._column_names("tracked_dates")
    assert "route_id" in cols


def test_seed_routes_inserts_once(db):
    db.seed_routes(SEED)
    assert len(db.all_routes()) == 4
    # Second call must not duplicate or re-seed.
    db.seed_routes(SEED)
    assert len(db.all_routes()) == 4


def test_seed_routes_no_reseed_when_all_inactive(db):
    db.seed_routes(SEED)
    for r in db.all_routes():
        db.set_route_active(r["id"], False)
    assert db.active_routes() == []
    # Even with every route inactive, seeding must not re-add the defaults.
    db.seed_routes(SEED)
    assert len(db.all_routes()) == 4
    assert db.active_routes() == []


def test_add_route_normalizes_and_rejects_duplicate(db):
    db.add_route("tls", "bod")
    pairs = {(r["origin"], r["destination"]) for r in db.all_routes()}
    assert ("TLS", "BOD") in pairs
    import sqlite3
    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        db.add_route("TLS", "BOD")


def test_active_routes_falls_back_to_config_when_empty(config, db):
    # No routes seeded yet -> fall back to config routes so collection still runs.
    routes = active_routes(config, db)
    assert routes == config.routes


def test_collector_iterates_over_db_routes(config, db):
    db.seed_routes([("TLS", "ORY")])  # only one active route in db
    routes = active_routes(config, db)
    assert routes == [Route("TLS", "ORY")]
    queries = build_queries(config, db)
    # 1 db route x 8 weekends, regardless of the 2 config routes.
    assert len(queries) == config.weekend_count
    assert {q.route.label() for q in queries} == {"TLS->ORY"}


def test_collector_respects_route_deactivation(config, db):
    db.seed_routes([("TLS", "ORY"), ("TLS", "CDG")])
    ory = next(r for r in db.all_routes() if r["destination"] == "ORY")
    db.set_route_active(ory["id"], False)
    queries = build_queries(config, db)
    assert {q.route.label() for q in queries} == {"TLS->CDG"}


def test_build_snipe_queries_uses_db_routes(config, db):
    db.seed_routes([("TLS", "ORY")])
    rows = [{"depart_date": "2030-01-04", "return_date": "2030-01-06"}]
    queries = build_snipe_queries(config, db, rows)
    assert len(queries) == 1
    assert queries[0].route.label() == "TLS->ORY"
