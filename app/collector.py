"""Amadeus Self-Service collector — fetches flight offers and stores them.

Uses OAuth2 ``client_credentials`` for the access token and the
``v2/shopping/flight-offers`` endpoint. Network errors retry with exponential
backoff (3 attempts) and never crash the process: a failed tick is logged and
skipped. Every API call is counted in the database so cadence can be reduced
when approaching the configured monthly quota.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from .config import Config, Route, upcoming_weekends
from .db import Database

logger = logging.getLogger(__name__)

AMADEUS_BASE_URL = "https://test.api.amadeus.com"
TOKEN_PATH = "/v1/security/oauth2/token"
OFFERS_PATH = "/v2/shopping/flight-offers"
PRICING_PATH = "/v1/shopping/flight-offers/pricing"

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0


@dataclass
class SearchQuery:
    """One Amadeus search: a route plus a departure/return date pair."""

    route: Route
    depart_date: str
    return_date: Optional[str]


@dataclass
class NormalizedOffer:
    """A flattened, cheapest-per-itinerary offer ready for insertion."""

    origin: str
    destination: str
    depart_date: str
    return_date: Optional[str]
    carrier: str
    price_eur: float
    deep_link: Optional[str]
    depart_time: Optional[str]
    return_time: Optional[str]
    duration_min: Optional[int]
    raw_offer: dict[str, Any]


def _iso_duration_to_minutes(value: Optional[str]) -> Optional[int]:
    """Convert an ISO-8601 duration like 'PT1H15M' into minutes."""
    if not value or not value.startswith("PT"):
        return None
    hours = 0
    minutes = 0
    num = ""
    for ch in value[2:]:
        if ch.isdigit():
            num += ch
        elif ch == "H":
            hours = int(num or 0)
            num = ""
        elif ch == "M":
            minutes = int(num or 0)
            num = ""
        else:
            num = ""
    return hours * 60 + minutes


def _segment_time(at_value: Optional[str]) -> Optional[str]:
    """Extract 'HH:MM' from an Amadeus datetime like '2026-09-12T17:35:00'."""
    if not at_value or "T" not in at_value:
        return None
    time_part = at_value.split("T", 1)[1]
    return time_part[:5]


def normalize_offers(
    payload: dict[str, Any], query: SearchQuery
) -> list[NormalizedOffer]:
    """Normalize an Amadeus flight-offers response into flat offers.

    The first itinerary's first segment provides departure info; for a round
    trip the second itinerary's first segment provides the return info. The
    validating carrier (or first segment carrier) is used as the carrier code.
    """
    offers: list[NormalizedOffer] = []
    data = payload.get("data") or []
    for raw in data:
        try:
            price = raw.get("price", {})
            total = price.get("grandTotal") or price.get("total")
            if total is None:
                continue
            price_eur = float(total)

            itineraries = raw.get("itineraries") or []
            if not itineraries:
                continue

            outbound = itineraries[0]
            out_segments = outbound.get("segments") or []
            if not out_segments:
                continue
            first_seg = out_segments[0]
            depart_time = _segment_time(
                (first_seg.get("departure") or {}).get("at")
            )
            duration_min = _iso_duration_to_minutes(outbound.get("duration"))

            return_time: Optional[str] = None
            if len(itineraries) > 1:
                inbound = itineraries[1]
                in_segments = inbound.get("segments") or []
                if in_segments:
                    return_time = _segment_time(
                        (in_segments[0].get("departure") or {}).get("at")
                    )

            validating = raw.get("validatingAirlineCodes") or []
            carrier = validating[0] if validating else first_seg.get(
                "carrierCode", "??"
            )

            offers.append(
                NormalizedOffer(
                    origin=query.route.origin,
                    destination=query.route.destination,
                    depart_date=query.depart_date,
                    return_date=query.return_date,
                    carrier=carrier,
                    price_eur=price_eur,
                    deep_link=None,  # Amadeus Self-Service returns no booking URL
                    depart_time=depart_time,
                    return_time=return_time,
                    duration_min=duration_min,
                    raw_offer=raw,
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Offre Amadeus ignorée (parsing) : %s", exc)
            continue
    return offers


class AmadeusClient:
    """Minimal Amadeus Self-Service client with token caching and retries."""

    def __init__(self, config: Config, base_url: str = AMADEUS_BASE_URL) -> None:
        self.config = config
        self.base_url = base_url
        self._token: Optional[str] = None
        self._token_expiry: datetime = datetime.now(timezone.utc)
        self._client = httpx.Client(base_url=base_url, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def _ensure_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and now < self._token_expiry:
            return self._token
        resp = self._client.post(
            TOKEN_PATH,
            data={
                "grant_type": "client_credentials",
                "client_id": self.config.amadeus_client_id,
                "client_secret": self.config.amadeus_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        expires_in = int(body.get("expires_in", 1800))
        # Refresh a minute early to avoid edge-of-expiry failures.
        self._token_expiry = now + timedelta(seconds=max(60, expires_in - 60))
        return self._token

    def search(self, query: SearchQuery) -> dict[str, Any]:
        """Run one flight-offers search with exponential-backoff retries."""
        params: dict[str, Any] = {
            "originLocationCode": query.route.origin,
            "destinationLocationCode": query.route.destination,
            "departureDate": query.depart_date,
            "adults": 1,
            "currencyCode": "EUR",
            "max": 50,
        }
        if query.return_date:
            params["returnDate"] = query.return_date

        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                token = self._ensure_token()
                resp = self._client.get(
                    OFFERS_PATH,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 401:
                    # Token rejected: force refresh and retry.
                    self._token = None
                    raise httpx.HTTPStatusError(
                        "401", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, httpx.TransportError) as exc:
                last_exc = exc
                wait = _BACKOFF_BASE_SECONDS ** attempt
                logger.warning(
                    "Amadeus search %s tentative %d/%d échouée : %s",
                    query.route.label(),
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    def price_offer(self, raw_offer: dict[str, Any]) -> dict[str, Any]:
        """Re-price one raw flight offer via Flight Offers Price (anti-stale).

        Used by the price sniper before raising a critical alert: the stored
        ``raw_offer`` is POSTed back and Amadeus returns the confirmed live
        price. Retries with exponential backoff like :meth:`search`.
        """
        body = {
            "data": {
                "type": "flight-offers-pricing",
                "flightOffers": [raw_offer],
            }
        }
        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                token = self._ensure_token()
                resp = self._client.post(
                    PRICING_PATH,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                if resp.status_code == 401:
                    self._token = None
                    raise httpx.HTTPStatusError(
                        "401", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, httpx.TransportError) as exc:
                last_exc = exc
                wait = _BACKOFF_BASE_SECONDS ** attempt
                logger.warning(
                    "Amadeus pricing tentative %d/%d échouée : %s",
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(wait)
        assert last_exc is not None
        raise last_exc


def build_queries(config: Config, db: Database) -> list[SearchQuery]:
    """Build the list of searches: upcoming weekends + active tracked dates.

    Each (depart, return) pair is searched on every configured route. Duplicate
    pairs (same depart/return) are de-duplicated per route.
    """
    pairs: list[tuple[str, Optional[str]]] = list(
        upcoming_weekends(config.weekend_count)
    )
    for row in db.active_tracked_dates():
        pairs.append((row["depart_date"], row["return_date"]))

    seen: set[tuple[str, str, Optional[str]]] = set()
    queries: list[SearchQuery] = []
    for route in config.routes:
        for depart, ret in pairs:
            key = (route.label(), depart, ret)
            if key in seen:
                continue
            seen.add(key)
            queries.append(SearchQuery(route=route, depart_date=depart, return_date=ret))
    return queries


def build_snipe_queries(config: Config, rows: list[Any]) -> list[SearchQuery]:
    """Build searches for a given set of snipe-candidate tracked-date rows.

    Each (depart, return) pair is searched on every configured route, with the
    same per-route de-duplication as :func:`build_queries`.
    """
    seen: set[tuple[str, str, Optional[str]]] = set()
    queries: list[SearchQuery] = []
    for route in config.routes:
        for row in rows:
            depart, ret = row["depart_date"], row["return_date"]
            key = (route.label(), depart, ret)
            if key in seen:
                continue
            seen.add(key)
            queries.append(
                SearchQuery(route=route, depart_date=depart, return_date=ret)
            )
    return queries


def quota_allows(config: Config, db: Database) -> bool:
    """Return False once we hit 80% of the configured monthly quota."""
    used = db.api_calls_this_month()
    ceiling = int(config.amadeus_monthly_quota * config.quota_safety_ratio)
    if used >= ceiling:
        logger.warning(
            "Quota Amadeus atteint (%d/%d, plafond %d) — collecte suspendue.",
            used,
            config.amadeus_monthly_quota,
            ceiling,
        )
        return False
    return True


def standard_scan_should_defer(
    config: Config, db: Database, snipe_candidate_count: int
) -> bool:
    """True when the weekend scan should skip its tick to spare quota.

    Sniper collections are prioritized: when the monthly usage is within one
    boosted-watch budget of the safety ceiling *and* at least one snipe is in
    its proximity window, the standard scan defers so the sniper keeps enough
    quota to re-check its dates. When no snipe is close, nothing defers.
    """
    if snipe_candidate_count <= 0:
        return False
    used = db.api_calls_this_month()
    ceiling = int(config.amadeus_monthly_quota * config.quota_safety_ratio)
    # Budget the sniper needs before the next standard scan: one boosted-watch
    # pass per candidate, across every route.
    reserve = snipe_candidate_count * max(1, len(config.routes))
    return used + reserve >= ceiling


def run_collection(
    config: Config,
    db: Database,
    client: Optional[AmadeusClient] = None,
    queries: Optional[list[SearchQuery]] = None,
) -> list[int]:
    """Run one collection tick. Returns inserted observation ids.

    By default the queries cover upcoming weekends + tracked dates. The boosted
    sniper watch passes an explicit, narrow ``queries`` list so it only spends
    quota on the dates that matter.

    Robust by design: a failure on one query is logged and skipped; the tick
    continues. Quota is checked before each call so we stop cleanly at the
    safety ceiling.
    """
    own_client = client is None
    if client is None:
        client = AmadeusClient(config)

    inserted: list[int] = []
    try:
        if queries is None:
            queries = build_queries(config, db)
        logger.info("Collecte : %d recherches planifiées.", len(queries))
        for query in queries:
            if not quota_allows(config, db):
                break
            try:
                db.record_api_call(OFFERS_PATH)
                payload = client.search(query)
            except Exception as exc:  # noqa: BLE001 — never crash a tick
                logger.error(
                    "Recherche échouée %s %s->%s : %s",
                    query.depart_date,
                    query.route.origin,
                    query.route.destination,
                    exc,
                )
                continue
            for offer in normalize_offers(payload, query):
                obs_id = db.insert_observation(
                    origin=offer.origin,
                    destination=offer.destination,
                    depart_date=offer.depart_date,
                    return_date=offer.return_date,
                    carrier=offer.carrier,
                    price_eur=offer.price_eur,
                    deep_link=offer.deep_link,
                    raw_offer=offer.raw_offer,
                    source="amadeus",
                    depart_time=offer.depart_time,
                    return_time=offer.return_time,
                    duration_min=offer.duration_min,
                )
                inserted.append(obs_id)
        logger.info("Collecte terminée : %d offres insérées.", len(inserted))
    finally:
        if own_client:
            client.close()
    return inserted
