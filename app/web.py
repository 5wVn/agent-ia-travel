"""Web dashboard (PLAN.md phase 4) — FastAPI served in the same asyncio loop.

Run as a uvicorn task on the bot's event loop (``uvicorn.Server(...).serve()``),
*not* ``uvicorn.run`` which would create its own loop. The dashboard shares the
:class:`app.db.Database` (and therefore its write lock) with the bot and the
scheduler, so Telegram and the dashboard write to the same source of truth.

Design constraints honoured here:
  - LAN only, password-protected (``DASHBOARD_PASSWORD``); without it the
    dashboard is disabled (see :func:`build_dashboard`).
  - server-rendered Jinja2 + htmx + Chart.js, all vendored in ``app/static`` —
    zero CDN, zero Node build.
  - no SQL in this module: every read/write goes through ``Database`` helpers,
    so the ``_write_lock`` discipline is respected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Config
from .db import Database
from .formatting import format_stops_roundtrip

logger = logging.getLogger(__name__)

NBSP = " "  # narrow no-break space, used before the euro sign / units

_FR_MONTHS = [
    "janv.", "févr.", "mars", "avr.", "mai", "juin",
    "juil.", "août", "sept.", "oct.", "nov.", "déc.",
]


def format_eur(value, *, decimals: int = 0) -> str:
    """Render a price the French way: ``54 €`` with a narrow no-break space.

    ``None`` renders as an em dash so templates never print ``None``.
    """
    if value is None:
        return "—"
    try:
        return f"{float(value):.{decimals}f}{NBSP}€"
    except (TypeError, ValueError):
        return "—"


def format_fr_date(value) -> str:
    """Render an ISO date/datetime as ``12 sept. 2026`` (French short month)."""
    if not value:
        return "—"
    s = str(value)[:10]
    try:
        y, m, d = s.split("-")
        return f"{int(d)} {_FR_MONTHS[int(m) - 1]} {y}"
    except (ValueError, IndexError):
        return s


def format_fr_datetime(value) -> str:
    """Render an ISO timestamp as ``12 sept. 2026 · 17h08``."""
    if not value:
        return "—"
    s = str(value)
    date_part = format_fr_date(s)
    if len(s) >= 16 and "T" in s:
        hh, mm = s[11:13], s[14:16]
        return f"{date_part} · {hh}h{mm}"
    return date_part


def format_fr_timerange(start, end) -> str:
    """Render a time window as ``17h–21h`` (or ``peu importe`` when unset)."""
    if not start:
        return "peu importe"
    a = str(start)[:5].replace(":", "h")
    if not end:
        return a
    return f"{a}–{str(end)[:5].replace(':', 'h')}"

_BASE = Path(__file__).resolve().parent
STATIC_DIR = _BASE / "static"
TEMPLATES_DIR = _BASE / "templates"

SESSION_COOKIE = "dashboard_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
IATA_RE = re.compile(r"^[A-Z]{3}$")
TIME_CHOICES = ["", "06:00", "08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"]


# ----- session signing (stdlib HMAC, no extra dependency) ----------------


def _sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def make_session_token(secret: str, now: Optional[float] = None) -> str:
    """Issue a signed session token ``<issued_at>.<hmac>``."""
    issued = str(int(now if now is not None else time.time()))
    return f"{issued}.{_sign(secret, issued)}"


def verify_session_token(secret: str, token: str, now: Optional[float] = None) -> bool:
    """Validate a session token's signature and TTL (constant-time compare)."""
    if not token or "." not in token:
        return False
    issued_s, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(secret, issued_s), sig):
        return False
    try:
        issued = int(issued_s)
    except ValueError:
        return False
    current = now if now is not None else time.time()
    return 0 <= current - issued <= SESSION_TTL_SECONDS


# ----- helpers -----------------------------------------------------------


def _provider_quota(config: Config, db: Database) -> dict:
    used = db.api_calls_this_month()
    total = config.provider_monthly_quota()
    ceiling = int(total * config.quota_safety_ratio)
    return {
        "provider": config.flight_provider,
        "used": used,
        "total": total,
        "ceiling": ceiling,
        "remaining": max(0, ceiling - used),
    }


def _price_situation(best, p25, median) -> str:
    """Classify the best price against the route's recent distribution.

    Returns one of ``good`` (≤ p25), ``neutral`` (p25..median) or ``high``
    (above median). Defaults to ``neutral`` when history is too thin.
    """
    if best is None or median is None:
        return "neutral"
    if p25 is not None and best <= p25:
        return "good"
    if best <= median:
        return "neutral"
    return "high"


