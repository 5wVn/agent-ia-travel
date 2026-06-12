"""LLM analysis via the Anthropic SDK (claude-opus-4-8).

The system prompt is stable and marked with ``cache_control: {"type":
"ephemeral"}`` so it is served from the prompt cache on repeated calls. Two
entry points: :func:`analyze_deal` (a short recommendation for one deal) and
:func:`daily_digest` (a morning summary). Both degrade gracefully: if the API
fails, a deterministic French template is returned so the alert still goes out.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .config import Config

logger = logging.getLogger(__name__)

# Stable across every call -> cacheable prefix.
SYSTEM_PROMPT_DEAL = (
    "Tu es un assistant de veille tarifaire pour des vols Toulouse <-> Paris. "
    "On te fournit une offre détectée comme bonne affaire et le contexte de "
    "prix (médiane glissante, percentiles, tendance des dernières collectes). "
    "Réponds en français, en 3 à 5 lignes maximum : (1) pourquoi c'est une "
    "bonne affaire, (2) la tendance du prix (en hausse / stable / en baisse), "
    "(3) une recommandation claire : réserver maintenant ou attendre. Sois "
    "factuel et concis, pas de listes à puces ni de markdown."
)

SYSTEM_PROMPT_DIGEST = (
    "Tu es un assistant de veille tarifaire pour des vols Toulouse <-> Paris. "
    "On te fournit des statistiques de prix par route (min / max / médiane) et "
    "le top 5 des meilleures offres du moment. Rédige en français un résumé "
    "matinal court et lisible : une phrase d'accroche, puis l'essentiel par "
    "route, puis les meilleures dates à surveiller. Pas de markdown lourd."
)


class Analyst:
    """Wraps the Anthropic client. Falls back to templates on any failure.

    "Mode sans LLM" : when ``config.llm_enabled()`` is False (no
    ``ANTHROPIC_API_KEY``), the Anthropic client is never instantiated and no
    network call is ever attempted — :meth:`analyze_deal` / :meth:`daily_digest`
    return the deterministic French templates directly. A single info log is
    emitted at construction; there is no repeated warning.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: Optional[Any] = None
        if config.llm_enabled():
            # Imported lazily so the package boots even if `anthropic` is absent
            # and no key is configured.
            import anthropic

            self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)
            logger.info("LLM activé (modèle %s).", config.llm_model)
        else:
            logger.info("Mode sans LLM : messages template (aucune clé Anthropic).")

    def _call(self, system_prompt: str, user_content: str) -> Optional[str]:
        """Single Messages API call with a cached system prompt.

        Returns the text response, or None if the call failed (the caller then
        uses a template fallback). When the LLM is disabled the client is None
        and we return None immediately — no network call is ever attempted.
        """
        if self._client is None:
            return None
        try:
            response = self._client.messages.create(
                model=self.config.llm_model,
                max_tokens=self.config.llm_max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
            parts = [b.text for b in response.content if b.type == "text"]
            text = "".join(parts).strip()
            return text or None
        except Exception as exc:  # noqa: BLE001 — never crash; fall back
            logger.error("Appel LLM échoué : %s", exc)
            return None

    def analyze_deal(self, deal_context: dict[str, Any]) -> str:
        """Short recommendation for one deal. Always returns French text."""
        user_content = json.dumps(deal_context, ensure_ascii=False, indent=2)
        text = self._call(SYSTEM_PROMPT_DEAL, user_content)
        if text:
            return text
        return _fallback_deal_message(deal_context)

    def daily_digest(self, stats_context: dict[str, Any]) -> str:
        """Morning digest. Always returns French text."""
        user_content = json.dumps(stats_context, ensure_ascii=False, indent=2)
        text = self._call(SYSTEM_PROMPT_DIGEST, user_content)
        if text:
            return text
        return _fallback_digest_message(stats_context)


def _trend_label(recent: Any) -> Optional[str]:
    """Describe the price trend from recent prices (most recent first)."""
    try:
        prices = [float(p) for p in recent]
    except (TypeError, ValueError):
        return None
    if len(prices) < 2:
        return None
    # recent[0] is the latest; compare it to the oldest in the window.
    latest, oldest = prices[0], prices[-1]
    if latest < oldest * 0.97:
        return "en baisse"
    if latest > oldest * 1.03:
        return "en hausse (signal dernière chance)"
    return "stable"


def _fallback_deal_message(ctx: dict[str, Any]) -> str:
    """Deterministic French recommendation when the LLM is unavailable.

    Reproduces the substance of the LLM output without any API call: prix vs
    médiane et p10, composantes du score, tendance, et une recommandation.
    """
    route = ctx.get("route", "?")
    price = ctx.get("price_eur", "?")
    median = ctx.get("median_30d")
    p10 = ctx.get("p10_30d")
    score = ctx.get("score")
    components = ctx.get("components") or {}
    stops = ctx.get("stops")
    head = f"Bonne affaire détectée sur {route} : {price} EUR"
    if stops:
        head += f" ({stops})"
    lines = [head + "."]

    if median:
        try:
            pct = round((1 - float(price) / float(median)) * 100)
            lines.append(f"Médiane sur 30 jours : {median} EUR ({pct:+d} %).")
        except (TypeError, ValueError, ZeroDivisionError):
            lines.append(f"Médiane sur 30 jours : {median} EUR.")
    if p10 is not None:
        lines.append(f"Plancher habituel (p10) : {p10} EUR.")

    if score is not None:
        detail = ", ".join(
            f"{k} {round(float(v))}"
            for k, v in components.items()
            if k != "composite" and isinstance(v, (int, float))
        )
        score_line = f"Score : {score}/100"
        if detail:
            score_line += f" ({detail})"
        lines.append(score_line + ".")

    trend = _trend_label(ctx.get("recent_prices"))
    if trend is not None:
        lines.append(f"Tendance : {trend}.")

    reco = "Reco : "
    if trend and "hausse" in trend:
        reco += "le prix remonte, réserver maintenant est prudent."
    elif median and price != "?" and float(price) <= float(median) * 0.8:
        reco += "nettement sous la médiane, réserver est raisonnable."
    else:
        reco += "si le prix est sous ton seuil habituel, réserver est raisonnable."
    lines.append(reco)
    return " ".join(lines)


def _fallback_digest_message(ctx: dict[str, Any]) -> str:
    """Deterministic French digest when the LLM is unavailable."""
    lines = ["Résumé quotidien des prix (mode dégradé, sans IA)."]
    for route in ctx.get("routes", []):
        label = route.get("route", "?")
        lines.append(
            f"{label} : min {route.get('min', '?')} EUR, "
            f"médiane {route.get('median', '?')} EUR, "
            f"max {route.get('max', '?')} EUR."
        )
    top = ctx.get("top_deals", [])
    if top:
        lines.append("Top offres :")
        for deal in top[:5]:
            stops = deal.get("stops")
            stops_txt = f", {stops}" if stops else ""
            lines.append(
                f"- {deal.get('route', '?')} {deal.get('depart_date', '?')} : "
                f"{deal.get('price_eur', '?')} EUR{stops_txt} "
                f"(score {deal.get('score', '?')})."
            )
    return "\n".join(lines)
