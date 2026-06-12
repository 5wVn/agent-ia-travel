"""Tests for the pure inline-keyboard builders (no Telegram network)."""

from __future__ import annotations

from app.bot import build_time_grid


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
