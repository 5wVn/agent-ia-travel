"""Dashboard tests via FastAPI TestClient (httpx). No network.

Covers: dashboard disabled without password, login required (302 redirect),
login flow + cookie, route add/toggle (IATA validation + duplicate refused),
tracked-date add with time window + route, snipe arming, and the overview
rendering 200 with seeded data.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Config, Route
from app.web import build_dashboard, make_session_token, verify_session_token


def _config(password="secret"):
    return Config(
        telegram_bot_token="x",
        telegram_chat_id="123",
        anthropic_api_key=None,
        routes=[Route("TLS", "ORY"), Route("TLS", "CDG")],
        dashboard_password=password,
        dashboard_secret="testsecret",
    )


@pytest.fixture
def seeded_db(db):
    """A db seeded with the two default routes (both directions trimmed)."""
    db.seed_routes([("TLS", "ORY"), ("TLS", "CDG")])
    return db


@pytest.fixture
def client(seeded_db):
    app = build_dashboard(_config(), seeded_db)
    return TestClient(app), seeded_db


# ----- enable/disable + session signing ----------------------------------


def test_dashboard_disabled_without_password(db):
    assert build_dashboard(_config(password=None), db) is None
    assert build_dashboard(_config(password=""), db) is None


def test_session_token_roundtrip():
    tok = make_session_token("s")
    assert verify_session_token("s", tok) is True
    assert verify_session_token("other", tok) is False
    assert verify_session_token("s", "garbage") is False


def test_session_token_expired():
    tok = make_session_token("s", now=0)
    assert verify_session_token("s", tok, now=10**9) is False


# ----- auth gating -------------------------------------------------------


def test_protected_routes_redirect_without_cookie(client):
    c, _ = client
    for path in ("/", "/routes", "/dates", "/status"):
        resp = c.get(path, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"


def test_login_page_is_public(client):
    c, _ = client
    assert c.get("/login").status_code == 200


def test_login_wrong_password_401(client):
    c, _ = client
    resp = c.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert resp.status_code == 401


def test_login_then_overview_200(client):
    c, _ = client
    resp = c.post("/login", data={"password": "secret"}, follow_redirects=False)
    assert resp.status_code == 302
    assert c.cookies.get("dashboard_session")
    over = c.get("/")
    assert over.status_code == 200
    assert "Vue d'ensemble" in over.text


# ----- routes / destinations ---------------------------------------------


def _login(c):
    c.post("/login", data={"password": "secret"})


def test_routes_add_valid(client):
    c, dbi = client
    _login(c)
    resp = c.post(
        "/routes/add", data={"origin": "tls", "destination": "bod"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    pairs = {(r["origin"], r["destination"]) for r in dbi.all_routes()}
    assert ("TLS", "BOD") in pairs  # normalized to upper-case


def test_routes_add_invalid_iata_rejected(client):
    c, dbi = client
    _login(c)
    before = len(dbi.all_routes())
    resp = c.post(
        "/routes/add", data={"origin": "TOULOUSE", "destination": "OR"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error" in resp.headers["location"]
    assert len(dbi.all_routes()) == before


def test_routes_add_duplicate_refused(client):
    c, dbi = client
    _login(c)
    before = len(dbi.all_routes())
    resp = c.post(
        "/routes/add", data={"origin": "TLS", "destination": "ORY"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error" in resp.headers["location"]
    assert len(dbi.all_routes()) == before  # TLS-ORY already seeded


def test_routes_toggle(client):
    c, dbi = client
    _login(c)
    rid = dbi.active_routes()[0]["id"]
    c.post(f"/routes/{rid}/toggle", follow_redirects=False)
    assert dbi.get_route(rid)["active"] == 0
    c.post(f"/routes/{rid}/toggle", follow_redirects=False)
    assert dbi.get_route(rid)["active"] == 1


# ----- dates + snipe -----------------------------------------------------


def test_dates_add_with_window_and_route(client):
    c, dbi = client
    _login(c)
    rid = dbi.active_routes()[0]["id"]
    resp = c.post(
        "/dates/add",
        data={
            "depart_date": "2030-06-12",
            "return_date": "2030-06-15",
            "depart_time_from": "08:00",
            "depart_time_to": "12:00",
            "route_id": str(rid),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    rows = dbi.all_tracked_dates()
    assert len(rows) == 1
    assert rows[0]["depart_date"] == "2030-06-12"
    assert rows[0]["depart_time_from"] == "08:00"
    assert rows[0]["route_id"] == rid


def test_dates_arm_and_disarm_snipe(client):
    c, dbi = client
    _login(c)
    tid = dbi.insert_tracked_date(depart_date="2030-07-01", return_date="2030-07-03")
    c.post(f"/dates/{tid}/arm", data={"threshold": "55"}, follow_redirects=False)
    tr = dbi.get_tracked_date(tid)
    assert tr["snipe_state"] == "armed"
    assert tr["snipe_price_eur"] == 55.0
    c.post(f"/dates/{tid}/disarm", follow_redirects=False)
    assert dbi.get_tracked_date(tid)["snipe_state"] is None


def test_overview_renders_with_seeded_prices(client):
    c, dbi = client
    _login(c)
    dbi.insert_observation(
        origin="TLS", destination="ORY", depart_date="2030-09-12",
        return_date="2030-09-14", carrier="AF", price_eur=54.0,
        deep_link=None, raw_offer=None,
    )
    resp = c.get("/")
    assert resp.status_code == 200
    assert "TLS→ORY" in resp.text
    assert "54" in resp.text
