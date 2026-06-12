"""Display helpers shared across alerts, digest, analyst templates and the web.

Single source of truth for rendering a flight's number of stops (escales) so the
Telegram alerts, the LLM context/fallbacks, the daily digest and the dashboard
all phrase it identically. Pure formatting only — no scoring, no filtering.
"""

from __future__ import annotations

from typing import Optional


def format_stops(transfers: Optional[int]) -> Optional[str]:
    """Render a stop count as French text, or ``None`` when unknown.

    ``0`` -> ``"direct"``, ``1`` -> ``"1 escale"``, ``n`` -> ``"n escales"``.
    ``None`` (and any non-integer) returns ``None`` so callers omit the mention
    entirely rather than printing a placeholder.
    """
    if transfers is None:
        return None
    try:
        n = int(transfers)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    if n == 0:
        return "direct"
    if n == 1:
        return "1 escale"
    return f"{n} escales"


def format_stops_roundtrip(
    transfers: Optional[int], return_transfers: Optional[int]
) -> Optional[str]:
    """Render the stops of a round trip, e.g. ``"direct / retour 1 escale"``.

    When only the outbound is known, only it is shown. When both legs match, a
    single mention is returned (no redundant "aller/retour"). When they differ,
    both legs are labelled. Returns ``None`` when nothing is known.
    """
    out = format_stops(transfers)
    back = format_stops(return_transfers)
    if out is None and back is None:
        return None
    if back is None:
        return out
    if out is None:
        return f"retour {back}"
    if out == back:
        return out
    return f"{out} / retour {back}"
