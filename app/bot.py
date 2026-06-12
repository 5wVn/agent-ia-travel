"""Telegram bot: alerts with inline buttons, commands, interactive calendar.

Long polling only (outbound traffic). Restricted to the configured chat id.
Inline calendars and time-range pickers are built by hand with
``InlineKeyboardMarkup`` — no external calendar dependency. The /track flow
walks: depart calendar -> depart time range -> return calendar -> return time
range -> confirmation -> insert into tracked_dates.
"""

from __future__ import annotations

import calendar
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .config import Config
from .db import Database
from .formatting import format_stops_roundtrip

logger = logging.getLogger(__name__)

# Callback data prefixes (kept short — Telegram caps callback_data at 64 bytes).
CB_DEAL = "deal"          # deal:<action>:<observation_id>
CB_CAL = "cal"            # cal:<role>:<action>:<payload>
CB_TIME = "tm"            # tm:<role>:<action>:<payload>
CB_UNTRACK = "ut"         # ut:<tracked_id>
CB_CONFIRM = "cf"         # cf:<yes|no>
CB_SNIPE = "sn"           # sn:pick:<tracked_id> | sn:amt:<tracked_id>:<eur>
                          # sn:page:<tracked_id>:<page> | sn:off:<tracked_id>
CB_TRIG = "tg"            # tg:<action>:<tracked_id>  (triggered-snipe buttons)

ROLE_DEPART = "d"
ROLE_RETURN = "r"

