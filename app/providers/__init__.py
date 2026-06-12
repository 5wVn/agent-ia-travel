"""Flight-data provider abstraction.

The collector talks to a single :class:`FlightProvider` chosen at boot via the
``FLIGHT_PROVIDER`` env (``travelpayouts`` by default, ``amadeus`` accepted).
Each provider knows how to:

  - ``search`` a route/date pair and return normalized offers ready for the db,
  - ``verify_price`` re-checks the live/freshest price for the sniper
    (anti-stale-price step 4bis),
  - expose its ``name`` (the value stored in the ``source`` column),
  - produce a ``freshness_note`` describing the age of a stored price (used in
    critical alerts when the data is cached rather than real-time).

This keeps the rest of the pipeline (scoring, sniper, bot, scheduler) provider
agnostic: switching the data source is one class + one env var (see PLAN.md
risk "Disparition d'une source").
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class NormalizedOffer:
    """A flattened, cheapest-per-itinerary offer ready for insertion.

    Shared by every provider so the db layer and scoring never need to know
    which source produced an observation.
    """

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
    # Number of stops per itinerary (None = unknown). Displayed only.
    transfers: Optional[int] = None
    return_transfers: Optional[int] = None


@dataclass
class VerifiedPrice:
    """Result of a sniper price re-verification.

    ``price_eur`` is the freshest/live confirmed total. ``deep_link`` and
    ``raw_offer`` are refreshed when the provider returns them, so the bot can
    offer an up-to-date booking link on a triggered snipe.
    """

    price_eur: float
    deep_link: Optional[str] = None
    raw_offer: Optional[dict[str, Any]] = None
    # Cache-age text for cached providers (Travelpayouts); None for real-time.
    freshness_note: Optional[str] = None


@dataclass
class SearchQuery:
    """One search: a route plus a departure/return date pair.

    ``route`` is the :class:`app.config.Route` being searched; kept loosely
    typed here to avoid a circular import with the config module.
    """

    route: Any
    depart_date: str
    return_date: Optional[str]


class FlightProvider(ABC):
    """Interface every flight-data source must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Identifier stored in the ``source`` column (e.g. 'travelpayouts')."""

    @abstractmethod
    def search(self, query: SearchQuery) -> list[NormalizedOffer]:
        """Return normalized offers for one route/date pair.

        Implementations handle their own HTTP + retry/backoff and must not
        raise on a recoverable error in a way the caller can't survive — the
        collector wraps each call but providers should keep crashes rare.
        """

    @abstractmethod
    def verify_price(self, observation: Any) -> Optional[VerifiedPrice]:
        """Re-check the freshest/live price for a stored observation row.

        Returns ``None`` when verification could not be performed (network or
        parse error); the sniper then falls back to the observed best price.
        ``observation`` is a ``sqlite3.Row``-like mapping of a
        ``price_observations`` row (has ``raw_offer``, ``origin``, ...).
        """

    def freshness_note(self, observation: Any) -> Optional[str]:
        """Human text on the age of a stored price, or None for real-time data.

        Used in critical alerts to warn that a cached price may have moved
        (e.g. "prix observé il y a ~2 h, vérifie au clic"). Default: None.
        """
        return None

    def close(self) -> None:
        """Release any held resources (HTTP client). Default: no-op."""


def build_provider(config: Any) -> FlightProvider:
    """Instantiate the provider selected by ``config.flight_provider``.

    Imports are local so importing this package never drags in httpx clients
    for the provider you are not using.
    """
    name = (getattr(config, "flight_provider", "") or "").strip().lower()
    if name == "amadeus":
        from .amadeus import AmadeusProvider

        return AmadeusProvider(config)
    if name in ("travelpayouts", "aviasales", ""):
        from .travelpayouts import TravelpayoutsProvider

        return TravelpayoutsProvider(config)
    raise RuntimeError(
        f"FLIGHT_PROVIDER inconnu : '{name}'. "
        "Valeurs acceptées : 'travelpayouts' (défaut) ou 'amadeus'."
    )
