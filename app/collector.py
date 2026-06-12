"""Collection orchestrator — picks a flight-data provider and stores offers.

The provider abstraction lives in :mod:`app.providers`. This module chooses one
via ``FLIGHT_PROVIDER`` (``travelpayouts`` by default, ``amadeus`` accepted —
the latter only until the 2026-07-17 Self-Service shutdown), builds the search
queries (upcoming weekends + tracked dates, with a narrow sniper variant), keeps
a generic per-provider API-call quota counter in the db, and runs one collection
tick robustly: a failure on one query is logged and skipped, never crashing the
process.

Backwards-compatible surface: the Amadeus client, the offer dataclasses, the
normalizer and the ISO-duration helper are re-exported here so existing imports
(``from app.collector import AmadeusClient, normalize_offers, SearchQuery, ...``)
keep working after the move into :mod:`app.providers`.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .config import Config, upcoming_weekends
from .db import Database
from .providers import (
    FlightProvider,
    NormalizedOffer,
    SearchQuery,
    VerifiedPrice,
    build_provider,
)

# Re-exports kept for backwards compatibility with existing imports/tests.
from .providers.amadeus import (  # noqa: F401
    AMADEUS_BASE_URL,
    OFFERS_PATH,
    PRICING_PATH,
    TOKEN_PATH,
    AmadeusClient,
    _iso_duration_to_minutes,  # noqa: F401
    _segment_time,  # noqa: F401
    normalize_offers,  # noqa: F401  (Amadeus normalizer; the historical one)
)

logger = logging.getLogger(__name__)

__all__ = [
    "AMADEUS_BASE_URL",
    "AmadeusClient",
    "FlightProvider",
    "NormalizedOffer",
    "OFFERS_PATH",
    "PRICING_PATH",
    "SearchQuery",
    "TOKEN_PATH",
    "VerifiedPrice",
    "build_provider",
    "build_queries",
    "build_snipe_queries",
    "normalize_offers",
    "quota_allows",
    "run_collection",
    "standard_scan_should_defer",
]


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
    """Return False once we hit the safety ratio of the configured quota."""
    used = db.api_calls_this_month()
    ceiling = int(config.provider_monthly_quota() * config.quota_safety_ratio)
    if used >= ceiling:
        logger.warning(
            "Quota %s atteint (%d/%d, plafond %d) — collecte suspendue.",
            config.flight_provider,
            used,
            config.provider_monthly_quota(),
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
    ceiling = int(config.provider_monthly_quota() * config.quota_safety_ratio)
    # Budget the sniper needs before the next standard scan: one boosted-watch
    # pass per candidate, across every route.
    reserve = snipe_candidate_count * max(1, len(config.routes))
    return used + reserve >= ceiling


def run_collection(
    config: Config,
    db: Database,
    provider: Optional[FlightProvider] = None,
    queries: Optional[list[SearchQuery]] = None,
) -> list[int]:
    """Run one collection tick. Returns inserted observation ids.

    By default the queries cover upcoming weekends + tracked dates. The boosted
    sniper watch passes an explicit, narrow ``queries`` list so it only spends
    quota on the dates that matter.

    The provider is chosen by ``FLIGHT_PROVIDER`` unless one is passed in. Each
    API call is counted in the db (generic quota, keyed by the provider name +
    endpoint) and the quota guard stops the tick cleanly at the safety ceiling.
    Robust by design: a failure on one query is logged and skipped.
    """
    own_provider = provider is None
    if provider is None:
        provider = build_provider(config)

    inserted: list[int] = []
    try:
        if queries is None:
            queries = build_queries(config, db)
        logger.info(
            "Collecte (%s) : %d recherches planifiées.",
            provider.name,
            len(queries),
        )
        for query in queries:
            if not quota_allows(config, db):
                break
            try:
                db.record_api_call(f"{provider.name}:search")
                offers = provider.search(query)
            except Exception as exc:  # noqa: BLE001 — never crash a tick
                logger.error(
                    "Recherche échouée %s %s->%s : %s",
                    query.depart_date,
                    query.route.origin,
                    query.route.destination,
                    exc,
                )
                continue
            for offer in offers:
                obs_id = db.insert_observation(
                    origin=offer.origin,
                    destination=offer.destination,
                    depart_date=offer.depart_date,
                    return_date=offer.return_date,
                    carrier=offer.carrier,
                    price_eur=offer.price_eur,
                    deep_link=offer.deep_link,
                    raw_offer=offer.raw_offer,
                    source=provider.name,
                    depart_time=offer.depart_time,
                    return_time=offer.return_time,
                    duration_min=offer.duration_min,
                )
                inserted.append(obs_id)
        logger.info("Collecte terminée : %d offres insérées.", len(inserted))
    finally:
        if own_provider:
            provider.close()
    return inserted
