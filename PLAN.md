# Agent IA Travel — Plan d'architecture

**Objectif** : un agent autonome hébergé sur un NAS qui surveille les prix des vols **Toulouse ⇄ Paris** (TLS ⇄ ORY/CDG, aller-retour), détecte les bonnes affaires, et demande confirmation via **Telegram** avant de proposer la réservation.

---

## 1. Décisions structurantes (et pourquoi)

| Décision | Choix | Justification |
|---|---|---|
| Canal de notification | **Telegram** (pas Slack) | Bot API gratuite, *long polling* = uniquement du trafic **sortant** → aucun port à ouvrir sur le NAS, pas de reverse proxy, pas de webhook HTTPS à exposer. Boutons inline natifs pour la confirmation. |
| Source de données vols | **Amadeus Self-Service API** (primaire) | Tier gratuit (~plusieurs centaines d'appels/mois), données GDS fiables, endpoint `flight-offers-search` couvre TLS-ORY/CDG. Fallback possible : Travelpayouts (gratuit, données cache + liens d'affiliation) ou SerpAPI Google Flights (payant, le plus exhaustif). |
| Source of truth | **SQLite** (fichier sur volume NAS) | Une seule table append-only d'observations de prix. Zéro serveur DB à maintenir, backup = copie de fichier, largement suffisant pour quelques milliers de lignes/mois. Tout le reste (baseline, alertes, digests) est **dérivé** de cette table. |
| Détection de deal | **Règles déterministes** (pas le LLM) | Comparer un prix à une médiane glissante est du SQL, pas de l'IA. Le LLM n'intervient que là où il a de la valeur : analyse, rédaction, recommandation, dialogue. → coût LLM quasi nul et comportement prévisible. |
| LLM | **`claude-opus-4-8`** (Anthropic API, SDK Python) | Modèle par défaut recommandé ($5/$25 par MTok). Volume minuscule (1–3 appels/jour, petits contextes) → coût de l'ordre de **quelques centimes/mois**. Option éco si tu veux : `claude-haiku-4-5` ($1/$5) — c'est ton arbitrage, pas une nécessité. |
| Déploiement | **Docker Compose**, 1 conteneur Python | Un seul service modulaire (collector + analyseur + bot) : moins de pièces mobiles sur un NAS. `restart: unless-stopped` + healthcheck = autonomie. |
| Réservation | **Deep link** (pas d'achat automatisé) | Automatiser l'achat (carte bancaire, anti-bot, CGU compagnies) est fragile et risqué. L'agent envoie le lien de réservation pré-rempli après ta confirmation — c'est le bon niveau d'automatisation. |

---

## 2. Architecture

```
┌──────────────────────────── NAS (Docker) ────────────────────────────┐
│                                                                      │
│  ┌─────────────────── conteneur "agent-travel" ───────────────────┐ │
│  │                                                                 │ │
│  │  Scheduler (APScheduler)                                        │ │
│  │   ├─ toutes les 4h ──▶ Collector ──▶ Amadeus API (sortant)      │ │
│  │   │                       │ normalise + insère                  │ │
│  │   │                       ▼                                     │ │
│  │   │              ┌─────────────────┐                            │ │
│  │   │              │ SQLite           │  ◀── SOURCE OF TRUTH      │ │
│  │   │              │ /data/prices.db  │      (append-only)        │ │
│  │   │              └─────────────────┘                            │ │
│  │   │                       │                                     │ │
│  │   ├─ après collecte ──▶ Rule Engine (SQL : médiane glissante,   │ │
│  │   │                     seuils) ── deal détecté ? ──┐           │ │
│  │   │                                                 ▼           │ │
│  │   │                              Agent LLM (claude-opus-4-8)    │ │
│  │   │                              analyse + rédige la reco       │ │
│  │   │                                                 │           │ │
│  │   └─ 1×/jour 8h ──▶ Digest quotidien ───────────────┤           │ │
│  │                                                     ▼           │ │
│  │                     Telegram Bot (long polling, sortant only)   │ │
│  │                      ├─ alerte + boutons [✅ Réserver]           │ │
│  │                      │             [⏳ Attendre] [🔕 Ignorer]    │ │
│  │                      └─ réponse user ──▶ log décision + deep    │ │
│  │                                          link de réservation    │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│   volumes : /data (db + logs)   secrets : .env                       │
└──────────────────────────────────────────────────────────────────────┘
```

**Point clé réseau** : tout le trafic est sortant (API Amadeus, API Anthropic, API Telegram). Le NAS n'expose **rien**.

---

## 3. Source of truth — schéma SQLite

```sql
-- Observations brutes : on n'écrase JAMAIS, on ajoute. Tout est rejouable.
CREATE TABLE price_observations (
    id            INTEGER PRIMARY KEY,
    observed_at   TEXT NOT NULL,          -- ISO 8601, moment de la collecte
    origin        TEXT NOT NULL,          -- 'TLS' ou 'ORY'/'CDG'
    destination   TEXT NOT NULL,
    depart_date   TEXT NOT NULL,
    return_date   TEXT,                   -- NULL = aller simple
    carrier       TEXT NOT NULL,
    price_eur     REAL NOT NULL,
    deep_link     TEXT,
    raw_offer     TEXT,                   -- JSON brut de l'API (audit/replay)
    source        TEXT NOT NULL DEFAULT 'amadeus'
);
CREATE INDEX idx_obs_route_date ON price_observations(origin, destination, depart_date, observed_at);

-- Décisions utilisateur (réponses Telegram) : la mémoire de l'agent
CREATE TABLE decisions (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    observation_id INTEGER REFERENCES price_observations(id),
    action        TEXT NOT NULL,          -- 'book' | 'wait' | 'ignore'
    note          TEXT
);

-- Alertes envoyées (anti-spam : ne pas re-notifier le même deal)
CREATE TABLE alerts_sent (
    id             INTEGER PRIMARY KEY,
    sent_at        TEXT NOT NULL,
    observation_id INTEGER REFERENCES price_observations(id),
    deal_key       TEXT NOT NULL UNIQUE   -- hash(route+dates+carrier+tranche de prix)
);
```

Tout dérive de `price_observations` : la baseline (médiane 30 jours par route/date), les seuils, les digests, les stats. Si on change la logique de détection demain, on ré-exécute sur l'historique — rien n'est perdu.

---

## 4. Pipeline détaillé

### Étape 1 — Collecte (toutes les 4h, configurable)
- Fenêtre de recherche configurable : ex. les **8 prochains week-ends** (vendredi→dimanche) + dates fixes que tu ajoutes via Telegram (`/track 2026-07-14 2026-07-18`).
- 1 appel Amadeus par couple (date_aller, date_retour) × 2 aéroports parisiens → budget quota maîtrisé (~16 combinaisons × 6 collectes/jour = vérifier le quota du tier gratuit, sinon passer à 2 collectes/jour).
- Normalisation → insertion en base. Erreurs API = retry exponentiel (3 tentatives), puis log et on attend le prochain tick — jamais de crash.

### Étape 2 — Détection (SQL pur, après chaque collecte)
Un deal = `price_eur < seuil_absolu` (ex. 60 € l'A/R) **OU** `price_eur < 0.75 × médiane_glissante_30j(route, mois)`. Dédoublonnage via `alerts_sent.deal_key`.

### Étape 3 — Analyse LLM (uniquement si deal, ou pour le digest)
L'agent reçoit : l'offre, l'historique de prix de la route (résumé compact), tes décisions passées. Il produit un message court : pourquoi c'est une bonne affaire, tendance (prix qui monte/descend), recommandation (réserver maintenant vs attendre).

```python
client.messages.create(
    model="claude-opus-4-8",
    max_tokens=1024,
    system=SYSTEM_PROMPT,            # stable → prompt caching
    messages=[{"role": "user", "content": deal_context}],
)
```

### Étape 4 — Notification + confirmation Telegram
```
✈️ Deal TLS→ORY  A/R 12–14 sept
💶 54 € (médiane 30j : 89 €, -39 %)
📈 Tendance : stable depuis 5 jours, dispo faible sur ce vol
🤖 Reco : réserver — sous la barre des 60 € c'est rare sur septembre

[✅ Réserver]  [⏳ Attendre]  [🔕 Ignorer cette date]
```
- **✅ Réserver** → l'agent répond avec le deep link de réservation + enregistre la décision.
- **⏳ Attendre** → surveillance renforcée de cette date ; re-alerte si le prix baisse encore ou remonte brutalement (signal "dernière chance").
- **🔕 Ignorer** → blacklist de la date, plus d'alertes dessus.

### Étape 5 — Digest quotidien (8h)
Un message Telegram : min/max/médiane par route, meilleures dates du moment, deals en attente. Rédigé par le LLM (1 appel/jour).

---

## 5. Stack technique

| Composant | Choix |
|---|---|
| Langage | Python 3.12 |
| Scheduler | APScheduler (in-process, pas de cron système à gérer) |
| Bot | `python-telegram-bot` (long polling) |
| HTTP | `httpx` + retry/backoff |
| DB | `sqlite3` stdlib (WAL mode) |
| LLM | SDK `anthropic`, modèle `claude-opus-4-8`, prompt caching sur le system prompt |
| Conteneur | image `python:3.12-slim`, `restart: unless-stopped`, healthcheck |
| Secrets | `.env` (jamais commité) : `AMADEUS_CLIENT_ID/SECRET`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| Backup | copie quotidienne de `prices.db` vers un second dossier du NAS (tâche Hyper Backup/rsync) |

### docker-compose.yml (squelette)
```yaml
services:
  agent-travel:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/data
    healthcheck:
      test: ["CMD", "python", "-m", "app.healthcheck"]
      interval: 5m
      timeout: 10s
      retries: 3
```

### Arborescence cible
```
agent-ia-travel/
├── app/
│   ├── main.py            # boot : scheduler + bot
│   ├── collector.py       # Amadeus → SQLite
│   ├── rules.py           # détection de deals (SQL)
│   ├── analyst.py         # appels Claude (analyse, digest)
│   ├── bot.py             # Telegram : alertes, boutons, commandes
│   ├── db.py              # schéma + accès SQLite
│   └── config.py          # routes, fenêtres, seuils (YAML/env)
├── data/                  # prices.db (gitignoré)
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── PLAN.md
```

---

## 6. Coûts estimés

| Poste | Coût |
|---|---|
| Amadeus Self-Service | 0 € (tier gratuit, ajuster la fréquence au quota) |
| Telegram Bot API | 0 € |
| Anthropic (`claude-opus-4-8`) | ~2–4 appels/jour × ~2K tokens in / 500 out ≈ **< 1 €/mois** (encore moins avec prompt caching ; ÷5 si option `claude-haiku-4-5`) |
| Hébergement | 0 € (NAS existant) |

---

## 7. Roadmap

1. **Phase 1 — Boucle minimale** : collector Amadeus → SQLite → alerte Telegram sur seuil absolu. *Déjà utile sans LLM.*
2. **Phase 2 — Intelligence** : médiane glissante, anti-spam, analyse + reco Claude, boutons de confirmation, table `decisions`.
3. **Phase 3 — Confort** : digest quotidien, commandes Telegram (`/track`, `/status`, `/pause`), mémoire des préférences (heures de vol préférées, compagnies à éviter).
4. **Phase 4 — Optionnel** : 2e source de prix (Travelpayouts) pour croiser, comparaison TGV (API SNCF) sur le même trajet, petit dashboard (Grafana/Streamlit) branché sur SQLite.

---

## 8. Risques & parades

| Risque | Parade |
|---|---|
| Quota Amadeus dépassé | Compteur d'appels en base, réduction auto de la fréquence à 80 % du quota |
| API down / réseau NAS | Retry + backoff, l'agent saute le tick sans crasher, alerte Telegram si > 24h sans collecte |
| Spam d'alertes | `deal_key` unique + cooldown par route |
| Clé API qui fuit | `.env` hors git, permissions fichier, token Telegram régénérable en 10 s |
| Corruption SQLite | Mode WAL + backup quotidien |
