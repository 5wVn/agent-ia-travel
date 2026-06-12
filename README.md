# Agent IA Travel

Agent autonome de surveillance des prix de vols **Toulouse ⇄ Paris** (TLS ⇄ ORY/CDG, aller-retour). Il collecte les prix via l'API Amadeus, détecte les bonnes affaires avec un scoring déterministe, rédige une recommandation avec Claude, et demande confirmation via Telegram avant de proposer la réservation.

Tout le trafic réseau est **sortant uniquement** (long polling Telegram, pas de webhook). Le NAS n'expose rien. Voir [PLAN.md](PLAN.md) pour l'architecture complète.

## Architecture en bref

- **Collecte** (toutes les 4 h) : Amadeus Self-Service → SQLite (`price_observations`, append-only).
- **Scoring** : score composite 0–100 (prix 0.45 / horaire 0.25 / confort 0.20 / tendance 0.10). Alerte si score ≥ 80 ou prix < 60 €, dédupliqué par `deal_key`.
- **Analyse** : Claude (`claude-opus-4-8`) rédige la reco — **optionnel** (voir « Mode sans LLM ») ; fallback template sinon.
- **Sniper de prix** : `/snipe` arme un seuil sur une date suivie ; surveillance boostée toutes les 15 min, re-vérification du prix en direct, alerte critique avec re-ping.
- **Telegram** : alertes avec boutons `[✅ Réserver] [⏳ Attendre] [🔕 Ignorer]`, calendrier interactif `/track`, commandes `/snipe` `/untrack` `/status` `/pause` `/resume`.

## Installation sur NAS (Docker Compose)

1. Cloner le dépôt sur le NAS, puis copier la configuration :

   ```sh
   cp .env.example .env
   # éditer .env avec vos clés (voir « Obtention des clés » ci-dessous)
   ```

2. Préparer le dossier de données. Le conteneur tourne en non-root (UID `10001`) ;
   le volume `./data` monté depuis l'hôte garde les permissions de l'hôte, il faut
   donc le rendre inscriptible par cet UID, sinon SQLite ne peut pas créer la base :

   ```sh
   mkdir -p data
   sudo chown -R 10001:10001 data
   ```

3. Lancer :

   ```sh
   docker compose up -d --build
   ```

4. Vérifier l'état :

   ```sh
   docker compose ps        # le healthcheck doit passer "healthy"
   docker compose logs -f   # suivre les logs
   ```

La base SQLite est persistée dans `./data/prices.db` (monté en volume). Pour la sauvegarde, copier ce dossier (Hyper Backup / rsync).

## Obtention des clés

### Amadeus (Self-Service API)

1. Créer un compte sur <https://developers.amadeus.com>.
2. Dans **My Self-Service Workspace**, créer une application.
3. Récupérer **API Key** (→ `AMADEUS_CLIENT_ID`) et **API Secret** (→ `AMADEUS_CLIENT_SECRET`).
4. Le code cible l'environnement de test (`test.api.amadeus.com`). Le tier gratuit suffit largement ; ajustez `AMADEUS_MONTHLY_QUOTA` à votre quota réel — la fréquence de collecte se réduit automatiquement à 80 % du quota.

### Telegram (BotFather)

1. Dans Telegram, ouvrir **@BotFather**, envoyer `/newbot`, suivre les étapes.
2. Récupérer le **token** (→ `TELEGRAM_BOT_TOKEN`).
3. Démarrer une conversation avec votre bot, puis obtenir votre **chat id** :
   - via **@userinfobot** (envoyez-lui un message), ou
   - en appelant `https://api.telegram.org/bot<TOKEN>/getUpdates` après avoir écrit au bot.
4. Renseigner `TELEGRAM_CHAT_ID` — le bot n'accepte que ce chat.

### Anthropic (optionnel)

1. Créer une clé sur <https://console.anthropic.com> (Settings → API Keys).
2. Renseigner `ANTHROPIC_API_KEY`. Coût attendu : quelques centimes/mois (1–3 appels/jour, prompt caching activé).

## Mode sans LLM

`ANTHROPIC_API_KEY` est **facultative**. Si elle est absente ou vide, l'agent
démarre en « mode sans LLM » :

- aucun client Anthropic n'est instancié et **aucun appel réseau n'est tenté** ;
- les recommandations de deal et le digest quotidien sont produits par des
  **messages template** déterministes (prix vs médiane et p10, composantes du
  score, tendance, recommandation) ;
- un **seul** log `Mode sans LLM` est émis au démarrage (pas de warning répété).

Tout le reste — collecte Amadeus, scoring composite, détection de deals, sniper
de prix, calendrier `/track`, alertes Telegram — fonctionne à l'identique. La
clé n'ajoute que la rédaction en langage naturel des recos et du digest.

## Commandes du bot

| Commande | Effet |
|---|---|
| `/track` | Calendrier interactif : choix de la date de départ, fourchette horaire (06h–22h, ou « Peu importe »), date et fourchette de retour, puis confirmation. Insère dans `tracked_dates`. |
| `/snipe` | Liste les dates suivies pour armer un **seuil de prix** (grille 30–120 € par pas de 5, paginée). Le bot surveille alors cette date toutes les 15 min ; sous le seuil et après re-vérification du prix en direct, il envoie une alerte critique avec re-ping. Re-liste aussi les snipes armés pour les désarmer. |
| `/untrack` | Liste les suivis actifs avec un bouton pour en désactiver. |
| `/status` | Suivis actifs + meilleur prix actuel de chacun, et l'état des snipes (seuil, armé/déclenché). |
| `/pause` | Met en pause toutes les alertes (et le digest). |
| `/resume` | Réactive les alertes. |

Boutons d'alerte :

- **✅ Réserver** → enregistre la décision et renvoie le lien de réservation (deep link si disponible, sinon une indication de recherche).
- **⏳ Attendre** → surveillance renforcée de la date (re-alerte si le prix bouge).
- **🔕 Ignorer** → désactive la date (plus d'alertes).

Alerte critique d'un snipe déclenché :

- **🎯 J'achète** → enregistre la décision, désarme le snipe, stoppe les re-pings et renvoie le lien/les infos vol.
- **⏳ Continue à viser** → réarme et stoppe les re-pings.
- **🔕 Désarmer** → arrête le snipe.

## Développement

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest
```

Les tests couvrent le scoring (cas limites), la base (schéma, migration idempotente, dédoublonnage `deal_key`), la normalisation Amadeus (fixture JSON figée), le sniper (armement, gating de proximité, déclenchement/réarmement avec collector mocké, priorité quota), les grilles de boutons (callback_data ≤ 64 octets) et l'analyste en mode sans LLM (aucun appel réseau, client non instancié). Aucun test ne fait d'appel réseau.

## Configuration

Toutes les options passent par variables d'environnement (voir `.env.example`). Aucun secret n'est codé en dur. Les routes, fenêtres, seuils et poids de scoring sont réglables sans toucher au code.