def _price_trend(history: list) -> dict:
    """Direction (↘/→/↗) and delta € between the last two history points."""
    if len(history) < 2:
        return {"dir": "flat", "delta": None}
    prev = history[-2][1]
    last = history[-1][1]
    delta = last - prev
    if delta <= -1:
        return {"dir": "down", "delta": delta}
    if delta >= 1:
        return {"dir": "up", "delta": delta}
    return {"dir": "flat", "delta": delta}


def _overview_rows(config: Config, db: Database) -> list[dict]:
    win = config.baseline_window_days
    rows = []
    for r in db.active_routes():
        origin, destination = r["origin"], r["destination"]
        history = db.route_price_history(origin, destination)
        best_offer = db.route_best_offer(origin, destination)
        best = float(best_offer["price_eur"]) if best_offer is not None else None
        best_stops = (
            format_stops_roundtrip(
                _row_value(best_offer, "transfers"),
                _row_value(best_offer, "return_transfers"),
            )
            if best_offer is not None
            else None
        )
        median = db.route_median(origin, destination, win)
        p25 = db.route_percentile(origin, destination, 25.0, win)
        delta = None
        delta_pct = None
        if best is not None and median is not None:
            delta = best - median
            if median:
                delta_pct = (best - median) / median * 100.0
        rows.append(
            {
                "id": r["id"],
                "label": f"{origin}→{destination}",
                "origin": origin,
                "destination": destination,
                "best": best,
                "best_stops": best_stops,
                "median": median,
                "p25": p25,
                "delta": delta,
                "delta_pct": delta_pct,
                "situation": _price_situation(best, p25, median),
                "trend": _price_trend(history),
                "snipe_threshold": db.route_armed_threshold(origin, destination),
                "last_seen": _row_value(best_offer, "observed_at") if best_offer is not None else None,
                "labels": [d for d, _ in history],
                "prices": [p for _, p in history],
            }
        )
    return rows


