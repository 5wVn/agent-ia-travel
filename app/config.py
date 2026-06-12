"""Configuration loaded from environment variables with sensible defaults.

All user-facing strings elsewhere are in French; this module holds tunable
parameters (routes, windows, thresholds, scoring weights, LLM model). No secret
has a default value: secrets must come from the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Route:
    """A directional flight route (origin -> destination IATA codes)."""

    origin: str
    destination: str

    def label(self) -> str:
        return f"{self.origin}->{self.destination}"


# Toulouse <-> Paris (ORY and CDG), round trips.
DEFAULT_ROUTES: list[Route] = [
    Route("TLS", "ORY"),
    Route("TLS", "CDG"),
]


@dataclass
class Config:
    """Runtime configuration. Built once at startup via :func:`load_config`."""

    # Secrets (no defaults — must be provided via env).
    amadeus_client_id: str
    amadeus_client_secret: str
    anthropic_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str

    # Data.
    db_path: str = "/data/prices.db"

    # Routes and search window.
    routes: list[Route] = field(default_factory=lambda: list(DEFAULT_ROUTES))
    weekend_count: int = 8  # number of upcoming Friday->Sunday weekends to track

    # Collection cadence (hours) and quota management.
    collect_interval_hours: int = 4
    amadeus_monthly_quota: int = 2000
    quota_safety_ratio: float = 0.80  # reduce frequency above 80% of monthly quota

    # Detection thresholds.
    absolute_price_threshold_eur: float = 60.0
    score_alert_threshold: float = 80.0

    # Scoring weights (must sum to 1.0).
    weight_price: float = 0.45
    weight_schedule: float = 0.25
    weight_comfort: float = 0.20
    weight_trend: float = 0.10

    # Baseline window.
    baseline_window_days: int = 30

    # Comfort scoring: preferred / avoided carrier IATA codes.
    preferred_carriers: list[str] = field(default_factory=list)
    avoided_carriers: list[str] = field(default_factory=list)
    max_reasonable_duration_min: int = 90  # TLS-Paris direct is ~75 min

    # LLM.
    llm_model: str = "claude-opus-4-8"
    llm_max_tokens: int = 1024

    # Digest.
    digest_hour: int = 8
    timezone: str = "Europe/Paris"

    def deal_price_bucket_eur(self) -> int:
        """Price bucket width (euros) used in deal_key dedup."""
        return 10


def load_config() -> Config:
    """Build a :class:`Config` from the environment.

    Raises:
        RuntimeError: if a required secret is missing.
    """
    required = {
        "AMADEUS_CLIENT_ID": os.environ.get("AMADEUS_CLIENT_ID"),
        "AMADEUS_CLIENT_SECRET": os.environ.get("AMADEUS_CLIENT_SECRET"),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Variables d'environnement manquantes : " + ", ".join(missing)
        )

    routes = _parse_routes(os.environ.get("ROUTES"))

    return Config(
        amadeus_client_id=required["AMADEUS_CLIENT_ID"],  # type: ignore[arg-type]
        amadeus_client_secret=required["AMADEUS_CLIENT_SECRET"],  # type: ignore[arg-type]
        anthropic_api_key=required["ANTHROPIC_API_KEY"],  # type: ignore[arg-type]
        telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],  # type: ignore[arg-type]
        telegram_chat_id=required["TELEGRAM_CHAT_ID"],  # type: ignore[arg-type]
        db_path=os.environ.get("DB_PATH", "/data/prices.db"),
        routes=routes,
        weekend_count=_get_int("WEEKEND_COUNT", 8),
        collect_interval_hours=_get_int("COLLECT_INTERVAL_HOURS", 4),
        amadeus_monthly_quota=_get_int("AMADEUS_MONTHLY_QUOTA", 2000),
        quota_safety_ratio=_get_float("QUOTA_SAFETY_RATIO", 0.80),
        absolute_price_threshold_eur=_get_float("ABSOLUTE_PRICE_THRESHOLD_EUR", 60.0),
        score_alert_threshold=_get_float("SCORE_ALERT_THRESHOLD", 80.0),
        weight_price=_get_float("WEIGHT_PRICE", 0.45),
        weight_schedule=_get_float("WEIGHT_SCHEDULE", 0.25),
        weight_comfort=_get_float("WEIGHT_COMFORT", 0.20),
        weight_trend=_get_float("WEIGHT_TREND", 0.10),
        baseline_window_days=_get_int("BASELINE_WINDOW_DAYS", 30),
        preferred_carriers=_get_list("PREFERRED_CARRIERS", []),
        avoided_carriers=_get_list("AVOIDED_CARRIERS", []),
        max_reasonable_duration_min=_get_int("MAX_REASONABLE_DURATION_MIN", 90),
        llm_model=os.environ.get("LLM_MODEL", "claude-opus-4-8"),
        llm_max_tokens=_get_int("LLM_MAX_TOKENS", 1024),
        digest_hour=_get_int("DIGEST_HOUR", 8),
        timezone=os.environ.get("TIMEZONE", "Europe/Paris"),
    )


def _parse_routes(raw: str | None) -> list[Route]:
    """Parse a ROUTES env string like 'TLS-ORY,TLS-CDG' into Route objects."""
    if not raw or not raw.strip():
        return list(DEFAULT_ROUTES)
    routes: list[Route] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            origin, _, destination = token.partition("-")
            routes.append(Route(origin.strip().upper(), destination.strip().upper()))
    return routes or list(DEFAULT_ROUTES)


def upcoming_weekends(count: int, today: date | None = None) -> list[tuple[str, str]]:
    """Return the next ``count`` (Friday, Sunday) date pairs as ISO strings.

    The first weekend is the next Friday strictly in the future (or today if
    today is a Friday). Each pair is (depart_date, return_date).
    """
    if today is None:
        today = date.today()
    # weekday(): Monday=0 ... Friday=4, Sunday=6.
    days_until_friday = (4 - today.weekday()) % 7
    first_friday = today + timedelta(days=days_until_friday)
    pairs: list[tuple[str, str]] = []
    for i in range(count):
        friday = first_friday + timedelta(weeks=i)
        sunday = friday + timedelta(days=2)
        pairs.append((friday.isoformat(), sunday.isoformat()))
    return pairs
