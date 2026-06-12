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

import anthropic

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
    """Wraps the Anthropic client. Falls back to templates on any failure."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def _call(self, system_prompt: str, user_content: str) -> Optional[str]:
        """Single Messages API call with a cached system prompt.

        Returns the text response, or None if the call failed (the caller then
        uses a template fallback).
        """
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


def _fallback_deal_message(ctx: dict[str, Any]) -> str:
    """Deterministic French recommendation when the LLM is unavailable."""
    route = ctx.get("route", "?")
    price = ctx.get("price_eur", "?")
    median = ctx.get("median_30d")
    lines = [f"Bonne affaire détectée sur {route} : {price} EUR."]
    if median:
        try:
            pct = round((1 - float(price) / float(median)) * 100)
            lines.append(f"Médiane sur 30 jours : {median} EUR ({pct:+d} %).")
        except (TypeError, ValueError, ZeroDivisionError):
            lines.append(f"Médiane sur 30 jours : {median} EUR.")
    lines.append("Reco : si le prix est sous ton seuil habituel, réserver est raisonnable.")
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
            lines.append(
                f"- {deal.get('route', '?')} {deal.get('depart_date', '?')} : "
                f"{deal.get('price_eur', '?')} EUR (score {deal.get('score', '?')})."
            )
    return "\n".join(lines)
