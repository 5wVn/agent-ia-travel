"""Container healthcheck.

Exit 0 when the database is reachable and the last collection is recent
(within 2x the configured interval). Exit 1 otherwise. Used by docker-compose.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone


def _interval_hours() -> int:
    raw = os.environ.get("COLLECT_INTERVAL_HOURS", "4")
    try:
        return int(raw)
    except ValueError:
        return 4


def check() -> int:
    """Return 0 if healthy, 1 otherwise."""
    db_path = os.environ.get("DB_PATH", "/data/prices.db")
    # Import here so the module is importable without a full config.
    from .db import Database

    try:
        db = Database(db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"DB inaccessible : {exc}", file=sys.stderr)
        return 1

    try:
        last = db.last_collection_at()
    finally:
        db.close()

    if last is None:
        # No collection yet — tolerate at startup.
        print("Aucune collecte encore enregistrée (démarrage).")
        return 0

    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        print(f"Horodatage de collecte illisible : {last}", file=sys.stderr)
        return 1

    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last_dt
    max_age = timedelta(hours=2 * _interval_hours())
    if age > max_age:
        print(
            f"Dernière collecte trop ancienne : {age} (> {max_age})",
            file=sys.stderr,
        )
        return 1
    print(f"OK — dernière collecte il y a {age}.")
    return 0


if __name__ == "__main__":
    sys.exit(check())