def _row_value(row, key):
    """Read ``key`` from a sqlite3.Row or mapping, tolerating absence -> None."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _dates_rows(db: Database) -> list[dict]:
    """Tracked dates augmented with their best observed price + stops mention."""
    rows = []
    for d in db.all_tracked_dates_with_routes():
        best = db.best_current_price(d["depart_date"], d["return_date"])
        rows.append(
            {
                "row": d,
                "best": float(best["price_eur"]) if best is not None else None,
                "best_stops": (
                    format_stops_roundtrip(
                        _row_value(best, "transfers"),
                        _row_value(best, "return_transfers"),
                    )
                    if best is not None
                    else None
                ),
            }
        )
    return rows


def build_dashboard(config: Config, db: Database) -> Optional[FastAPI]:
    """Build the FastAPI app, or return None when the dashboard is disabled.

    Disabled = no ``DASHBOARD_PASSWORD``; we log an info line and the caller
    simply does not start the uvicorn task.
    """
    if not config.dashboard_enabled():
        logger.info(
            "Dashboard désactivé : DASHBOARD_PASSWORD non défini "
            "(définissez-le pour activer l'interface web LAN sur le port %d).",
            config.dashboard_port,
        )
        return None

    secret = config.dashboard_signing_secret()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["eur"] = format_eur
    templates.env.filters["fr_date"] = format_fr_date
    templates.env.filters["fr_datetime"] = format_fr_datetime
    templates.env.filters["fr_timerange"] = format_fr_timerange
    app = FastAPI(title="Agent IA Travel — Dashboard", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # PWA manifest : servi par une route dédiée pour garantir le bon
    # content-type (``application/manifest+json``) — le module mimetypes ne
    # connaît pas toujours ``.webmanifest``, et certains navigateurs refusent
    # un manifest servi en ``application/octet-stream``. Public (pas d'auth) :
    # un manifest ne révèle aucune donnée.
    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> Response:
        path = STATIC_DIR / "manifest.webmanifest"
        return Response(
            content=path.read_bytes(),
            media_type="application/manifest+json",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    def authed(request: Request) -> bool:
        return verify_session_token(secret, request.cookies.get(SESSION_COOKIE, ""))

    def redirect_login() -> RedirectResponse:
        return RedirectResponse("/login", status_code=302)

    # ----- auth ----------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, error: Optional[str] = None):
        return templates.TemplateResponse(
            request, "login.html", {"error": error}
        )

    @app.post("/login")
    async def login_submit(request: Request, password: str = Form("")):
        if hmac.compare_digest(password, config.dashboard_password or ""):
            resp = RedirectResponse("/", status_code=302)
            resp.set_cookie(
                SESSION_COOKIE,
                make_session_token(secret),
                httponly=True,
                samesite="lax",
                max_age=SESSION_TTL_SECONDS,
            )
            return resp
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Mot de passe incorrect."},
            status_code=401,
        )

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ----- overview ------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        if not authed(request):
            return redirect_login()
        return templates.TemplateResponse(
            request,
            "overview.html",
            {
                "active": "overview",
                "routes": _overview_rows(config, db),
                "alerts": db.recent_alerts(),
            },
        )

    # ----- routes / destinations -----------------------------------------

    @app.get("/routes", response_class=HTMLResponse)
    async def routes_page(request: Request, error: Optional[str] = None):
        if not authed(request):
            return redirect_login()
        return templates.TemplateResponse(
            request,
            "routes.html",
            {
                "active": "routes",
                "routes": db.all_routes(),
                "error": error,
            },
        )

    @app.post("/routes/add")
    async def routes_add(request: Request, origin: str = Form(""), destination: str = Form("")):
        if not authed(request):
            return redirect_login()
        o, d = origin.strip().upper(), destination.strip().upper()
        if not IATA_RE.match(o) or not IATA_RE.match(d):
            return RedirectResponse(
                "/routes?error=Codes+IATA+invalides+(3+lettres,+ex.+TLS).",
                status_code=302,
            )
        if o == d:
            return RedirectResponse(
                "/routes?error=Origine+et+destination+identiques.", status_code=302
            )
        try:
            db.add_route(o, d)
        except Exception:  # noqa: BLE001 — UNIQUE violation = doublon
            return RedirectResponse(
                "/routes?error=Cette+route+existe+déjà.", status_code=302
            )
        return RedirectResponse("/routes", status_code=302)

    @app.post("/routes/{route_id}/toggle")
    async def routes_toggle(request: Request, route_id: int):
        if not authed(request):
            return redirect_login()
        row = db.get_route(route_id)
        if row is not None:
            db.set_route_active(route_id, not bool(row["active"]))
        return RedirectResponse("/routes", status_code=302)

    # ----- dates ---------------------------------------------------------

    @app.get("/dates", response_class=HTMLResponse)
    async def dates_page(request: Request, error: Optional[str] = None):
        if not authed(request):
            return redirect_login()
        return templates.TemplateResponse(
            request,
            "dates.html",
            {
                "active": "dates",
                "dates": _dates_rows(db),
                "routes": db.active_routes(),
                "time_choices": TIME_CHOICES,
                "error": error,
            },
        )

    @app.post("/dates/add")
    async def dates_add(
        request: Request,
        depart_date: str = Form(""),
        return_date: str = Form(""),
        depart_time_from: str = Form(""),
        depart_time_to: str = Form(""),
        return_time_from: str = Form(""),
        return_time_to: str = Form(""),
        route_id: str = Form(""),
    ):
        if not authed(request):
            return redirect_login()
        if not depart_date.strip():
            return RedirectResponse(
                "/dates?error=Date+de+départ+requise.", status_code=302
            )
        rid: Optional[int] = None
        if route_id.strip():
            try:
                rid = int(route_id)
            except ValueError:
                rid = None
        db.insert_tracked_date(
            depart_date=depart_date.strip(),
            return_date=return_date.strip() or None,
            depart_time_from=depart_time_from.strip() or None,
            depart_time_to=depart_time_to.strip() or None,
            return_time_from=return_time_from.strip() or None,
            return_time_to=return_time_to.strip() or None,
            route_id=rid,
        )
        return RedirectResponse("/dates", status_code=302)

    @app.post("/dates/{tracked_id}/deactivate")
    async def dates_deactivate(request: Request, tracked_id: int):
        if not authed(request):
            return redirect_login()
        db.deactivate_tracked_date(tracked_id)
        return RedirectResponse("/dates", status_code=302)

    @app.post("/dates/{tracked_id}/arm")
    async def dates_arm(request: Request, tracked_id: int, threshold: str = Form("")):
        if not authed(request):
            return redirect_login()
        try:
            price = float(threshold)
        except ValueError:
            return RedirectResponse(
                "/dates?error=Seuil+invalide.", status_code=302
            )
        db.arm_snipe(tracked_id, price)
        return RedirectResponse("/dates", status_code=302)

    @app.post("/dates/{tracked_id}/disarm")
    async def dates_disarm(request: Request, tracked_id: int):
        if not authed(request):
            return redirect_login()
        db.disarm_snipe(tracked_id)
        return RedirectResponse("/dates", status_code=302)

    # ----- status --------------------------------------------------------

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        if not authed(request):
            return redirect_login()
        return templates.TemplateResponse(
            request,
            "status.html",
            {
                "active": "status",
                "quota": _provider_quota(config, db),
                "last_collection": db.last_collection_at(),
                "llm_enabled": config.llm_enabled(),
                "llm_model": config.llm_model,
                "active_routes": len(db.active_routes()),
                "armed_snipes": db.armed_snipes(),
            },
        )

    return app