FRENCH_MONTHS = [
    "",
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


@dataclass
class TrackDraft:
    """In-progress /track selection for the single authorized user."""

    depart_date: Optional[str] = None
    return_date: Optional[str] = None
    depart_time_from: Optional[str] = None
    depart_time_to: Optional[str] = None
    return_time_from: Optional[str] = None
    return_time_to: Optional[str] = None
    # Transient navigation/selection state.
    cal_year: int = field(default_factory=lambda: date.today().year)
    cal_month: int = field(default_factory=lambda: date.today().month)
    pending_time_start: Optional[str] = None  # first time click, awaiting end


# ----- calendar / time keyboards (pure builders) -------------------------


def build_calendar(year: int, month: int, role: str, min_date: Optional[date]) -> InlineKeyboardMarkup:
    """Build a month grid. Days before ``min_date`` are non-clickable."""
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton("◀", callback_data=f"{CB_CAL}:{role}:prev:"),
            InlineKeyboardButton(
                f"{FRENCH_MONTHS[month]} {year}", callback_data=f"{CB_CAL}:{role}:noop:"
            ),
            InlineKeyboardButton("▶", callback_data=f"{CB_CAL}:{role}:next:"),
        ]
    )
    rows.append(
        [InlineKeyboardButton(d, callback_data=f"{CB_CAL}:{role}:noop:")
         for d in ["L", "Ma", "Me", "J", "V", "S", "D"]]
    )
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        line: list[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                line.append(InlineKeyboardButton(" ", callback_data=f"{CB_CAL}:{role}:noop:"))
                continue
            d = date(year, month, day)
            if min_date is not None and d < min_date:
                line.append(InlineKeyboardButton("·", callback_data=f"{CB_CAL}:{role}:noop:"))
            else:
                iso = d.isoformat()
                line.append(InlineKeyboardButton(str(day), callback_data=f"{CB_CAL}:{role}:pick:{iso}"))
        rows.append(line)
    return InlineKeyboardMarkup(rows)


def build_time_grid(
    role: str, min_hour: Optional[int], is_end: bool = False
) -> InlineKeyboardMarkup:
    """Build an hours grid 06h-22h. Hours < ``min_hour`` are non-clickable.

    ``is_end`` is True for the second click (choosing the end of the range):
    the "Peu importe" button is then omitted, because it would wipe the start
    already chosen and leave an ambiguous half-open window. "Peu importe" is
    only meaningful at the start step, where it means "no preference at all".
    """
    rows: list[list[InlineKeyboardButton]] = []
    hours = list(range(6, 23))
    for i in range(0, len(hours), 4):
        line: list[InlineKeyboardButton] = []
        for hour in hours[i : i + 4]:
            label = f"{hour:02d}h"
            if min_hour is not None and hour < min_hour:
                line.append(InlineKeyboardButton("·", callback_data=f"{CB_TIME}:{role}:noop:"))
            else:
                line.append(
                    InlineKeyboardButton(label, callback_data=f"{CB_TIME}:{role}:pick:{hour:02d}:00")
                )
        rows.append(line)
    if not is_end:
        rows.append([InlineKeyboardButton("Peu importe", callback_data=f"{CB_TIME}:{role}:any:")])
    return InlineKeyboardMarkup(rows)


def build_deal_keyboard(observation_id: int) -> InlineKeyboardMarkup:
    """The [Réserver][Attendre][Ignorer] inline keyboard for a deal alert."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Réserver", callback_data=f"{CB_DEAL}:book:{observation_id}"),
                InlineKeyboardButton("⏳ Attendre", callback_data=f"{CB_DEAL}:wait:{observation_id}"),
                InlineKeyboardButton("🔕 Ignorer", callback_data=f"{CB_DEAL}:ignore:{observation_id}"),
            ]
        ]
    )


def build_amount_grid(
    tracked_id: int,
    min_eur: int,
    max_eur: int,
    step_eur: int,
    page: int,
    page_size: int,
) -> InlineKeyboardMarkup:
    """Grid of threshold amounts (e.g. 30-120 EUR, step 5), paginated.

    Each amount button's callback_data is ``sn:amt:<tracked_id>:<eur>`` which
    stays well under Telegram's 64-byte cap. Navigation buttons carry the page.
    """
    amounts = list(range(min_eur, max_eur + 1, step_eur))
    pages = max(1, (len(amounts) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    chunk = amounts[start : start + page_size]

    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(chunk), 3):
        line = [
            InlineKeyboardButton(
                f"{eur} €", callback_data=f"{CB_SNIPE}:amt:{tracked_id}:{eur}"
            )
            for eur in chunk[i : i + 3]
        ]
        rows.append(line)

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀", callback_data=f"{CB_SNIPE}:page:{tracked_id}:{page - 1}"
            )
        )
    if pages > 1:
        nav.append(
            InlineKeyboardButton(
                f"{page + 1}/{pages}", callback_data=f"{CB_SNIPE}:noop:"
            )
        )
    if page < pages - 1:
        nav.append(
            InlineKeyboardButton(
                "▶", callback_data=f"{CB_SNIPE}:page:{tracked_id}:{page + 1}"
            )
        )
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def build_triggered_keyboard(tracked_id: int) -> InlineKeyboardMarkup:
    """Buttons for a critical (triggered) snipe alert."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎯 J'achète", callback_data=f"{CB_TRIG}:book:{tracked_id}"
                ),
                InlineKeyboardButton(
                    "⏳ Continue à viser", callback_data=f"{CB_TRIG}:keep:{tracked_id}"
                ),
                InlineKeyboardButton(
                    "🔕 Désarmer", callback_data=f"{CB_TRIG}:off:{tracked_id}"
                ),
            ]
        ]
    )


# ----- the bot -----------------------------------------------------------


