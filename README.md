# Agent IA Travel

Agent autonome de surveillance des prix de vols **Toulouse ⇄ Paris** (TLS ⇄ ORY/CDG, aller-retour). Il collecte les prix via une **abstraction multi-provider** (Travelpayouts/Aviasales par défaut, Amadeus en option), détecte les bonnes affaires avec un scoring déterministe, rédige une recommandation avec Claude, et demande confirmation via Telegram avant de proposer la réservation.

Tout le trafic **internet** est sortant uniquement (long polling Telegram, pas de webhook) : rien n'est exposé sur internet. Seul le **dashboard web** écoute sur le port 8080, **en LAN uniquement** (voir la section dédiée). Voir [PLAN.md](PLAN.md) pour l'architecture complète.

> ⚠️ **Migration de source de données.** Amadeus décommissionne son portail
> **Self-Service le 17/07/2026** (clés désactivées, inscriptions gelées avant).
> La collecte passe donc derrière une abstraction *provider* et le défaut
> devient **Travelpayouts/Aviasales Data API** (gratuit pour un particulier,
> deep links de réservation inclus). Amadeus reste sélectionnable
> (`FLIGHT_PROVIDER=amadeus`) **jusqu'à l'arrêt**. Changer de source = une
> variable d'env, le reste du pipeline ne bouge pas.

## Choix du provider de données vols

Le provider est choisi par `FLIGHT_PROVIDER` (`travelpayouts` par défaut, `amadeus` accepté).

