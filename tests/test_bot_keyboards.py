"""Tests for the pure inline-keyboard builders (no Telegram network)."""

from __future__ import annotations

from app.bot import (
    build_amount_grid,
    build_time_grid,
    build_triggered_keyboard,
    format_deal_alert,
    format_triggered_alert,
)


def _all_callback_data(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def test_time_grid_start_step_has_peu_importe():
    markup = build_time_grid("d", min_hour=None, is_end=False)
    assert any(cb.endswith(":any:") for cb in _all_callback_data(markup))


def test_time_grid_end_step_omits_peu_importe():
    # On the end step, "Peu importe" is hidden so it cannot wipe the chosen
    # start and leave an ambiguous half-open window.
    markup = build_time_grid("d", min_hour=18, is_end=True)
    assert not any(cb.endswith(":any:") for cb in _all_callback_data(markup))


def test_time_grid_callback_data_within_telegram_limit():
    # Telegram caps callback_data at 64 bytes.
    markup = build_time_grid("d", min_hour=None, is_end=False)
    for cb in _all_callback_data(markup):
        assert len(cb.encode("utf-8")) <= 64


def _amount_labels(markup) -> list[str]:
    return [
        btn.text
        for row in markup.inline_keyboard
        for btn in row
        if btn.text.endswith("€")
    ]


def test_amount_grid_covers_full_range():
    # 30..120 step 5 = 19 amounts; with a large page size they fit on one page.
    markup = build_amount_grid(7, 30, 120, 5, page=0, page_size=100)
    labels = _amount_labels(markup)
    assert "30 €" in labels
    assert "120 €" in labels
    assert len(labels) == 19


def test_amount_grid_paginates():
    # page_size 18 over 19 amounts -> two pages.
    p0 = build_amount_grid(7, 30, 120, 5, page=0, page_size=18)
    p1 = build_amount_grid(7, 30, 120, 5, page=1, page_size=18)
    assert len(_amount_labels(p0)) == 18
    assert len(_amount_labels(p1)) == 1
    # Page 0 has a "next" nav, page 1 has a "prev" nav.
    assert any(":page:7:1" in cb for cb in _all_callback_data(p0))
    assert any(":page:7:0" in cb for cb in _all_callback_data(p1))


def test_amount_grid_callback_data_within_telegram_limit():
    markup = build_amount_grid(999999, 30, 120, 5, page=0, page_size=18)
    for cb in _all_callback_data(markup):
        assert len(cb.encode("utf-8")) <= 64


def test_triggered_keyboard_callback_data_within_limit():
    markup = build_triggered_keyboard(123456)
    cbs = _all_callback_data(markup)
    assert any(cb.startswith("tg:book:") for cb in cbs)
    assert any(cb.startswith("tg:keep:") for cb in cbs)
    assert any(cb.startswith("tg:off:") for cb in cbs)
    for cb in cbs:
        assert len(cb.encode("utf-8")) <= 64


# ----- alert message bodies (stops mention) ------------------------------


def _obs(**kw):
    base = dict(
        origin="TLS", destination="ORY", depart_date="2026-09-12",
        return_date="2026-09-14", price_eur=54.0, transfers=0,
        return_transfers=0,
    )
    base.update(kw)
    return base


def test_deal_alert_shows_direct():
    text = format_deal_alert(_obs(transfers=0, return_transfers=0), "{}", "ok")
    assert "direct" in text
    assert "escale" not in text


def test_deal_alert_shows_escales_and_asymmetry():
    text = format_deal_alert(_obs(transfers=0, return_transfers=1), "{}", "ok")
    assert "direct / retour 1 escale" in text


def test_deal_alert_omits_stops_when_unknown():
    text = format_deal_alert(_obs(transfers=None, return_transfers=None), "{}", "ok")
    assert "escale" not in text
    assert "direct" not in text


def test_triggered_alert_shows_stops():
    text = format_triggered_alert(
        depart_date="2026-09-12", return_date="2026-09-14",
        price_eur=48.0, threshold_eur=55.0, transfers=1, return_transfers=1,
    )
    assert "SNIPE DÉCLENCHÉ" in text
    assert "1 escale" in text


def test_triggered_alert_omits_stops_when_unknown():
    text = format_triggered_alert(
        depart_date="2026-09-12", return_date="2026-09-14",
        price_eur=48.0, threshold_eur=55.0, transfers=None, return_transfers=None,
    )
    assert "escale" not in text
    # No stops mention is appended to the flight line ("· direct" / "· N escale").
    assert " · " not in text
