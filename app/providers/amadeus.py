"""Amadeus Self-Service provider (real-time, no deep links).

Moved verbatim from the former ``app/collector.py`` Amadeus client — same
OAuth2 ``client_credentials`` token flow, same ``v2/shopping/flight-offers``
search, same Flight Offers Price re-verification, same httpx + exponential
backoff (3 attempts). The only change is packaging: it now implements the
:class:`app.providers.FlightProvider` interface.

⚠️ Amadeus decommissions its Self-Service portal on 2026-07-17 (keys disabled).
This provider stays functional until then; ``travelpayouts`` is the default.

Freshness: Amadeus is real-time, so :meth:`AmadeusProvider.freshness_note`
returns ``None`` (no "price observed N hours ago" caveat to show).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from ..config import Config
from . import FlightProvider, NormalizedOffer, SearchQuery, VerifiedPrice

logger = logging.getLogger(__name__)

AMADEUS_BASE_URL = "https://test.api.amadeus.com"
TOKEN_PATH = "/v1/security/oauth2/token"
OFFERS_PATH = "/v2/shopping/flight-offers"
PRICING_PATH = "/v1/shopping/flight-offers/pricing"

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0


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


def extract_priced_total(payload: dict[str, Any]) -> Optional[float]:
    """Pull the grand total (EUR) out of a Flight Offers Price response."""
    try:
        data = payload.get("data") or {}
        offers = data.get("flightOffers") or []
        if not offers:
            return None
        price = offers[0].get("price", {})
        total = price.get("grandTotal") or price.get("total")
        return float(total) if total is not None else None
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Réponse pricing illisible : %s", exc)
        return None


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


class AmadeusProvider(FlightProvider):
    """:class:`FlightProvider` backed by the Amadeus Self-Service API."""

    def __init__(self, config: Config, client: Optional[AmadeusClient] = None) -> None:
        self.config = config
        self._client = client or AmadeusClient(config)

    @property
    def name(self) -> str:
        return "amadeus"

    def search(self, query: SearchQuery) -> list[NormalizedOffer]:
        payload = self._client.search(query)
        return normalize_offers(payload, query)

    def verify_price(self, observation: Any) -> Optional[VerifiedPrice]:
        """Re-price the stored raw offer via Flight Offers Price.

        Returns ``None`` on network/parse error so the sniper keeps watching.
        Amadeus is real-time, so there is no freshness caveat.
        """
        raw_offer = _load_raw_offer(_row_get(observation, "raw_offer"))
        if raw_offer is None:
            return None
        try:
            payload = self._client.price_offer(raw_offer)
        except Exception as exc:  # noqa: BLE001 — never crash a tick
            logger.warning("Re-vérification Amadeus échouée : %s", exc)
            return None
        total = extract_priced_total(payload)
        if total is None:
            return None
        return VerifiedPrice(price_eur=total, deep_link=None, raw_offer=None)

    def freshness_note(self, observation: Any) -> Optional[str]:
        return None  # real-time data

    def close(self) -> None:
        self._client.close()

    # Exposed so the sniper can keep using ``price_offer`` directly in tests.
    def price_offer(self, raw_offer: dict[str, Any]) -> dict[str, Any]:
        return self._client.price_offer(raw_offer)


def _row_get(row: Any, key: str) -> Any:
    """Read ``key`` from a sqlite3.Row or a plain mapping, tolerating absence."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        try:
            return row.get(key)  # type: ignore[union-attr]
        except AttributeError:
            return None