class TravelBot:
    """Encapsulates the python-telegram-bot Application and handlers."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.application: Application = (
            Application.builder().token(config.telegram_bot_token).build()
        )
        self._draft = TrackDraft()
        # Set by main.py to (re)schedule cancellable re-ping jobs for a snipe.
        # Signature: cancel_repings(tracked_id) -> None.
        self.cancel_repings = None  # type: ignore[assignment]
        self._register_handlers()

    def _register_handlers(self) -> None:
        app = self.application
        app.add_handler(CommandHandler("start", self.cmd_status))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("track", self.cmd_track))
        app.add_handler(CommandHandler("untrack", self.cmd_untrack))
        app.add_handler(CommandHandler("snipe", self.cmd_snipe))
        app.add_handler(CommandHandler("pause", self.cmd_pause))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_error_handler(self._on_error)

    async def _on_error(self, _update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Swallow handler exceptions (e.g. stale callback queries that can no
        longer be answered/edited after 48h) so the polling loop keeps running.
        """
        logger.error("Erreur dans un handler Telegram : %s", ctx.error)

    # ----- authorization --------------------------------------------------

    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and str(chat.id) == str(self.config.telegram_chat_id)

    # ----- commands -------------------------------------------------------

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        n_routes = len(self.db.active_routes())
        rows = self.db.active_tracked_dates()
        if not rows:
            await update.effective_message.reply_text(
                f"Aucun suivi actif ({n_routes} routes actives). "
                "Utilise /track pour en ajouter."
            )
            return
        lines = [f"Suivis actifs ({n_routes} routes actives) :"]
        for r in rows:
            best = self.db.best_current_price(r["depart_date"], r["return_date"])
            price = f"{best['price_eur']:.0f} EUR" if best else "pas encore de prix"
            ret = f" -> {r['return_date']}" if r["return_date"] else ""
            line = f"• {r['depart_date']}{ret} : {price}"
            state = r["snipe_state"] if "snipe_state" in r.keys() else None
            if state in ("armed", "triggered") and r["snipe_price_eur"] is not None:
                etat = "armé" if state == "armed" else "déclenché"
                line += f"  🎯 snipe {etat} ≤ {float(r['snipe_price_eur']):.0f} €"
            lines.append(line)
        paused = self.db.get_state("alerts_paused", "0") == "1"
        if paused:
            lines.append("\n⏸️ Alertes en pause (/resume pour réactiver).")
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_pause(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self.db.set_state("alerts_paused", "1")
        await update.effective_message.reply_text("⏸️ Alertes mises en pause.")

    async def cmd_resume(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self.db.set_state("alerts_paused", "0")
        await update.effective_message.reply_text("▶️ Alertes réactivées.")

    async def cmd_untrack(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        rows = self.db.active_tracked_dates()
        if not rows:
            await update.effective_message.reply_text("Aucun suivi actif à désactiver.")
            return
        buttons = []
        for r in rows:
            ret = f" -> {r['return_date']}" if r["return_date"] else ""
            buttons.append(
                [InlineKeyboardButton(f"🗑️ {r['depart_date']}{ret}", callback_data=f"{CB_UNTRACK}:{r['id']}")]
            )
        await update.effective_message.reply_text(
            "Choisis un suivi à désactiver :", reply_markup=InlineKeyboardMarkup(buttons)
        )

    def _snipe_amount_grid(self, tracked_id: int, page: int = 0) -> InlineKeyboardMarkup:
        return build_amount_grid(
            tracked_id,
            self.config.snipe_amount_min_eur,
            self.config.snipe_amount_max_eur,
            self.config.snipe_amount_step_eur,
            page,
            self.config.snipe_grid_page_size,
        )

    async def cmd_snipe(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """List active tracked dates to arm a snipe, plus already-armed snipes.

        No active tracked date -> suggest /track. Each active date gets a button
        to (re)arm a threshold; each armed snipe gets a disarm button.
        """
        if not self._authorized(update):
            return
        rows = self.db.active_tracked_dates()
        if not rows:
            await update.effective_message.reply_text(
                "Aucune date suivie. Utilise /track pour en ajouter, "
                "puis /snipe pour armer un seuil."
            )
            return

        buttons: list[list[InlineKeyboardButton]] = []
        for r in rows:
            ret = f"→{r['return_date']}" if r["return_date"] else ""
            label = f"🎯 {r['depart_date']}{ret}"
            state = r["snipe_state"] if "snipe_state" in r.keys() else None
            if state in ("armed", "triggered") and r["snipe_price_eur"] is not None:
                label += f" (armé ≤ {float(r['snipe_price_eur']):.0f} €)"
            buttons.append(
                [InlineKeyboardButton(label, callback_data=f"{CB_SNIPE}:pick:{r['id']}")]
            )

        armed = self.db.armed_snipes()
        for r in armed:
            ret = f"→{r['return_date']}" if r["return_date"] else ""
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"🔕 Désarmer {r['depart_date']}{ret}",
                        callback_data=f"{CB_SNIPE}:off:{r['id']}",
                    )
                ]
            )
        await update.effective_message.reply_text(
            "Choisis une date à sniper (le bot achètera au meilleur prix sur "
            "ta validation) :",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def cmd_track(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        today = date.today()
        self._draft = TrackDraft(cal_year=today.year, cal_month=today.month)
        await update.effective_message.reply_text(
            "Choisis la date de départ :",
            reply_markup=build_calendar(today.year, today.month, ROLE_DEPART, today),
        )

    # ----- callback dispatch ----------------------------------------------

    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        prefix = data.split(":", 1)[0]
        if prefix == CB_DEAL:
            await self._handle_deal_callback(update, ctx, data)
        elif prefix == CB_CAL:
            await self._handle_calendar_callback(update, data)
        elif prefix == CB_TIME:
            await self._handle_time_callback(update, data)
        elif prefix == CB_UNTRACK:
            await self._handle_untrack_callback(update, data)
        elif prefix == CB_CONFIRM:
            await self._handle_confirm_callback(update, data)
        elif prefix == CB_SNIPE:
            await self._handle_snipe_callback(update, data)
        elif prefix == CB_TRIG:
            await self._handle_triggered_callback(update, data)

    async def _handle_deal_callback(
        self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE, data: str
    ) -> None:
        _, action, obs_id_s = data.split(":", 2)
        obs_id = int(obs_id_s)
        query = update.callback_query
        obs = self.db.get_observation(obs_id)
        if action == "book":
            self.db.log_decision(obs_id, "book")
            link = (obs["deep_link"] if obs and obs["deep_link"] else None)
            if link:
                msg = f"✅ Décision enregistrée. Lien de réservation : {link}"
            else:
                route = f"{obs['origin']}-{obs['destination']}" if obs else "vol"
                msg = (
                    "✅ Décision enregistrée. Pas de lien direct pour cette offre "
                    f"— recherche {route} sur Aviasales / Google Flights / le "
                    "site de la compagnie."
                )
            await query.edit_message_text(msg)
        elif action == "wait":
            self.db.log_decision(obs_id, "wait", note="surveillance renforcée")
            if obs is not None:
                tr = self.db.matching_tracked_date(obs["depart_date"], obs["return_date"])
                if tr is None:
                    self.db.insert_tracked_date(
                        depart_date=obs["depart_date"], return_date=obs["return_date"]
                    )
            await query.edit_message_text(
                "⏳ Noté. Surveillance renforcée : nouvelle alerte si le prix bouge."
            )
        elif action == "ignore":
            self.db.log_decision(obs_id, "ignore", note="date désactivée")
            if obs is not None:
                tr = self.db.matching_tracked_date(obs["depart_date"], obs["return_date"])
                if tr is not None:
                    self.db.deactivate_tracked_date(tr["id"])
            await query.edit_message_text("🔕 D'accord, plus d'alertes sur cette date.")

    async def _handle_calendar_callback(self, update: Update, data: str) -> None:
        _, role, action, payload = data.split(":", 3)
        query = update.callback_query
        draft = self._draft
        if action == "noop":
            return
        if action in ("prev", "next"):
            month = draft.cal_month + (1 if action == "next" else -1)
            year = draft.cal_year
            if month < 1:
                month, year = 12, year - 1
            elif month > 12:
                month, year = 1, year + 1
            draft.cal_month, draft.cal_year = month, year
            min_date = self._calendar_min_date(role)
            await query.edit_message_reply_markup(
                reply_markup=build_calendar(year, month, role, min_date)
            )
            return
        if action == "pick":
            if role == ROLE_DEPART:
                draft.depart_date = payload
                draft.pending_time_start = None
                await query.edit_message_text(
                    f"Départ : {payload}. Choisis le début de la fourchette horaire de départ :",
                    reply_markup=build_time_grid(ROLE_DEPART, None),
                )
            else:
                draft.return_date = payload
                draft.pending_time_start = None
                await query.edit_message_text(
                    f"Retour : {payload}. Choisis le début de la fourchette horaire de retour :",
                    reply_markup=build_time_grid(ROLE_RETURN, None),
                )

    def _calendar_min_date(self, role: str) -> Optional[date]:
        if role == ROLE_RETURN and self._draft.depart_date:
            return date.fromisoformat(self._draft.depart_date)
        return date.today()

    async def _handle_time_callback(self, update: Update, data: str) -> None:
        _, role, action, payload = data.split(":", 3)
        query = update.callback_query
        draft = self._draft
        if action == "noop":
            return
        if action == "any":
            self._set_times(role, None, None)
            await self._advance_after_times(update, role)
            return
        if action == "pick":
            if draft.pending_time_start is None:
                draft.pending_time_start = payload
                min_hour = int(payload[:2])
                await query.edit_message_text(
                    f"Début {payload}. Choisis la fin de la fourchette :",
                    reply_markup=build_time_grid(role, min_hour, is_end=True),
                )
            else:
                self._set_times(role, draft.pending_time_start, payload)
                draft.pending_time_start = None
                await self._advance_after_times(update, role)

    def _set_times(self, role: str, lo: Optional[str], hi: Optional[str]) -> None:
        if role == ROLE_DEPART:
            self._draft.depart_time_from = lo
            self._draft.depart_time_to = hi
        else:
            self._draft.return_time_from = lo
            self._draft.return_time_to = hi

    async def _advance_after_times(self, update: Update, role: str) -> None:
        query = update.callback_query
        draft = self._draft
        if role == ROLE_DEPART:
            min_date = self._calendar_min_date(ROLE_RETURN)
            await query.edit_message_text(
                "Choisis la date de retour :",
                reply_markup=build_calendar(draft.cal_year, draft.cal_month, ROLE_RETURN, min_date),
            )
        else:
            await query.edit_message_text(
                self._draft_summary() + "\n\nConfirmer ce suivi ?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ Oui", callback_data=f"{CB_CONFIRM}:yes"),
                            InlineKeyboardButton("❌ Non", callback_data=f"{CB_CONFIRM}:no"),
                        ]
                    ]
                ),
            )

    def _draft_summary(self) -> str:
        d = self._draft

        def window(lo: Optional[str], hi: Optional[str]) -> str:
            return f"{lo}-{hi}" if lo and hi else "peu importe"

        return (
            f"✅ Suivi : {d.depart_date} (départ {window(d.depart_time_from, d.depart_time_to)}) "
            f"→ {d.return_date} (retour {window(d.return_time_from, d.return_time_to)})"
        )

    async def _handle_confirm_callback(self, update: Update, data: str) -> None:
        _, choice = data.split(":", 1)
        query = update.callback_query
        if choice == "no":
            self._draft = TrackDraft()
            await query.edit_message_text("Annulé. Rien n'a été enregistré.")
            return
        d = self._draft
        if not d.depart_date:
            await query.edit_message_text("Erreur : date de départ manquante.")
            return
        self.db.insert_tracked_date(
            depart_date=d.depart_date,
            return_date=d.return_date,
            depart_time_from=d.depart_time_from,
            depart_time_to=d.depart_time_to,
            return_time_from=d.return_time_from,
            return_time_to=d.return_time_to,
        )
        summary = self._draft_summary()
        self._draft = TrackDraft()
        await query.edit_message_text(summary + "\n\nEnregistré.")

    async def _handle_untrack_callback(self, update: Update, data: str) -> None:
        _, tracked_id_s = data.split(":", 1)
        self.db.deactivate_tracked_date(int(tracked_id_s))
        await update.callback_query.edit_message_text("🗑️ Suivi désactivé.")

    # ----- snipe arming / triggered alerts --------------------------------

    async def _handle_snipe_callback(self, update: Update, data: str) -> None:
        parts = data.split(":")
        action = parts[1]
        query = update.callback_query
        if action == "noop":
            return
        if action == "pick":
            tracked_id = int(parts[2])
            await query.edit_message_text(
                "Choisis le seuil de prix (le snipe se déclenche en dessous) :",
                reply_markup=self._snipe_amount_grid(tracked_id, page=0),
            )
            return
        if action == "page":
            tracked_id, page = int(parts[2]), int(parts[3])
            await query.edit_message_reply_markup(
                reply_markup=self._snipe_amount_grid(tracked_id, page=page)
            )
            return
        if action == "amt":
            tracked_id, eur = int(parts[2]), int(parts[3])
            self.db.arm_snipe(tracked_id, float(eur))
            tr = self.db.get_tracked_date(tracked_id)
            ret = f"→{tr['return_date']}" if tr and tr["return_date"] else ""
            depart = tr["depart_date"] if tr else "?"
            await query.edit_message_text(
                f"🎯 Snipe armé : {depart}{ret} à ≤ {eur} €."
            )
            return
        if action == "off":
            tracked_id = int(parts[2])
            self.db.disarm_snipe(tracked_id)
            if self.cancel_repings is not None:
                self.cancel_repings(tracked_id)
            await query.edit_message_text("🔕 Snipe désarmé.")

    async def _handle_triggered_callback(self, update: Update, data: str) -> None:
        _, action, tracked_id_s = data.split(":", 2)
        tracked_id = int(tracked_id_s)
        query = update.callback_query
        # Any response stops the re-pings.
        if self.cancel_repings is not None:
            self.cancel_repings(tracked_id)
        tr = self.db.get_tracked_date(tracked_id)
        best = (
            self.db.best_current_price(tr["depart_date"], tr["return_date"])
            if tr
            else None
        )
        if action == "book":
            obs_id = int(best["id"]) if best else None
            self.db.log_decision(obs_id, "book", note="snipe")
            self.db.disarm_snipe(tracked_id)
            link = best["deep_link"] if best and best["deep_link"] else None
            if link:
                msg = f"🎯 C'est noté ! Lien de réservation : {link}"
            else:
                route = (
                    f"{best['origin']}-{best['destination']}" if best else "TLS-Paris"
                )
                price = f" à {float(best['price_eur']):.0f} €" if best else ""
                msg = (
                    f"🎯 C'est noté ! Pas de lien direct pour cette offre — "
                    f"réserve {route}{price} sur Aviasales / Google Flights / le "
                    "site de la compagnie. Snipe désarmé."
                )
            await query.edit_message_text(msg)
        elif action == "keep":
            self.db.set_snipe_state(tracked_id, "armed")
            await query.edit_message_text(
                "⏳ OK, je continue à viser ce prix — nouvelle alerte au prochain "
                "passage sous le seuil."
            )
        elif action == "off":
            self.db.set_snipe_state(tracked_id, None)
            await query.edit_message_text("🔕 Snipe désarmé.")

    # ----- outbound alerts ------------------------------------------------

    async def send_alert(self, text: str, observation_id: int) -> None:
        """Send a deal alert with the decision keyboard."""
        await self.application.bot.send_message(
            chat_id=self.config.telegram_chat_id,
            text=text,
            reply_markup=build_deal_keyboard(observation_id),
        )

    async def send_message(self, text: str) -> None:
        """Send a plain message (e.g. the daily digest)."""
        await self.application.bot.send_message(
            chat_id=self.config.telegram_chat_id, text=text
        )

    async def send_triggered_alert(self, text: str, tracked_id: int) -> int:
        """Send a critical snipe alert (pinned) with the triggered keyboard.

        Returns the message id so re-pings can be sent and the message edited
        if the price rebounds before the user replies.
        """
        msg = await self.application.bot.send_message(
            chat_id=self.config.telegram_chat_id,
            text=text,
            reply_markup=build_triggered_keyboard(tracked_id),
        )
        try:
            await self.application.bot.pin_chat_message(
                chat_id=self.config.telegram_chat_id,
                message_id=msg.message_id,
                disable_notification=True,
            )
        except Exception as exc:  # noqa: BLE001 — pin is best-effort
            logger.warning("Épinglage impossible : %s", exc)
        return msg.message_id

    async def send_reping(self, tracked_id: int, n: int, total: int) -> None:
        """Send a re-ping for an unanswered triggered snipe."""
        await self.application.bot.send_message(
            chat_id=self.config.telegram_chat_id,
            text=f"🎯 Rappel {n}/{total} : un prix snipé t'attend, réponds vite !",
            reply_markup=build_triggered_keyboard(tracked_id),
        )

    async def edit_rebounded(self, message_id: int) -> None:
        """Edit a triggered alert when the price climbed back above threshold."""
        try:
            await self.application.bot.edit_message_text(
                chat_id=self.config.telegram_chat_id,
                message_id=message_id,
                text="🙁 Raté : le prix est remonté avant ta réponse. Je continue à viser.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Édition message raté impossible : %s", exc)

    def alerts_paused(self) -> bool:
        return self.db.get_state("alerts_paused", "0") == "1"


