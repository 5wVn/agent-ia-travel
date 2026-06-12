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


def test_overview_shows_stops_mention(client):
    c, dbi = client
    _login(c)
    # Cheapest offer on TLS→ORY is a direct flight; the overview's best-price
    # metric should carry the shared stops mention.
    dbi.insert_observation(
        origin="TLS", destination="ORY", depart_date="2030-09-12",
        return_date="2030-09-14", carrier="AF", price_eur=54.0,
        deep_link=None, raw_offer=None, transfers=0, return_transfers=0,
    )
    dbi.insert_observation(
        origin="TLS", destination="ORY", depart_date="2030-09-12",
        return_date="2030-09-14", carrier="IB", price_eur=120.0,
        deep_link=None, raw_offer=None, transfers=1, return_transfers=0,
    )
    resp = c.get("/")
    assert resp.status_code == 200
    # Cheapest (54 €) is the direct one -> "direct" shown next to best price.
    assert "direct" in resp.text


def test_dates_page_shows_best_price_with_stops(client):
    c, dbi = client
    _login(c)
    dbi.insert_tracked_date(depart_date="2030-09-12", return_date="2030-09-14")
    dbi.insert_observation(
        origin="TLS", destination="ORY", depart_date="2030-09-12",
        return_date="2030-09-14", carrier="IB", price_eur=99.0,
        deep_link=None, raw_offer=None, transfers=1, return_transfers=0,
    )
    resp = c.get("/dates")
    assert resp.status_code == 200
    assert "99" in resp.text
    assert "1 escale" in resp.text and "retour direct" in resp.text


# ----- PWA : manifest, icônes, navigation mobile -------------------------


def test_manifest_served_with_correct_content_type(client):
    c, _ = client
    # Public (pas d'auth requise) et content-type manifest correct.
    resp = c.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    data = resp.json()
    assert data["name"] == "Agent Travel"
    assert data["display"] == "standalone"
    # icônes 192 + 512 référencées
    srcs = {i["src"] for i in data["icons"]}
    assert "/static/icons/icon-192.png" in srcs
    assert "/static/icons/icon-512.png" in srcs


def test_pwa_icons_present_as_png(client):
    c, _ = client
    for path in (
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/apple-touch-180.png",
    ):
        resp = c.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"] == "image/png"
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"  # signature PNG


def test_pages_carry_mobile_chrome_and_manifest(client):
    c, _ = client
    _login(c)
    for path in ("/", "/routes", "/dates", "/status"):
        resp = c.get(path)
        assert resp.status_code == 200, path
        # tab bar mobile + lien manifest présents dans le chrome de base
        assert 'class="tabbar"' in resp.text, path
        assert 'rel="manifest"' in resp.text, path
        assert 'href="/manifest.webmanifest"' in resp.text, path
    # déconnexion accessible depuis la page Statut sur mobile
    assert 'class="m-logout' in c.get("/status").text
