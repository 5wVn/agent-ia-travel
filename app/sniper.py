"""Price sniper — armed price thresholds (PLAN.md step 4bis).

A snipe is a price threshold armed on a tracked date. A dedicated APScheduler
job runs every ``snipe_interval_minutes`` and only processes dates whose last
observed best price is within ``snipe_proximity_ratio`` of the threshold — so
the boosted watch spends quota only where it matters, and sniper collections
take priority over the standard weekend scan when the quota guard is close to
the ceiling.

When the best observed price drops to/below the threshold, the offer is
re-checked live via the Amadeus Flight Offers Price endpoint (anti-stale-price)
using the raw offer kept in ``raw_offer``. If confirmed, the snipe is marked
``triggered`` and a critical alert is raised (handled by the bot). If the live
price has climbed back above the threshold, the snipe simply stays armed.

This module is deliberately free of Telegram/asyncio concerns: it returns plain
result objects so it can be unit-tested with a mocked collector/client. The bot
turns the results into messages and re-ping jobs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from .config import Config
from .db import Database

logger = logging.getLogger(__name__)

PRICING_PATH = "/v1/shopping/flight-offers/pricing"


@dataclass
class SnipeResult:
    """Outcome of evaluating one armed snipe during a boosted watch tick."""

    tracked_id: int
    depart_date: str
    return_date: Optional[str]
    threshold_eur: float
    best_price_eur: Optional[float]
    triggered: bool
    confirmed_price_eur: Optional[float] = None
    observation_id: Optional[int] = None
    deep_link: Optional[str] = None
    # 'triggered' | 'rebounded' | 'watching' | 'no_data'
    status: str = "watching"
    # Cache-age note for cached providers (e.g. Travelpayouts); None = real-time.
    freshness_note: Optional[str] = None


def snipe_candidates(config: Config, db: Database) -> list[Any]:
    """Armed snipes whose best observed price is within proximity of threshold.

    These are the only dates the boosted watch should collect — the proximity
    gate (best price < threshold * ratio) keeps quota spend focused. A snipe
    with no price history yet is always a candidate (we have nothing to gate
    on, so we want to start observing it).
    """
    candidates: list[Any] = []
    for row in db.armed_snipes():
        threshold = float(row["snipe_price_eur"])
        best = db.best_current_price(row["depart_date"], row["return_date"])
        if best is None:
            candidates.append(row)
            continue
        if float(best["price_eur"]) < threshold * config.snipe_proximity_ratio:
            candidates.append(row)
    return candidates


def needs_boosted_watch(config: Config, db: Database) -> bool:
    """True if at least one armed snipe is close enough to warrant boosting."""
    return len(snipe_candidates(config, db)) > 0


def reverify_price(
    config: Config,
    client: Any,
    raw_offer: dict[str, Any],
) -> Optional[float]:
    """Re-check a raw offer's live price (legacy ``price_offer`` path).

    Returns the confirmed total price in EUR, or None if the verification
    could not be performed (network/parse error) — the caller then treats the
    snipe as unconfirmed and keeps watching. ``client`` must expose
    ``price_offer(raw_offer) -> payload`` (see :class:`AmadeusClient`). New code
    goes through ``provider.verify_price`` instead; this helper stays for the
    Amadeus pricing endpoint and its unit tests.
    """
    try:
        payload = client.price_offer(raw_offer)
    except Exception as exc:  # noqa: BLE001 — never crash a tick
        logger.warning("Re-vérification de prix échouée : %s", exc)
        return None
    return extract_priced_total(payload)


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


def _verify(config: Config, provider: Any, best: Any) -> tuple[
    Optional[float], Optional[str], Optional[str]
]:
    """Re-verify the freshest price for an observation row.

    Prefers the provider interface ``verify_price(observation) -> VerifiedPrice``
    (Travelpayouts re-fetch with a freshness note + fresh deep link); falls back
    to the legacy ``price_offer`` re-pricing (Amadeus) used by existing tests.

    Returns ``(price, freshness_note, deep_link)``; ``price`` is None when the
    check could not be performed.
    """
    if hasattr(provider, "verify_price"):
        verified = provider.verify_price(best)
        if verified is None:
            return None, None, None
        return verified.price_eur, getattr(verified, "freshness_note", None), getattr(
            verified, "deep_link", None
        )
    # Legacy path: re-price the stored raw offer via price_offer.
    raw_offer = _load_raw_offer(best["raw_offer"])
    if raw_offer is None:
        return None, None, None
    return reverify_price(config, provider, raw_offer), None, None


def evaluate_snipe(
    config: Config,
    db: Database,
    tracked_row: Any,
    provider: Any,
) -> SnipeResult:
    """Evaluate one armed snipe: detect trigger and re-verify the freshest price.

    Flow per PLAN.md step 4bis:
      - best observed price > threshold  -> still watching
      - best observed price <= threshold -> re-verify (live price for Amadeus,
        freshest cached price for Travelpayouts):
          * confirmed <= threshold -> triggered (state set by caller path)
          * confirmed  > threshold -> rebounded ("raté, je continue à viser")

    ``provider`` is a :class:`app.providers.FlightProvider` (uses
    ``verify_price``); a legacy ``price_offer`` client is still accepted.
    """
    tracked_id = int(tracked_row["id"])
    threshold = float(tracked_row["snipe_price_eur"])
    depart = tracked_row["depart_date"]
    ret = tracked_row["return_date"]

    best = db.best_current_price(depart, ret)
    if best is None:
        return SnipeResult(
            tracked_id=tracked_id,
            depart_date=depart,
            return_date=ret,
            threshold_eur=threshold,
            best_price_eur=None,
            triggered=False,
            status="no_data",
        )

    best_price = float(best["price_eur"])
    if best_price > threshold:
        return SnipeResult(
            tracked_id=tracked_id,
            depart_date=depart,
            return_date=ret,
            threshold_eur=threshold,
            best_price_eur=best_price,
            triggered=False,
            status="watching",
        )

    # Candidate trigger: re-verify the freshest price before alerting.
    confirmed, freshness, fresh_link = _verify(config, provider, best)
    # If we cannot re-verify, fall back to the observed best price so a genuine
    # drop is not silently dropped on a transient pricing error.
    effective = confirmed if confirmed is not None else best_price
    deep_link = fresh_link or best["deep_link"]

    if effective <= threshold:
        return SnipeResult(
            tracked_id=tracked_id,
            depart_date=depart,
            return_date=ret,
            threshold_eur=threshold,
            best_price_eur=best_price,
            triggered=True,
            confirmed_price_eur=effective,
            observation_id=int(best["id"]),
            deep_link=deep_link,
            status="triggered",
            freshness_note=freshness,
        )

    # Live/fresh price climbed back above the threshold before we could alert.
    return SnipeResult(
        tracked_id=tracked_id,
        depart_date=depart,
        return_date=ret,
        threshold_eur=threshold,
        best_price_eur=best_price,
        triggered=False,
        confirmed_price_eur=effective,
        observation_id=int(best["id"]),
        status="rebounded",
        freshness_note=freshness,
    )


def _load_raw_offer(raw: Any) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None
