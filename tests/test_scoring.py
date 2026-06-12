"""Tests for the deterministic scoring and detection logic."""

from __future__ import annotations

from datetime import datetime, timezone

from app.scoring import (
    is_deal,
    score_and_detect,
    score_comfort,
    score_price,
    score_schedule,
    score_trend,
)


def test_score_price_no_history_is_neutral():
    assert score_price(100.0, None, None) == 50.0


def test_score_price_at_or_below_p10_is_100():
    assert score_price(40.0, 50.0, 90.0) == 100.0
    assert score_price(50.0, 50.0, 90.0) == 100.0


def test_score_price_at_or_above_median_is_0():
    assert score_price(90.0, 50.0, 90.0) == 0.0
    assert score_price(120.0, 50.0, 90.0) == 0.0


def test_score_price_linear_between():
    # Halfway between p10 (50) and median (90) is 70 -> score 50.
    assert score_price(70.0, 50.0, 90.0) == 50.0


def test_score_schedule_no_window_is_100():
    assert score_schedule("17:00", "19:00", None) == 100.0


def test_score_schedule_inside_window_is_100():
    tracked = {
        "depart_time_from": "17:00",
        "depart_time_to": "21:00",
        "return_time_from": "16:00",
        "return_time_to": "20:00",
    }
    assert score_schedule("18:00", "18:30", tracked) == 100.0


def test_score_schedule_outside_window_decays():
    tracked = {
        "depart_time_from": "17:00",
        "depart_time_to": "21:00",
        "return_time_from": None,
        "return_time_to": None,
    }
    # Departure two hours before the window -> -50 on that leg; return leg 100.
    # depart score = 100 - 2*25 = 50; return = 100; mean = 75.
    assert score_schedule("15:00", "18:00", tracked) == 75.0


def test_score_comfort_avoided_carrier_penalized(config):
    base = score_comfort(config, "AF", 75)
    avoided = score_comfort(config, "XX", 75)
    assert avoided < base


def test_score_comfort_long_duration_penalized(config):
    short = score_comfort(config, "AF", 75)
    long = score_comfort(config, "AF", 200)
    assert long < short


def test_score_trend_falling_is_high():
    # newest cheaper than previous -> good moment.
    assert score_trend([80.0, 100.0, 110.0]) == 100.0


def test_score_trend_too_few_points_neutral():
    assert score_trend([100.0]) == 50.0


def test_is_deal_absolute_price():
    from app.config import Config, Route

    cfg = Config(
        amadeus_client_id="x", amadeus_client_secret="x", anthropic_api_key="x",
        telegram_bot_token="x", telegram_chat_id="1",
    )
    assert is_deal(cfg, 55.0, 10.0) == "absolute_price"
    assert is_deal(cfg, 70.0, 85.0) == "score"
    assert is_deal(cfg, 70.0, 50.0) is None


def _insert(db, price, depart="2026-09-12", carrier="AF", observed_at=None, **kw):
    return db.insert_observation(
        origin="TLS", destination="ORY", depart_date=depart, return_date="2026-09-14",
        carrier=carrier, price_eur=price, deep_link=None, raw_offer=None,
        observed_at=observed_at, **kw,
    )


def test_score_and_detect_low_price_triggers(db, config):
    obs_id = _insert(db, 45.0, depart_time="17:30", return_time="19:10", duration_min=75)
    deals = score_and_detect(config, db, [obs_id])
    assert len(deals) == 1
    assert deals[0].reason == "absolute_price"
    # Score persisted.
    assert db.get_score(obs_id) is not None


def test_score_and_detect_dedup_by_deal_key(db, config):
    now = datetime.now(timezone.utc)
    o1 = _insert(db, 45.0, observed_at=now.isoformat())
    o2 = _insert(db, 47.0, observed_at=now.isoformat())  # same 10-EUR bucket
    deals1 = score_and_detect(config, db, [o1])
    deals2 = score_and_detect(config, db, [o2])
    assert len(deals1) == 1
    assert len(deals2) == 0  # same deal_key -> deduplicated


def test_score_and_detect_expensive_vs_baseline_not_a_deal(db, config):
    # Build a baseline of cheap prices, then a pricey observation: its price
    # score is 0 (>= median), so the composite stays below the alert threshold.
    now = datetime.now(timezone.utc)
    for p in [50.0, 55.0, 60.0, 65.0, 70.0]:
        _insert(db, p, observed_at=now.isoformat())
    obs_id = _insert(
        db, 120.0, observed_at=now.isoformat(),
        depart_time="08:00", return_time="20:00", duration_min=75,
    )
    deals = score_and_detect(config, db, [obs_id])
    assert len(deals) == 0
    score = db.get_score(obs_id)
    assert score is not None
    assert score["score"] < config.score_alert_threshold


def test_score_and_detect_cheap_vs_baseline_is_a_deal(db, config):
    # Baseline of expensive prices, then a cheap observation in a tracked window:
    # price score 100, schedule 100, comfort 100 -> composite well above 80.
    now = datetime.now(timezone.utc)
    db.insert_tracked_date(
        depart_date="2026-09-12", return_date="2026-09-14",
        depart_time_from="17:00", depart_time_to="21:00",
    )
    for p in [120.0, 130.0, 140.0, 150.0, 160.0]:
        _insert(db, p, observed_at=now.isoformat())
    obs_id = _insert(
        db, 75.0, observed_at=now.isoformat(),
        depart_time="18:00", return_time="19:00", duration_min=75,
    )
    deals = score_and_detect(config, db, [obs_id])
    assert len(deals) == 1
    assert deals[0].reason == "score"
