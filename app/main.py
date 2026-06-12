"""Application entry point: scheduler + Telegram bot.

Boot sequence: load config, init the database, build the bot, schedule the
collection/scoring/alert job (every N hours) and the daily digest (08:00
Europe/Paris), then run the bot's long-polling loop. SIGTERM triggers a clean
shutdown. Collection and digest jobs are wrapped so a failure logs and waits
for the next tick rather than crashing the process.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .analyst import Analyst
from .bot import TravelBot, format_deal_alert
from .collector import run_collection
from .config import Config, load_config
from .db import Database
from .scoring import score_and_detect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _build_deal_context(db: Database, config: Config, deal) -> dict:
    """Assemble the compact context passed to the LLM for one deal."""
    obs = db.get_observation(deal.observation_id)
    median = db.baseline_median(
        obs["origin"], obs["destination"], obs["depart_date"], config.baseline_window_days
    )
    p10 = db.baseline_percentile(
        obs["origin"], obs["destination"], obs["depart_date"], 10.0, config.baseline_window_days
    )
    recent = db.recent_prices(obs["origin"], obs["destination"], obs["depart_date"], limit=3)
    return {
        "route": f"{obs['origin']}-{obs['destination']}",
        "depart_date": obs["depart_date"],
        "return_date": obs["return_date"],
        "carrier": obs["carrier"],
        "price_eur": round(float(obs["price_eur"]), 2),
        "median_30d": round(median, 2) if median is not None else None,
        "p10_30d": round(p10, 2) if p10 is not None else None,
        "recent_prices": [round(p, 2) for p in recent],
        "score": round(deal.score, 1),
        "components": deal.components.as_dict(),
        "trigger": deal.reason,
    }


def _build_digest_context(db: Database, config: Config) -> dict:
    """Assemble per-route stats and the top 5 deals for the daily digest."""
    routes_ctx = []
    for route in config.routes:
        cur = db.conn.execute(
            """
            SELECT MIN(price_eur) AS mn, MAX(price_eur) AS mx
            FROM price_observations
            WHERE origin = ? AND destination = ?
            """,
            (route.origin, route.destination),
        )
        row = cur.fetchone()
        prices_cur = db.conn.execute(
            "SELECT price_eur FROM price_observations WHERE origin = ? AND destination = ?",
            (route.origin, route.destination),
        )
        prices = sorted(float(r["price_eur"]) for r in prices_cur.fetchall())
        median = prices[len(prices) // 2] if prices else None
        if row and row["mn"] is not None:
            routes_ctx.append(
                {
                    "route": route.label(),
                    "min": round(float(row["mn"]), 2),
                    "max": round(float(row["mx"]), 2),
                    "median": round(median, 2) if median is not None else None,
                }
            )

    top_cur = db.conn.execute(
        """
        SELECT o.origin, o.destination, o.depart_date, o.return_date,
               o.price_eur, s.score
        FROM flight_scores s
        JOIN price_observations o ON o.id = s.observation_id
        ORDER BY s.score DESC
        LIMIT 5
        """
    )
    top = [
        {
            "route": f"{r['origin']}-{r['destination']}",
            "depart_date": r["depart_date"],
            "return_date": r["return_date"],
            "price_eur": round(float(r["price_eur"]), 2),
            "score": round(float(r["score"]), 1),
        }
        for r in top_cur.fetchall()
    ]
    return {"date": datetime.now(timezone.utc).date().isoformat(), "routes": routes_ctx, "top_deals": top}


class App:
    """Wires together db, bot, analyst, and the scheduler."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self.db.init_schema()
        self.bot = TravelBot(config, self.db)
        self.analyst = Analyst(config)
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)

    async def collection_tick(self) -> None:
        """One scheduled cycle: collect -> score -> alert. Never raises."""
        try:
            inserted = await asyncio.to_thread(run_collection, self.config, self.db)
            deals = await asyncio.to_thread(
                score_and_detect, self.config, self.db, inserted
            )
            if self.bot.alerts_paused():
                if deals:
                    logger.info("%d deals détectés mais alertes en pause.", len(deals))
                return
            for deal in deals:
                try:
                    ctx = _build_deal_context(self.db, self.config, deal)
                    llm_text = await asyncio.to_thread(self.analyst.analyze_deal, ctx)
                    obs = self.db.get_observation(deal.observation_id)
                    score_row = self.db.get_score(deal.observation_id)
                    components_json = score_row["components"] if score_row else "{}"
                    text = format_deal_alert(obs, components_json, llm_text)
                    await self.bot.send_alert(text, deal.observation_id)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Envoi alerte échoué (obs %d) : %s", deal.observation_id, exc)
        except Exception as exc:  # noqa: BLE001 — never crash the scheduler
            logger.error("Cycle de collecte échoué : %s", exc)

    async def digest_tick(self) -> None:
        """Daily digest. Never raises."""
        try:
            if self.bot.alerts_paused():
                logger.info("Digest ignoré : alertes en pause.")
                return
            ctx = await asyncio.to_thread(_build_digest_context, self.db, self.config)
            text = await asyncio.to_thread(self.analyst.daily_digest, ctx)
            await self.bot.send_message("📊 " + text)
        except Exception as exc:  # noqa: BLE001
            logger.error("Digest échoué : %s", exc)

    def _schedule_jobs(self) -> None:
        self.scheduler.add_job(
            self.collection_tick,
            IntervalTrigger(hours=self.config.collect_interval_hours),
            id="collection",
            next_run_time=datetime.now(timezone.utc),  # run once at startup
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.digest_tick,
            CronTrigger(hour=self.config.digest_hour, minute=0, timezone=self.config.timezone),
            id="digest",
            max_instances=1,
        )

    async def run(self) -> None:
        """Run the bot polling loop with the scheduler attached."""
        self._schedule_jobs()
        self.scheduler.start()

        await self.bot.application.initialize()
        await self.bot.application.start()
        await self.bot.application.updater.start_polling()
        logger.info("Agent IA Travel démarré.")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, ValueError):
                pass  # signals not available (e.g. non-main thread / Windows)

        try:
            await stop_event.wait()
        finally:
            logger.info("Arrêt en cours…")
            self.scheduler.shutdown(wait=False)
            await self.bot.application.updater.stop()
            await self.bot.application.stop()
            await self.bot.application.shutdown()
            self.db.close()
            logger.info("Arrêt propre terminé.")


def main(config: Optional[Config] = None) -> None:
    cfg = config or load_config()
    app = App(cfg)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
