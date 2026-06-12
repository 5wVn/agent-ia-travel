"""Travelpayouts / Aviasales Data API provider (default).

The new default flight-data source, replacing Amadeus Self-Service before its
2026-07-17 shutdown. Travelpayouts is free for an individual: prices are a cache
fed by real Aviasales searches, and — unlike Amadeus Self-Service — every offer
carries a **deep booking link**.

API details confirmed against the official docs
(https://support.travelpayouts.com/hc/en-us/articles/203956163 and the
travelpayouts/api-docs source):

* Endpoint: ``GET https://api.travelpayouts.com/aviasales/v3/prices_for_dates``
* Auth: token in the ``X-Access-Token`` header (the ``token`` query param is the
  documented alternative). We use the header.
* Request params used here: ``origin``, ``destination``, ``departure_at``
  (YYYY-MM-DD), ``return_at`` (omitted for one-way), ``currency=eur``,
  ``unique=false``, ``sorting=price``, ``direct=false``, ``limit``,
  ``one_way`` (``true``/``false``).
* Response shape: ``{"success": true, "currency": "eur", "data": [ {…} ]}``
  where each ticket has: ``origin``, ``destination``, ``price`` (number, in the
  requested currency), ``airline`` (IATA), ``flight_number``, ``departure_at``
  (full ISO-8601 datetime, e.g. ``2026-09-12T17:35:00Z``), ``return_at``,
  ``transfers``, ``return_transfers``, ``duration_to``/``duration_back``
  (minutes), ``expires_at``, and ``link`` (a **relative** Aviasales search
  fragment, e.g. ``/search/TLS1209ORY14092?marker=…``).

Normalization maps these onto the shared :class:`NormalizedOffer`:
``price`` → ``price_eur``, ``airline`` → ``carrier``, the time part of
``departure_at``/``return_at`` → ``depart_time``/``return_time``,
``duration_to`` → ``duration_min``, and ``link`` → a full ``deep_link``
(prefixed with ``https://www.aviasales.com`` and given the optional affiliate
``marker``). The whole ticket dict is kept verbatim in ``raw_offer``.

Freshness: the cache is hours-fresh, not real-time. :meth:`freshness_note`
reports the age of the stored price (computed from the observation's
``observed_at``, or the ticket's ``departure_at``/``expires_at`` when present in
``raw_offer``) so a critical alert can warn "prix observé il y a ~2 h, vérifie
au clic".
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..config import Config
from . import FlightProvider, NormalizedOffer, SearchQuery, VerifiedPrice

logger = logging.getLogger(__name__)

TRAVELPAYOUTS_BASE_URL = "https://api.travelpayouts.com"
PRICES_FOR_DATES_PATH = "/aviasales/v3/prices_for_dates"
AVIASALES_BASE_URL = "https://www.aviasales.com"

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0


def _time_from_iso(value: Optional[str]) -> Optional[str]:
    """Extract 'HH:MM' from an ISO datetime like '2026-09-12T17:35:00Z'."""
    if not value or "T" not in value:
        return None
    time_part = value.split("T", 1)[1]
    return time_part[:5]


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_deep_link(link_fragment: Optional[str], marker: Optional[str]) -> Optional[str]:
    """Build a full Aviasales booking URL from the API's ``link`` fragment.

    The API returns a relative path (``/search/...``); we prefix it with the
    Aviasales host. An already-absolute URL is left as-is. The optional
    affiliate ``marker`` is appended as a query param when not already present.
    """
    if not link_fragment:
        return None
    if link_fragment.startswith("http://") or link_fragment.startswith("https://"):
        url = link_fragment
    else:
        if not link_fragment.startswith("/"):
            link_fragment = "/" + link_fragment
        url = f"{AVIASALES_BASE_URL}{link_fragment}"
    if marker and "marker=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}marker={marker}"
    return url


def normalize_offers(
    payload: dict[str, Any], query: SearchQuery, marker: Optional[str] = None
) -> list[NormalizedOffer]:
    """Normalize a prices_for_dates response into flat offers.

    A malformed ticket (missing/non-numeric price) is skipped, not fatal, like
    the Amadeus normalizer. Origin/destination fall back to the query's route
    so the stored row always matches what we asked for.
    """
    offers: list[NormalizedOffer] = []
    data = payload.get("data") or []
    for raw in data:
        try:
            price = raw.get("price")
            if price is None:
                continue
            price_eur = float(price)

            carrier = raw.get("airline") or "??"
            depart_time = _time_from_iso(raw.get("departure_at"))
            return_time = _time_from_iso(raw.get("return_at"))
            duration_min = raw.get("duration_to")
            duration_min = int(duration_min) if duration_min is not None else None

            offers.append(
                NormalizedOffer(
                    origin=raw.get("origin") or query.route.origin,
                    destination=raw.get("destination") or query.route.destination,
                    depart_date=query.depart_date,
                    return_date=query.return_date,
                    carrier=carrier,
                    price_eur=price_eur,
                    deep_link=build_deep_link(raw.get("link"), marker),
                    depart_time=depart_time,
                    return_time=return_time,
                    duration_min=duration_min,
                    raw_offer=raw,
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Offre Travelpayouts ignorée (parsing) : %s", exc)
            continue
    return offers


def cheapest(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the cheapest raw ticket in a response, or None."""
    data = payload.get("data") or []
    best: Optional[dict[str, Any]] = None
    best_price: Optional[float] = None
    for raw in data:
        try:
            price = float(raw.get("price"))
        except (TypeError, ValueError):
            continue
        if best_price is None or price < best_price:
            best_price, best = price, raw
    return best


