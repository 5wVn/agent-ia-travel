"""SQLite access layer — the single source of truth.

Schema follows PLAN.md section 3 exactly. The database runs in WAL mode for
resilience. ``price_observations`` is append-only; everything else (baseline,
scores, alerts, decisions) derives from it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_observations (
    id            INTEGER PRIMARY KEY,
    observed_at   TEXT NOT NULL,
    origin        TEXT NOT NULL,
    destination   TEXT NOT NULL,
    depart_date   TEXT NOT NULL,
    return_date   TEXT,
    carrier       TEXT NOT NULL,
    price_eur     REAL NOT NULL,
    deep_link     TEXT,
    raw_offer     TEXT,
    source        TEXT NOT NULL DEFAULT 'amadeus',
    depart_time   TEXT,
    return_time   TEXT,
    duration_min  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_obs_route_date
    ON price_observations(origin, destination, depart_date, observed_at);

CREATE TABLE IF NOT EXISTS tracked_dates (
    id               INTEGER PRIMARY KEY,
    created_at       TEXT NOT NULL,
    depart_date      TEXT NOT NULL,
    return_date      TEXT,
    depart_time_from TEXT,
    depart_time_to   TEXT,
    return_time_from TEXT,
    return_time_to   TEXT,
    active           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS decisions (
    id             INTEGER PRIMARY KEY,
    created_at     TEXT NOT NULL,
    observation_id INTEGER REFERENCES price_observations(id),
    action         TEXT NOT NULL,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS flight_scores (
    observation_id INTEGER PRIMARY KEY REFERENCES price_observations(id),
    computed_at    TEXT NOT NULL,
    score          REAL NOT NULL,
    components     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    id             INTEGER PRIMARY KEY,
    sent_at        TEXT NOT NULL,
    observation_id INTEGER REFERENCES price_observations(id),
    deal_key       TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS api_calls (
    id          INTEGER PRIMARY KEY,
    called_at   TEXT NOT NULL,
    endpoint    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    """Current UTC instant as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thin wrapper over a SQLite connection with the project's access helpers.

    A single connection is reused; ``check_same_thread=False`` allows use from
    the scheduler's worker thread (collection runs via ``asyncio.to_thread``)
    and the bot's asyncio loop thread. Because both threads share one
    connection, every write (the ``execute`` + ``commit``/``rollback`` group)
    is guarded by ``_write_lock`` so a transaction boundary from one thread
    cannot commit or roll back another thread's in-flight changes.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self._write_lock = threading.Lock()

    def init_schema(self) -> None:
        """Create tables and indexes if they do not exist."""
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Cursor]:
        # Serialize the whole execute+commit group: with a shared connection
        # across threads, an unguarded commit/rollback would otherwise apply to
        # another thread's pending changes (lost writes / spurious rollbacks).
        with self._write_lock:
            cur = self.conn.cursor()
            try:
                yield cur
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                cur.close()

    # ----- price_observations --------------------------------------------

    def insert_observation(
        self,
        *,
        origin: str,
        destination: str,
        depart_date: str,
        return_date: Optional[str],
        carrier: str,
        price_eur: float,
        deep_link: Optional[str],
        raw_offer: Optional[dict[str, Any] | str],
        source: str = "amadeus",
        depart_time: Optional[str] = None,
        return_time: Optional[str] = None,
        duration_min: Optional[int] = None,
        observed_at: Optional[str] = None,
    ) -> int:
        """Insert one observation and return its id. Never overwrites."""
        if isinstance(raw_offer, (dict, list)):
            raw_offer = json.dumps(raw_offer, separators=(",", ":"))
        with self._write() as cur:
            cur.execute(
                """
                INSERT INTO price_observations
                    (observed_at, origin, destination, depart_date, return_date,
                     carrier, price_eur, deep_link, raw_offer, source,
                     depart_time, return_time, duration_min)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observed_at or _now(),
                    origin,
                    destination,
                    depart_date,
                    return_date,
                    carrier,
                    price_eur,
                    deep_link,
                    raw_offer,
                    source,
                    depart_time,
                    return_time,
                    duration_min,
                ),
            )
            return int(cur.lastrowid)

    def get_observation(self, observation_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM price_observations WHERE id = ?", (observation_id,)
        )
        return cur.fetchone()

    def baseline_median(
        self,
        origin: str,
        destination: str,
        depart_date: str,
        window_days: int = 30,
        now: Optional[datetime] = None,
    ) -> Optional[float]:
        """Rolling median price over the last ``window_days`` for a route/date.

        Returns ``None`` when there is no history yet. Median is computed in
        Python (SQLite lacks a built-in median) over all observations of the
        same origin/destination/depart_date within the window.
        """
        prices = self._baseline_prices(
            origin, destination, depart_date, window_days, now
        )
        if not prices:
            return None
        return _median(prices)

    def baseline_percentile(
        self,
        origin: str,
        destination: str,
        depart_date: str,
        percentile: float,
        window_days: int = 30,
        now: Optional[datetime] = None,
    ) -> Optional[float]:
        """Percentile (0..100) of prices over the rolling window, or None."""
        prices = self._baseline_prices(
            origin, destination, depart_date, window_days, now
        )
        if not prices:
            return None
        return _percentile(prices, percentile)

    def _baseline_prices(
        self,
        origin: str,
        destination: str,
        depart_date: str,
        window_days: int,
        now: Optional[datetime],
    ) -> list[float]:
        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = (now.timestamp() - window_days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            SELECT price_eur FROM price_observations
            WHERE origin = ? AND destination = ? AND depart_date = ?
              AND observed_at >= ?
            """,
            (origin, destination, depart_date, cutoff_iso),
        )
        return [float(r["price_eur"]) for r in cur.fetchall()]

    def recent_prices(
        self,
        origin: str,
        destination: str,
        depart_date: str,
        limit: int = 3,
    ) -> list[float]:
        """Last ``limit`` observation prices (most recent first) for trend."""
        cur = self.conn.execute(
            """
            SELECT price_eur FROM price_observations
            WHERE origin = ? AND destination = ? AND depart_date = ?
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            (origin, destination, depart_date, limit),
        )
        return [float(r["price_eur"]) for r in cur.fetchall()]

    # ----- tracked_dates --------------------------------------------------

    def insert_tracked_date(
        self,
        *,
        depart_date: str,
        return_date: Optional[str],
        depart_time_from: Optional[str] = None,
        depart_time_to: Optional[str] = None,
        return_time_from: Optional[str] = None,
        return_time_to: Optional[str] = None,
    ) -> int:
        with self._write() as cur:
            cur.execute(
                """
                INSERT INTO tracked_dates
                    (created_at, depart_date, return_date, depart_time_from,
                     depart_time_to, return_time_from, return_time_to, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    _now(),
                    depart_date,
                    return_date,
                    depart_time_from,
                    depart_time_to,
                    return_time_from,
                    return_time_to,
                ),
            )
            return int(cur.lastrowid)

    def active_tracked_dates(self) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM tracked_dates WHERE active = 1 ORDER BY depart_date"
        )
        return cur.fetchall()

    def all_tracked_dates(self) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM tracked_dates ORDER BY active DESC, depart_date"
        )
        return cur.fetchall()

    def deactivate_tracked_date(self, tracked_id: int) -> None:
        with self._write() as cur:
            cur.execute(
                "UPDATE tracked_dates SET active = 0 WHERE id = ?", (tracked_id,)
            )

    def matching_tracked_date(
        self, depart_date: str, return_date: Optional[str]
    ) -> Optional[sqlite3.Row]:
        """Return the active tracked_dates row matching this departure/return."""
        cur = self.conn.execute(
            """
            SELECT * FROM tracked_dates
            WHERE active = 1 AND depart_date = ?
              AND (return_date = ? OR (return_date IS NULL AND ? IS NULL))
            LIMIT 1
            """,
            (depart_date, return_date, return_date),
        )
        return cur.fetchone()

    # ----- flight_scores --------------------------------------------------

    def upsert_score(
        self,
        observation_id: int,
        score: float,
        components: dict[str, float],
    ) -> None:
        """Insert or replace a derived score for an observation."""
        with self._write() as cur:
            cur.execute(
                """
                INSERT INTO flight_scores (observation_id, computed_at, score, components)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    computed_at = excluded.computed_at,
                    score = excluded.score,
                    components = excluded.components
                """,
                (
                    observation_id,
                    _now(),
                    score,
                    json.dumps(components, separators=(",", ":")),
                ),
            )

    def get_score(self, observation_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM flight_scores WHERE observation_id = ?", (observation_id,)
        )
        return cur.fetchone()

    # ----- decisions ------------------------------------------------------

    def log_decision(
        self, observation_id: Optional[int], action: str, note: Optional[str] = None
    ) -> int:
        with self._write() as cur:
            cur.execute(
                """
                INSERT INTO decisions (created_at, observation_id, action, note)
                VALUES (?, ?, ?, ?)
                """,
                (_now(), observation_id, action, note),
            )
            return int(cur.lastrowid)

    def recent_decisions(self, limit: int = 20) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return cur.fetchall()

    # ----- alerts_sent (dedup) -------------------------------------------

    def try_register_alert(self, observation_id: int, deal_key: str) -> bool:
        """Register a deal_key. Return True if newly registered, False if dup.

        The UNIQUE constraint on deal_key provides atomic deduplication.
        """
        try:
            with self._write() as cur:
                cur.execute(
                    """
                    INSERT INTO alerts_sent (sent_at, observation_id, deal_key)
                    VALUES (?, ?, ?)
                    """,
                    (_now(), observation_id, deal_key),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def alert_exists(self, deal_key: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM alerts_sent WHERE deal_key = ? LIMIT 1", (deal_key,)
        )
        return cur.fetchone() is not None

    # ----- api_calls (quota) ---------------------------------------------

    def record_api_call(self, endpoint: str = "flight-offers") -> None:
        with self._write() as cur:
            cur.execute(
                "INSERT INTO api_calls (called_at, endpoint) VALUES (?, ?)",
                (_now(), endpoint),
            )

    def api_calls_this_month(self, now: Optional[datetime] = None) -> int:
        if now is None:
            now = datetime.now(timezone.utc)
        month_start = now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        cur = self.conn.execute(
            "SELECT COUNT(*) AS n FROM api_calls WHERE called_at >= ?", (month_start,)
        )
        row = cur.fetchone()
        return int(row["n"]) if row else 0

    # ----- app_state ------------------------------------------------------

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        cur = self.conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._write() as cur:
            cur.execute(
                """
                INSERT INTO app_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def last_collection_at(self) -> Optional[str]:
        """ISO timestamp of the most recent observation, or None."""
        cur = self.conn.execute(
            "SELECT MAX(observed_at) AS ts FROM price_observations"
        )
        row = cur.fetchone()
        return row["ts"] if row and row["ts"] else None

    def best_current_price(
        self, depart_date: str, return_date: Optional[str]
    ) -> Optional[sqlite3.Row]:
        """Cheapest observation seen for a depart/return pair (any route)."""
        cur = self.conn.execute(
            """
            SELECT * FROM price_observations
            WHERE depart_date = ?
              AND (return_date = ? OR (return_date IS NULL AND ? IS NULL))
            ORDER BY price_eur ASC
            LIMIT 1
            """,
            (depart_date, return_date, return_date),
        )
        return cur.fetchone()


def make_deal_key(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str],
    carrier: str,
    price_eur: float,
    bucket_eur: int = 10,
) -> str:
    """Stable hash of route + dates + carrier + a price bucket of ``bucket_eur``.

    Bucketing the price means a deal stays "the same deal" while it drifts
    within a 10 EUR band, so we don't re-notify on every small move.
    """
    bucket = int(price_eur // bucket_eur)
    raw = "|".join(
        [
            origin,
            destination,
            depart_date,
            return_date or "",
            carrier,
            str(bucket),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _percentile(values: list[float], percentile: float) -> float:
    """Linear-interpolation percentile (percentile in 0..100)."""
    if not values:
        raise ValueError("empty values")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (percentile / 100.0) * (len(s) - 1)
    low = int(rank)
    high = min(low + 1, len(s) - 1)
    frac = rank - low
    return s[low] + (s[high] - s[low]) * frac