def _obs_get(obs: Any, key: str) -> Any:
    """Read ``key`` from a sqlite3.Row or plain mapping, tolerating absence."""
    try:
        return obs[key]
    except (KeyError, IndexError, TypeError):
        try:
            return obs.get(key)  # type: ignore[union-attr]
        except AttributeError:
            return None


def format_triggered_alert(
    depart_date: str,
    return_date: Optional[str],
    price_eur: float,
    threshold_eur: float,
    transfers: Optional[int] = None,
    return_transfers: Optional[int] = None,
    freshness_note: Optional[str] = None,
) -> str:
    """Build the critical (triggered) snipe alert body shown above the buttons.

    The flight line carries the number of stops (escales) when known, phrased by
    the shared :func:`format_stops_roundtrip` helper.
    """
    ret = f"→{return_date}" if return_date else ""
    flight_line = f"💶 {price_eur:.0f} € (seuil ≤ {threshold_eur:.0f} €)"
    stops = format_stops_roundtrip(transfers, return_transfers)
    if stops:
        flight_line += f" · {stops}"
    text = (
        f"🎯 SNIPE DÉCLENCHÉ : {depart_date}{ret}\n"
        f"{flight_line} — prix confirmé en direct.\n"
        "Valide vite avant qu'il ne remonte."
    )
    if freshness_note:
        text += f"\n⚠️ {freshness_note}"
    return text


def format_deal_alert(obs: dict, components_json: str, llm_text: str) -> str:
    """Build the alert message body shown above the inline buttons."""
    route = f"{obs['origin']}→{obs['destination']}"
    ret = f" A/R {obs['depart_date']} → {obs['return_date']}" if obs["return_date"] else f" {obs['depart_date']}"
    header = f"✈️ Deal {route}{ret}\n💶 {obs['price_eur']:.0f} EUR"
    stops = format_stops_roundtrip(
        _obs_get(obs, "transfers"), _obs_get(obs, "return_transfers")
    )
    if stops:
        header += f" · {stops}"
    try:
        comp = json.loads(components_json)
        header += f"  (score {comp.get('composite', '?')})"
    except (json.JSONDecodeError, TypeError):
        pass
    return f"{header}\n\n🤖 {llm_text}"