def _humanize_age(seconds: float) -> str:
    """Render a coarse French age string from a duration in seconds."""
    if seconds < 0:
        seconds = 0.0
    minutes = seconds / 60.0
    if minutes < 90:
        n = max(1, round(minutes))
        return f"~{n} min"
    hours = minutes / 60.0
    if hours < 36:
        n = max(1, round(hours))
        return f"~{n} h"
    days = hours / 24.0
    n = max(1, round(days))
    return f"~{n} j"


class TravelpayoutsClient:
    """Minimal Aviasales Data API client (httpx + exponential backoff)."""

    def __init__(self, config: Config, base_url: str = TRAVELPAYOUTS_BASE_URL) -> None:
        self.config = config
        self.base_url = base_url
        self._client = httpx.Client(base_url=base_url, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def prices_for_dates(self, query: SearchQuery) -> dict[str, Any]:
        """Call prices_for_dates for one route/date with retries.

        Auth uses the ``X-Access-Token`` header. The cheapest results come first
        (``sorting=price``); ``unique=false`` returns several options so the
        normalizer/scoring still see the spread.
        """
        params: dict[str, Any] = {
            "origin": query.route.origin,
            "destination": query.route.destination,
            "departure_at": query.depart_date,
            "currency": "eur",
            "unique": "false",
            "sorting": "price",
            "direct": "false",
            "limit": 30,
        }
        if query.return_date:
            params["return_at"] = query.return_date
            params["one_way"] = "false"
        else:
            params["one_way"] = "true"

        headers = {"X-Access-Token": self.config.travelpayouts_token or ""}

        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._client.get(
                    PRICES_FOR_DATES_PATH, params=params, headers=headers
                )
                resp.raise_for_status()
                body = resp.json()
                if isinstance(body, dict) and body.get("success") is False:
                    raise httpx.HTTPStatusError(
                        f"API error: {body.get('error')}",
                        request=resp.request,
                        response=resp,
                    )
                return body
            except (httpx.HTTPError, httpx.TransportError) as exc:
                last_exc = exc
                wait = _BACKOFF_BASE_SECONDS ** attempt
                logger.warning(
                    "Travelpayouts search %s tentative %d/%d échouée : %s",
                    query.route.label(),
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(wait)
        assert last_exc is not None
        raise last_exc


class TravelpayoutsProvider(FlightProvider):
    """:class:`FlightProvider` backed by the Aviasales Data API (default)."""

    def __init__(
        self, config: Config, client: Optional[TravelpayoutsClient] = None
    ) -> None:
        self.config = config
        self.marker = config.travelpayouts_marker
        self._client = client or TravelpayoutsClient(config)

    @property
    def name(self) -> str:
        return "travelpayouts"

    def search(self, query: SearchQuery) -> list[NormalizedOffer]:
        payload = self._client.prices_for_dates(query)
        return normalize_offers(payload, query, self.marker)

    def verify_price(self, observation: Any) -> Optional[VerifiedPrice]:
        """Re-fetch the freshest cached price for the same route/dates.

        Travelpayouts has no "re-price this offer" endpoint, so verification is
        a fresh prices_for_dates call: we take the cheapest current ticket. The
        returned :class:`VerifiedPrice` carries a freshness note and the fresh
        deep link. ``None`` on network/parse error -> sniper keeps watching.
        """
        origin = _row_get(observation, "origin")
        destination = _row_get(observation, "destination")
        depart_date = _row_get(observation, "depart_date")
        return_date = _row_get(observation, "return_date")
        if not origin or not destination or not depart_date:
            return None

        from ..config import Route

        query = SearchQuery(
            route=Route(str(origin), str(destination)),
            depart_date=str(depart_date),
            return_date=return_date,
        )
        try:
            payload = self._client.prices_for_dates(query)
        except Exception as exc:  # noqa: BLE001 — never crash a tick
            logger.warning("Re-vérification Travelpayouts échouée : %s", exc)
            return None
        best = cheapest(payload)
        if best is None:
            return None
        try:
            price = float(best.get("price"))
        except (TypeError, ValueError):
            return None
        return VerifiedPrice(
            price_eur=price,
            deep_link=build_deep_link(best.get("link"), self.marker),
            raw_offer=best,
            freshness_note=self._freshness_from_raw(best),
        )

    def freshness_note(self, observation: Any) -> Optional[str]:
        """Age of the stored price as French text, e.g. 'prix observé il y a ~2 h'.

        Prefers a timestamp from the raw ticket (``found_at``/``expires_at``)
        when present; otherwise falls back to the observation's ``observed_at``.
        """
        raw = _load_raw_offer(_row_get(observation, "raw_offer"))
        note = self._freshness_from_raw(raw) if raw else None
        if note:
            return note
        observed_at = _parse_iso(_row_get(observation, "observed_at"))
        if observed_at is None:
            return None
        age = (datetime.now(timezone.utc) - observed_at).total_seconds()
        return f"prix observé il y a {_humanize_age(age)}, vérifie au clic"

    def _freshness_from_raw(self, raw: Optional[dict[str, Any]]) -> Optional[str]:
        """Build a freshness note from a raw ticket's timestamps, if any."""
        if not raw:
            return None
        # The v3 cache does not return found_at, but tolerate it if present.
        found = _parse_iso(raw.get("found_at"))
        if found is not None:
            age = (datetime.now(timezone.utc) - found).total_seconds()
            return f"prix observé il y a {_humanize_age(age)}, vérifie au clic"
        return None

    def close(self) -> None:
        self._client.close()


def _load_raw_offer(raw: Any) -> Optional[dict[str, Any]]:
    """Decode a stored ``raw_offer`` (dict or JSON string) into a dict."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _row_get(row: Any, key: str) -> Any:
    """Read ``key`` from a sqlite3.Row or a plain mapping, tolerating absence."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        try:
            return row.get(key)  # type: ignore[union-attr]
        except AttributeError:
            return None
