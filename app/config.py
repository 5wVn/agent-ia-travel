"""Configuration loaded from environment variables with sensible defaults.

All user-facing strings elsewhere are in French; this module holds tunable
parameters (routes, windows, thresholds, scoring weights, LLM model). No secret
has a default value: secrets must come from the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional


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

    # Secrets.
    telegram_bot_token: str
    telegram_chat_id: str
    # Amadeus keys are now optional: required only when FLIGHT_PROVIDER=amadeus
    # (validated in load_config). They default to "" so tests/configs that use
    # Travelpayouts need not provide them.
    amadeus_client_id: str = ""
    amadeus_client_secret: str = ""
    # Optional: when empty/None, the LLM is disabled and templates are used.
    anthropic_api_key: Optional[str] = None

    # Flight-data provider selection. 'travelpayouts' (default) or 'amadeus'.
    # ⚠️ Amadeus Self-Service is decommissioned on 2026-07-17.
    flight_provider: str = "travelpayouts"
    travelpayouts_token: Optional[str] = None
    travelpayouts_marker: Optional[str] = None

    # Data.
    db_path: str = "/data/prices.db"

    # Routes and search window.
    routes: list[Route] = field(default_factory=lambda: list(DEFAULT_ROUTES))
    weekend_count: int = 8  # number of upcoming Friday->Sunday weekends to track

    # Collection cadence (hours) and quota management.
    collect_interval_hours: int = 4
    amadeus_monthly_quota: int = 2000
    # Travelpayouts/Aviasales Data API: generous free tier; the guard is mostly
    # a courtesy rate-limit. Default chosen well above the weekend-scan needs.
    travelpayouts_monthly_quota: int = 10000
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

    # Price sniper (PLAN.md step 4bis).
    snipe_interval_minutes: int = 15           # boosted watch cadence
    snipe_proximity_ratio: float = 1.15        # only watch dates < threshold * this
    snipe_reping_interval_minutes: int = 5     # critical alert re-ping cadence
    snipe_reping_max: int = 6                  # max number of re-pings
    snipe_amount_min_eur: int = 30             # threshold grid lower bound
    snipe_amount_max_eur: int = 120            # threshold grid upper bound
    snipe_amount_step_eur: int = 5             # threshold grid step
    snipe_grid_page_size: int = 18             # amounts shown per page

    def provider_monthly_quota(self) -> int:
        """Monthly API-call quota for the active provider (generic guard)."""
        if (self.flight_provider or "").strip().lower() == "amadeus":
            return self.amadeus_monthly_quota
        return self.travelpayouts_monthly_quota

    def deal_price_bucket_eur(self) -> int:
        """Price bucket width (euros) used in deal_key dedup."""
        return 10

    def llm_enabled(self) -> bool:
        """True only when an Anthropic API key is configured (non-empty)."""
        return bool(self.anthropic_api_key and self.anthropic_api_key.strip())


def load_config() -> Config:
    """Build a :class:`Config` from the environment.

    Raises:
        RuntimeError: if a required secret is missing.
    """
    # ANTHROPIC_API_KEY is intentionally optional: without it the agent runs in
    # "mode sans LLM" (template messages). Telegram secrets are always required.
    required = {
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Variables d'environnement manquantes : " + ", ".join(missing)
        )

    provider = (os.environ.get("FLIGHT_PROVIDER") or "travelpayouts").strip().lower()
    if provider not in ("travelpayouts", "amadeus"):
        raise RuntimeError(
            f"FLIGHT_PROVIDER invalide : '{provider}'. "
            "Valeurs acceptées : 'travelpayouts' (défaut) ou 'amadeus'."
        )

    amadeus_id = os.environ.get("AMADEUS_CLIENT_ID") or ""
    amadeus_secret = os.environ.get("AMADEUS_CLIENT_SECRET") or ""
    tp_token = os.environ.get("TRAVELPAYOUTS_TOKEN") or None
    tp_marker = os.environ.get("TRAVELPAYOUTS_MARKER") or None

    # Provider-specific credential checks: fail fast with a clear message.
    if provider == "amadeus" and not (amadeus_id and amadeus_secret):
        raise RuntimeError(
            "FLIGHT_PROVIDER=amadeus exige AMADEUS_CLIENT_ID et "
            "AMADEUS_CLIENT_SECRET (le portail Self-Service ferme le 17/07/2026)."
        )
    if provider == "travelpayouts" and not tp_token:
        raise RuntimeError(
            "FLIGHT_PROVIDER=travelpayouts exige TRAVELPAYOUTS_TOKEN "
            "(inscription gratuite sur travelpayouts.com, token dans l'espace "
            "affilié)."
        )

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key is not None and not anthropic_key.strip():
        anthropic_key = None

    routes = _parse_routes(os.environ.get("ROUTES"))

    return Config(
        amadeus_client_id=amadeus_id,
        amadeus_client_secret=amadeus_secret,
        anthropic_api_key=anthropic_key,
        flight_provider=provider,
        travelpayouts_token=tp_token,
        travelpayouts_marker=tp_marker,
        telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],  # type: ignore[arg-type]
        telegram_chat_id=required["TELEGRAM_CHAT_ID"],  # type: ignore[arg-type]
        db_path=os.environ.get("DB_PATH", "/data/prices.db"),
        routes=routes,
        weekend_count=_get_int("WEEKEND_COUNT", 8),
        collect_interval_hours=_get_int("COLLECT_INTERVAL_HOURS", 4),
        amadeus_monthly_quota=_get_int("AMADEUS_MONTHLY_QUOTA", 2000),
        travelpayouts_monthly_quota=_get_int("TRAVELPAYOUTS_MONTHLY_QUOTA", 10000),
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
        snipe_interval_minutes=_get_int("SNIPE_INTERVAL_MINUTES", 15),
        snipe_proximity_ratio=_get_float("SNIPE_PROXIMITY_RATIO", 1.15),
        snipe_reping_interval_minutes=_get_int("SNIPE_REPING_INTERVAL_MINUTES", 5),
        snipe_reping_max=_get_int("SNIPE_REPING_MAX", 6),
        snipe_amount_min_eur=_get_int("SNIPE_AMOUNT_MIN_EUR", 30),
        snipe_amount_max_eur=_get_int("SNIPE_AMOUNT_MAX_EUR", 120),
        snipe_amount_step_eur=_get_int("SNIPE_AMOUNT_STEP_EUR", 5),
        snipe_grid_page_size=_get_int("SNIPE_GRID_PAGE_SIZE", 18),
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
