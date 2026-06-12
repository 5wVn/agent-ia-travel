"""Composite 0-100 scoring and deterministic deal detection.

Implements PLAN.md section 4, step 2. The score is a weighted blend of four
sub-scores (price, schedule, comfort, trend). Detection fires when the score
clears the configured threshold OR the absolute price is below the floor, with
deduplication via ``alerts_sent.deal_key``. No machine learning — the formula
is transparent and recomputable from ``price_observations``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import Config
from .db import Database, make_deal_key

logger = logging.getLogger(__name__)


@dataclass
class ScoreComponents:
    """The four sub-scores plus the weighted composite, all 0-100."""

    prix: float
    horaire: float
    confort: float
    tendance: float
    composite: float

    def as_dict(self) -> dict[str, float]:
        return {
            "prix": round(self.prix, 2),
            "horaire": round(self.horaire, 2),
            "confort": round(self.confort, 2),
            "tendance": round(self.tendance, 2),
            "composite": round(self.composite, 2),
        }


@dataclass
class Deal:
    """A detected deal: the observation plus its score and dedup key."""

    observation_id: int
    score: float
    components: ScoreComponents
    deal_key: str
    reason: str  # 'score' | 'absolute_price'


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _minutes_between_hhmm(a: str, b: str) -> int:
    """Absolute minute gap between two 'HH:MM' strings."""
    ah, am = int(a[:2]), int(a[3:5])
    bh, bm = int(b[:2]), int(b[3:5])
    return abs((ah * 60 + am) - (bh * 60 + bm))


def score_price(
    price: float, p10: Optional[float], median: Optional[float]
) -> float:
    """Position vs history: 100 at/below p10, 0 at/above median, linear between.

    With no history (p10 or median is None), returns a neutral 50 — we cannot
    tell whether the price is good without a baseline.
    """
    if p10 is None or median is None:
        return 50.0
    if median <= p10:
        # Degenerate window (all prices equal-ish): cheap if at/under it.
        return 100.0 if price <= median else 0.0
    if price <= p10:
        return 100.0
    if price >= median:
        return 0.0
    # Linear interpolation between p10 (100) and median (0).
    return _clamp(100.0 * (median - price) / (median - p10))


def score_schedule(
    depart_time: Optional[str],
    return_time: Optional[str],
    tracked: Optional[dict[str, Optional[str]]],
) -> float:
    """100 inside the tracked window, decaying per hour of deviation.

    ``tracked`` carries the optional ``depart_time_from``/``..._to`` and
    ``return_time_from``/``..._to`` bounds. With no tracked window (or no time
    bounds), returns 100 — there is no preference to violate.
    """
    if not tracked:
        return 100.0

    def window_score(
        observed: Optional[str], lo: Optional[str], hi: Optional[str]
    ) -> float:
        if lo is None or hi is None:
            return 100.0  # "peu importe" — no preference
        if observed is None:
            return 50.0  # unknown time, mild penalty
        if lo <= observed <= hi:
            return 100.0
        # Distance to the nearest bound, 25 points lost per hour.
        nearest = lo if observed < lo else hi
        gap_min = _minutes_between_hhmm(observed, nearest)
        return _clamp(100.0 - (gap_min / 60.0) * 25.0)

    out = window_score(
        depart_time, tracked.get("depart_time_from"), tracked.get("depart_time_to")
    )
    ret = window_score(
        return_time, tracked.get("return_time_from"), tracked.get("return_time_to")
    )
    return (out + ret) / 2.0


def score_comfort(
    config: Config,
    carrier: str,
    duration_min: Optional[int],
) -> float:
    """Comfort from duration and carrier preference.

    Starts at 100. Reasonable duration keeps it high; long durations cost
    points. Preferred carriers earn a bonus, avoided carriers a heavy penalty.
    """
    score = 100.0
    if duration_min is not None and duration_min > config.max_reasonable_duration_min:
        excess = duration_min - config.max_reasonable_duration_min
        score -= (excess / 30.0) * 10.0  # -10 points per extra 30 minutes

    carrier_u = (carrier or "").upper()
    if carrier_u in {c.upper() for c in config.avoided_carriers}:
        score -= 50.0
    elif carrier_u in {c.upper() for c in config.preferred_carriers}:
        score += 10.0

    return _clamp(score)


def score_trend(recent_prices: list[float]) -> float:
    """Trend from the last few observations (most recent first).

    Falling prices score high (a deal getting better); a sharp rebound scores
    high too (a "last chance" signal). A flat trend is neutral (50). With fewer
    than two points there is no trend, so 50.
    """
    if len(recent_prices) < 2:
        return 50.0
    newest = recent_prices[0]
    previous = recent_prices[1]
    if previous == 0:
        return 50.0
    change = (newest - previous) / previous  # negative = price dropped
    if change <= -0.05:
        return 100.0  # dropping meaningfully — good moment
    if change >= 0.10:
        return 90.0  # sharp rebound — "last chance" signal
    if change >= 0.0:
        return _clamp(50.0 - change * 200.0)  # rising slowly — slightly worse
    # Slight drop between 0 and 5%.
    return _clamp(50.0 + (-change) * 1000.0)


def compute_score(
    config: Config,
    db: Database,
    observation: dict,
    now: Optional[datetime] = None,
) -> ScoreComponents:
    """Compute the composite score for one observation row.

    ``observation`` is expected to expose the price_observations columns
    (sqlite3.Row supports mapping access, as does a plain dict).
    """
    origin = observation["origin"]
    destination = observation["destination"]
    depart_date = observation["depart_date"]
    return_date = observation["return_date"]
    price = float(observation["price_eur"])

    p10 = db.baseline_percentile(
        origin, destination, depart_date, 10.0, config.baseline_window_days, now
    )
    median = db.baseline_median(
        origin, destination, depart_date, config.baseline_window_days, now
    )
    s_price = score_price(price, p10, median)

    tracked_row = db.matching_tracked_date(depart_date, return_date)
    tracked = dict(tracked_row) if tracked_row is not None else None
    s_schedule = score_schedule(
        observation["depart_time"], observation["return_time"], tracked
    )

    s_comfort = score_comfort(config, observation["carrier"], observation["duration_min"])

    recent = db.recent_prices(origin, destination, depart_date, limit=3)
    s_trend = score_trend(recent)

    composite = (
        config.weight_price * s_price
        + config.weight_schedule * s_schedule
        + config.weight_comfort * s_comfort
        + config.weight_trend * s_trend
    )
    return ScoreComponents(
        prix=s_price,
        horaire=s_schedule,
        confort=s_comfort,
        tendance=s_trend,
        composite=composite,
    )


def is_deal(config: Config, price: float, score: float) -> Optional[str]:
    """Return the trigger reason ('score'|'absolute_price') or None."""
    if price < config.absolute_price_threshold_eur:
        return "absolute_price"
    if score >= config.score_alert_threshold:
        return "score"
    return None


def score_and_detect(
    config: Config,
    db: Database,
    observation_ids: list[int],
    now: Optional[datetime] = None,
) -> list[Deal]:
    """Score the given observations, persist scores, and return new deals.

    Every observation is scored and stored (recomputable history). Deals are
    only returned when they trigger and their ``deal_key`` is newly registered
    in ``alerts_sent`` — so the same deal is not surfaced twice.
    """
    deals: list[Deal] = []
    for obs_id in observation_ids:
        row = db.get_observation(obs_id)
        if row is None:
            continue
        try:
            components = compute_score(config, db, row, now)
        except Exception as exc:  # noqa: BLE001 — never crash the tick
            logger.error("Scoring échoué pour l'observation %d : %s", obs_id, exc)
            continue

        db.upsert_score(obs_id, components.composite, components.as_dict())

        reason = is_deal(config, float(row["price_eur"]), components.composite)
        if reason is None:
            continue

        deal_key = make_deal_key(
            row["origin"],
            row["destination"],
            row["depart_date"],
            row["return_date"],
            row["carrier"],
            float(row["price_eur"]),
            config.deal_price_bucket_eur(),
        )
        if not db.try_register_alert(obs_id, deal_key):
            continue  # already alerted on this deal_key

        deals.append(
            Deal(
                observation_id=obs_id,
                score=components.composite,
                components=components,
                deal_key=deal_key,
                reason=reason,
            )
        )
    logger.info(
        "Scoring : %d observations, %d nouveaux deals.",
        len(observation_ids),
        len(deals),
    )
    return deals