| Provider | Fraîcheur | Deep links | Coût | Statut |
|---|---|---|---|---|
| **Travelpayouts/Aviasales** (défaut) | Cache alimenté par les recherches réelles Aviasales (fraîcheur en **heures**) ; l'alerte critique affiche l'âge du prix (« prix observé il y a ~2 h, vérifie au clic »). | **Oui** — lien Aviasales pré-rempli, avec marker affilié optionnel. | Gratuit (compte affilié). | Actif, recommandé. |
| **Amadeus Self-Service** | **Temps réel** (re-vérification via Flight Offers Price). | Non (Self-Service ne renvoie pas d'URL de réservation). | Gratuit (tier de test). | ⚠️ **Mort au 17/07/2026.** |

L'abstraction permet de brancher un 3ᵉ provider (ex. SerpAPI Google Flights, payant, temps réel) en une classe.

## Architecture en bref

- **Collecte** (toutes les 4 h) : provider de vols (Travelpayouts par défaut) → SQLite (`price_observations`, append-only, champ `source`).
- **Scoring** : score composite 0–100 (prix 0.45 / horaire 0.25 / confort 0.20 / tendance 0.10). Alerte si score ≥ 80 ou prix < 60 €, dédupliqué par `deal_key`.
- **Analyse** : Claude (`claude-opus-4-8`) rédige la reco — **optionnel** (voir « Mode sans LLM ») ; fallback template sinon.
- **Sniper de prix** : `/snipe` arme un seuil sur une date suivie ; surveillance boostée toutes les 15 min, re-vérification du prix (en direct pour Amadeus, prix le plus frais re-fetché pour Travelpayouts), alerte critique avec re-ping et, pour un provider en cache, l'âge du prix.
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

### Travelpayouts / Aviasales (provider par défaut)

1. Inscription **gratuite** sur <https://www.travelpayouts.com> (programme d'affiliation).
2. Dans l'espace affilié, ouvrir la section **développeurs / API** (« Travel insights with Travelpayouts Data API ») pour récupérer votre **token d'accès** → `TRAVELPAYOUTS_TOKEN`. Le token est passé à l'API dans l'en-tête `X-Access-Token`.
3. Optionnel : votre **marker** d'affiliation → `TRAVELPAYOUTS_MARKER`. S'il est renseigné, il est ajouté aux deep links de réservation (`...&marker=<id>`) pour vous attribuer les clics ; sans lui, les liens fonctionnent quand même.
4. La Data API (`api.travelpayouts.com/aviasales/v3/prices_for_dates`) renvoie des prix en cache (alimentés par les recherches réelles Aviasales) avec un lien de réservation. Quota généreux ; ajustez `TRAVELPAYOUTS_MONTHLY_QUOTA` si besoin — la collecte se réduit automatiquement à 80 % du quota.

### Amadeus (Self-Service API) — optionnel, ⚠️ jusqu'au 17/07/2026

Requis **uniquement** si `FLIGHT_PROVIDER=amadeus`. Sinon, laissez ces variables vides.

1. Créer un compte sur <https://developers.amadeus.com>.
2. Dans **My Self-Service Workspace**, créer une application — guide officiel : [Quick start](https://developers.amadeus.com/self-service/apis-docs/guides/developer-guides/quick-start/) et [obtenir ses clés API](https://developers.amadeus.com/self-service/apis-docs/guides/developer-guides/API-Keys/). (Les anciennes URL `developers.amadeus.com/get-started/...` ne fonctionnent plus.)
3. Récupérer **API Key** (→ `AMADEUS_CLIENT_ID`) et **API Secret** (→ `AMADEUS_CLIENT_SECRET`).
4. Le code cible l'environnement de test (`test.api.amadeus.com`). Le tier gratuit suffit largement ; ajustez `AMADEUS_MONTHLY_QUOTA` à votre quota réel — la fréquence de collecte se réduit automatiquement à 80 % du quota.
5. ⚠️ Le portail Self-Service ferme le **17/07/2026** : prévoyez de basculer sur `travelpayouts` avant cette date.

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

Tout le reste — collecte (provider Travelpayouts/Amadeus), scoring composite,
détection de deals, sniper de prix, calendrier `/track`, alertes Telegram —
fonctionne à l'identique. La clé n'ajoute que la rédaction en langage naturel
des recos et du digest.

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

## Dashboard web

Un dashboard web tourne dans **le même conteneur** que le bot (même boucle
asyncio, même base SQLite et même verrou d'écriture) : Telegram et le dashboard
écrivent dans la même source de vérité.

- **URL** : `http://IP_DU_NAS:8080` (port configurable via `DASHBOARD_PORT`).
- **Activation** : définissez `DASHBOARD_PASSWORD` dans `.env`. S'il est absent,
  le dashboard est **désactivé** (un seul log info au démarrage) et le port ne
  sert rien.
- **Connexion** : formulaire de login, session par cookie signé (HMAC). Toutes
  les pages sont protégées sauf `/login` et `/static`.

⚠️ **LAN uniquement.** Ne faites **pas** de redirection de port internet vers le
8080. Pour l'accès distant, passez par le **VPN/Tailscale** du NAS.

Pages :

| Page | Contenu |
|---|---|
| **Vue d'ensemble** (`/`) | Par route active : meilleur prix, médiane 30 j, graphique d'historique (Chart.js, données de `price_observations`), alertes récentes. |
| **Destinations** (`/routes`) | Liste des routes, activation/désactivation (suppression logique, jamais de DELETE), ajout origin/destination (codes IATA `[A-Z]{3}`, normalisés en majuscules, doublon refusé). |
| **Dates** (`/dates`) | Liste des dates suivies (route, fourchettes horaires, état snipe), ajout avec sélecteurs de date aller/retour, fourchettes horaires et choix de route ; désactivation ; armement/désarmement de snipe avec seuil. |
| **Statut** (`/status`) | Provider actif, quota consommé/restant, dernière collecte, mode LLM, routes actives, snipes armés. |

Les destinations et les dates sont des **données** : la liste de routes vient de
la table `routes` (semée au premier démarrage depuis la config, dans les deux
sens TLS⇄ORY/CDG), plus la config de `.env`. Front 100 % server-rendered
(Jinja2 + htmx + Chart.js **vendorisés** dans `app/static`, zéro CDN, zéro Node).

## Développement

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest
```

Les tests couvrent le scoring (cas limites), la base (schéma, migration idempotente, dédoublonnage `deal_key`), la normalisation des providers Amadeus **et** Travelpayouts (fixtures JSON figées, deep_link/marker, `verify_price`, `freshness_note`), la sélection du provider par `FLIGHT_PROVIDER` et l'erreur claire si le token manque, le sniper (armement, gating de proximité, déclenchement/réarmement avec provider mocké, priorité quota, note de fraîcheur), les grilles de boutons (callback_data ≤ 64 octets) et l'analyste en mode sans LLM (aucun appel réseau, client non instancié). Aucun test ne fait d'appel réseau.

## Configuration

Toutes les options passent par variables d'environnement (voir `.env.example`). Aucun secret n'est codé en dur. Les routes, fenêtres, seuils et poids de scoring sont réglables sans toucher au code.
