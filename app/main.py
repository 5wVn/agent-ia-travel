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
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .analyst import Analyst
from .bot import TravelBot, format_deal_alert
from .collector import (
    build_snipe_queries,
    quota_allows,
    run_collection,
    standard_scan_should_defer,
)
from .providers import build_provider
from .config import Config, load_config
from .db import Database
from .scoring import score_and_detect
from .sniper import SnipeResult, evaluate_snipe, snipe_candidates

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
        # Cancellable re-ping job ids per triggered snipe (tracked_id -> [ids]).
        self._reping_jobs: dict[int, list[str]] = {}
        self.bot.cancel_repings = self._cancel_repings

    # ----- price sniper orchestration -------------------------------------

    def _cancel_repings(self, tracked_id: int) -> None:
        """Remove any pending re-ping jobs for a snipe (called on user reply)."""
        for job_id in self._reping_jobs.pop(tracked_id, []):
            try:
                self.scheduler.remove_job(job_id)
            except Exception:  # noqa: BLE001 — job may already have fired
                pass

    def _schedule_repings(self, tracked_id: int) -> None:
        """Schedule up to ``snipe_reping_max`` cancellable re-ping jobs."""
        self._cancel_repings(tracked_id)
        ids: list[str] = []
        total = self.config.snipe_reping_max
        for n in range(1, total + 1):
            job_id = f"reping:{tracked_id}:{n}"
            self.scheduler.add_job(
                self._reping_tick,
                "date",
                run_date=datetime.now(timezone.utc)
                + timedelta(minutes=self.config.snipe_reping_interval_minutes * n),
                args=[tracked_id, n, total],
                id=job_id,
                max_instances=1,
            )
            ids.append(job_id)
        self._reping_jobs[tracked_id] = ids

    async def _reping_tick(self, tracked_id: int, n: int, total: int) -> None:
        """Send one re-ping, unless the snipe is no longer triggered."""
        try:
            tr = self.db.get_tracked_date(tracked_id)
            if tr is None or tr["snipe_state"] != "triggered":
                self._cancel_repings(tracked_id)
                return
            await self.bot.send_reping(tracked_id, n, total)
        except Exception as exc:  # noqa: BLE001
            logger.error("Re-ping snipe %d échoué : %s", tracked_id, exc)

    async def snipe_tick(self) -> None:
        """Boosted watch: collect only near-threshold snipes, then evaluate.

        Runs every ``snipe_interval_minutes``. Quota-priority: sniper collects
        come first, so the standard scan defers when the budget is tight (see
        ``collection_tick``). Never raises.
        """
        try:
            candidates = await asyncio.to_thread(snipe_candidates, self.config, self.db)
            if not candidates:
                return
            if not await asyncio.to_thread(quota_allows, self.config, self.db):
                logger.warning("Quota atteint — snipe tick sans collecte fraîche.")
            else:
                queries = build_snipe_queries(self.config, candidates)
                await asyncio.to_thread(
                    run_collection, self.config, self.db, None, queries
                )

            provider = await asyncio.to_thread(build_provider, self.config)
            try:
                for row in candidates:
                    tr = self.db.get_tracked_date(int(row["id"]))
                    if tr is None or tr["snipe_state"] != "armed":
                        continue  # disarmed/triggered meanwhile
                    result = await asyncio.to_thread(
                        evaluate_snipe, self.config, self.db, tr, provider
                    )
                    await self._handle_snipe_result(result)
            finally:
                await asyncio.to_thread(provider.close)
        except Exception as exc:  # noqa: BLE001 — never crash the scheduler
            logger.error("Snipe tick échoué : %s", exc)

    async def _handle_snipe_result(self, result: SnipeResult) -> None:
        """Turn a sniper evaluation into a Telegram alert + re-ping jobs."""
        if self.bot.alerts_paused():
            return
        if result.status == "triggered":
            self.db.set_snipe_state(result.tracked_id, "triggered")
            ret = f"→{result.return_date}" if result.return_date else ""
            price = result.confirmed_price_eur or result.best_price_eur
            text = (
                f"🎯 SNIPE DÉCLENCHÉ : {result.depart_date}{ret}\n"
                f"💶 {price:.0f} € (seuil ≤ {result.threshold_eur:.0f} €) — prix "
                "confirmé en direct.\nValide vite avant qu'il ne remonte."
            )
            if result.freshness_note:
                # Cached provider (Travelpayouts): warn the price may have moved.
                text += f"\n⚠️ {result.freshness_note}"
            await self.bot.send_triggered_alert(text, result.tracked_id)
            self._schedule_repings(result.tracked_id)
        elif result.status == "rebounded":
            # Live price climbed back: stay armed, no re-ping.
            self.db.set_snipe_state(result.tracked_id, "armed")
            logger.info(
                "Snipe %d : prix remonté à la re-vérification, reste armé.",
                result.tracked_id,
            )

    async def collection_tick(self) -> None:
        """One scheduled cycle: collect -> score -> alert. Never raises.

        Quota-priority: if armed snipes are close to threshold and the budget is
        tight, the standard weekend scan defers this tick so the sniper keeps
        enough quota for its boosted re-checks.
        """
        try:
            candidates = await asyncio.to_thread(
                snipe_candidates, self.config, self.db
            )
            if await asyncio.to_thread(
                standard_scan_should_defer, self.config, self.db, len(candidates)
            ):
                logger.info(
                    "Scan week-ends reporté : quota réservé aux %d snipes proches.",
                    len(candidates),
                )
                return
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
        self.scheduler.add_job(
            self.snipe_tick,
            IntervalTrigger(minutes=self.config.snipe_interval_minutes),
            id="snipe",
            max_instances=1,
            coalesce=True,
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
