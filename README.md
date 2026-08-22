# 🏠 SeLoger Tracker

Plateforme web pour suivre les annonces immobilières SeLoger. Le scraping se fait côté serveur (via les API SeLoger), qui gère le parsing, le stockage, la déduplication et les notifications push via [ntfy.sh](https://ntfy.sh).

## Fonctionnalités

- **Multi-utilisateur** : chaque utilisateur a son propre espace et ses recherches
- **Scraping automatique** : un scheduler interne relance chaque recherche active selon son intervalle configuré, protégé par un verrou Postgres contre les exécutions dupliquées
- **API REST** : gérer utilisateurs, recherches et annonces en JSON
- **Notifications push** : alertes instantanées via ntfy pour chaque nouvelle annonce, avec retry automatique en cas d'échec
- **Frontend web** : dashboard, gestion des recherches, consultation des annonces
- **Panel admin** : supervision des utilisateurs/recherches/annonces, console SQL en lecture seule, gestion des logs
- **Déduplication intelligente** : les annonces sont stockées une seule fois, même entre utilisateurs

## Démarrage rapide

### Configuration

```bash
cp .env.example .env
```

Puis renseigner dans `.env` :

| Variable | Requis | Description |
|----------|--------|--------------|
| `DATABASE_URL` | oui | URL de connexion PostgreSQL |
| `SECRET_KEY` | oui | Clé de signature des sessions Flask — l'app refuse de démarrer sans elle (`python -c "import secrets; print(secrets.token_hex(32))"`) |
| `ADMIN_USERNAME` | oui, pour `/admin` | Doit correspondre au username d'un compte existant ; sans cette variable, l'accès admin est refusé (fail-closed) |
| `PORT` | non | Port d'écoute (défaut `10000`) |

### Installation locale

```bash
pip install -r requirements.txt
python main.py
```

Le serveur démarre sur `http://localhost:10000`.

### Déploiement Render

1. Créer un **Web Service** sur Render
2. Utiliser **Docker** comme Runtime
3. Renseigner `DATABASE_URL`, `SECRET_KEY` et `ADMIN_USERNAME` dans les variables d'environnement
4. Le Dockerfile gère tout automatiquement

Le conteneur tourne avec `gunicorn --workers 1` : le scheduler et la déduplication des scrapes en mémoire ne sont pas partagés entre process, ce nombre de workers ne doit pas être augmenté sans revoir `core/scrape_control.py`.

### Migrations

L'app **ne modifie jamais le schéma automatiquement au démarrage** (`main.py` vérifie juste que les tables existent, et refuse de démarrer sinon). Après tout déploiement qui touche au schéma (nouvelle colonne, nouvelle table...), il faut lancer manuellement :

```bash
python -m scripts.migrate
```

Chaque instruction du script est idempotente (`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`) — sans risque de la relancer plusieurs fois, y compris sur une base déjà à jour. Sur Render, ouvrir un shell sur le service et exécuter la même commande.

## Usage

### 1. Créer un compte

Allez sur `http://votre-url/login` et entrez un nom d'utilisateur.

### 2. Créer une recherche

Sur la page **Recherches**, créez une recherche avec :
- **Label** : un nom pour identifier la recherche (ex: "Paris 13e T2")
- **Topic ntfy** : le topic où envoyer les notifications (ex: "mes-alertes-immo")
- **Critères** : zones, budget, surface, type de bien, etc.

### 3. Scraping automatique

Un scheduler interne vérifie toutes les 30 secondes les recherches actives et relance celles dont l'intervalle configuré est dépassé. Le scraping peut aussi être déclenché manuellement via l'API ou l'interface web.

### 4. Consulter les annonces

Sur la page **Annonces**, consultez toutes les annonces trouvées avec prix, surface, localisation, photos et liens directs vers SeLoger.

### 5. Administration

Si votre compte correspond à `ADMIN_USERNAME`, la page `/admin` donne accès au dashboard global, à la gestion des utilisateurs/recherches/annonces, à une console SQL (lecture seule — toute tentative d'écriture est rejetée par Postgres) et à la purge des logs.

## API Endpoints

Auth via header : `X-API-Token: <token>`

### Utilisateurs

| Endpoint | Méthode | Auth | Description |
|----------|---------|------|-------------|
| `/api/users` | POST | — | Créer un utilisateur |
| `/api/users/login` | POST | — | Se connecter (ne renvoie pas le token) |
| `/api/sources` | GET | — | Lister les sources disponibles et leurs capacités |
| `/api/locations?q=` | GET | — | Autocomplete de ville (ville, code postal, code INSEE) |
| `/api/stats` | GET | token | Statistiques du compte |

### Recherches

| Endpoint | Méthode | Auth | Description |
|----------|---------|------|-------------|
| `/api/searches` | POST | token | Créer une recherche |
| `/api/searches` | GET | token | Lister ses recherches |
| `/api/searches/<id>` | DELETE | token | Supprimer une recherche |
| `/api/searches/<id>/urls` | GET | token | URL de recherche reconstruite pour la source |
| `/api/searches/<id>/criteria` | PUT | token | Modifier les critères / l'intervalle |
| `/api/searches/<id>/toggle-active` | POST | token | Activer/désactiver une recherche |
| `/api/searches/<id>/blacklist-mode` | PUT | token | Mode de filtrage des agences (`exclude` / `no_notify`) |
| `/api/searches/<id>/blacklist-agencies` | PUT | token | Liste des agences filtrées |

### Scraping & annonces

| Endpoint | Méthode | Auth | Description |
|----------|---------|------|-------------|
| `/api/scrape/<search_id>` | POST | token | Déclencher un scraping manuel |
| `/api/listings/<search_id>` | GET | token | Consulter les annonces (filtres, pagination, tri) |
| `/api/cleanup` | POST | token | Supprimer les annonces plus vieilles que N jours |

### Logs de scrape

| Endpoint | Méthode | Auth | Description |
|----------|---------|------|-------------|
| `/api/searches/<id>/logs/export` | GET | token | Exporter l'historique des scrapes en `.zip` |
| `/api/searches/<id>/logs/import` | POST | token | Réimporter un historique exporté |

## Tests

La suite est divisée en trois étages, chacun avec son marqueur (appliqué
automatiquement selon le répertoire) :

| Répertoire | Marqueur | Ce qu'il exerce | Dépendances |
|------------|----------|-----------------|-------------|
| `tests/unit/` | `unit` | logique pure ou doublée (critères, parsers, scraper, services, constructeurs SQL) | aucune |
| `tests/functional/` | `functional` | l'app Flask **réelle** (`create_app()`) avec `Storage` doublé : routes, autorisation, CSRF | aucune |
| `tests/integration/` | `integration` | le SQL réel : migrations DDL, repositories, pool, verrou du scheduler | Postgres jetable |

Le socle (`tests/conftest.py`) installe des garde-fous *autouse*, chacun né d'un
incident réel : les répertoires de logs sont redirigés vers `tmp_path` (le
pipeline y **supprime** des fichiers), le transport HTTP réel et `psycopg2`
lèvent hors tests marqués, `time.sleep` est neutralisé et ses durées
enregistrées (fixture `slept`, qui permet d'asserter le throttling), et les
caches process (géo, registre de parsers, pools, proxies) sont réinitialisés
entre chaque test. Les fixtures partagées sont dans `tests/functional/conftest.py`,
les constructeurs d'objets dans `tests/helpers/factories.py`, les doubles dans
`tests/helpers/fakes.py`.

- **Tests unitaires et fonctionnels** (aucune dépendance externe) :
  ```bash
  pip install -r requirements-dev.txt
  pytest tests/ -m "not integration"
  ```
- **Tests d'intégration** (`tests/integration/`, vrai Postgres) : sautés automatiquement si **`TEST_DATABASE_URL`** n'est pas défini. Pour les exécuter réellement :
  ```bash
  docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
  ```
  Ce compose démarre un Postgres jetable et lance toute la suite (unitaires + intégration) dedans.

  Ou contre un Postgres jetable local :
  ```bash
  docker run -d --rm --name appart-test-pg -e POSTGRES_USER=testuser \
    -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=testdb -p 55432:5432 postgres:16-alpine
  TEST_DATABASE_URL=postgresql://testuser:testpass@localhost:55432/testdb pytest tests/
  ```

> ⚠️ **Jamais `DATABASE_URL` pour les tests.** Les tests d'intégration font
> `TRUNCATE ... CASCADE` sur toutes les tables avant chaque test. Ils lisent donc
> exclusivement `TEST_DATABASE_URL`, refusent tout host non local, et
> `tests/conftest.py` purge `DATABASE_URL` de l'environnement pendant tout le run
> (cette variable pointe la production et peut arriver via un `load_dotenv()` en
> effet de bord d'import). Un run sain sans `TEST_DATABASE_URL` affiche des
> `skipped` : si les tests d'intégration ne sont **pas** sautés alors que vous
> n'avez pas défini `TEST_DATABASE_URL`, arrêtez tout — ils tapent une vraie base.

## Critères unifiés

L'utilisateur définit ses critères **une seule fois**, dans un vocabulaire
canonique qui n'appartient à aucune source ; c'est chaque parser qui les traduit
ensuite au format de la sienne. Le front, la base et le pipeline ne connaissent
que ce vocabulaire — voir `core/criteria.py`.

| Critère | Valeurs |
|---------|---------|
| `locations` | une liste de périmètres, chacun portant son niveau — voir ci-dessous |
| `transaction` | `rent` \| `buy` |
| `propertyTypes` | `apartment`, `house`, `parking`, `land` |
| `priceMin` / `priceMax` | entiers, en euros |
| `surfaceMin` / `surfaceMax` | entiers, en m² |
| `rooms` / `bedrooms` | `[int]` — `5` signifie « 5 et plus » |
| `sourceOverrides` | `{"<source>": {...}}` — la seule échappatoire : ce que l'utilisateur a saisi à la main pour une source précise (le Place ID SeLoger ou le(s) zoneId bienici, en repli) |

Les recherches créées avant l'unification **ne sont pas migrées** : elles sont
normalisées à la lecture (`SearchRepository._load_criteria`), donc l'ancien
vocabulaire SeLoger (`distributionTypes`, `estateTypes`, `placeIds`, `spaceMin`)
reste compris partout, y compris via l'API.

### Périmètres de recherche

Une recherche ne se limite pas à un code postal. L'autocomplete
(`GET /api/locations?q=`) propose quatre niveaux, et chaque entrée de
`locations` porte le sien dans `kind` :

| `kind` | Ce que ça couvre | Champs | Exemple |
|--------|------------------|--------|---------|
| `region` | une région entière | `name`, `code`, `departments[]` | « Île-de-France » → 8 départements |
| `department` | un département | `name`, `code` | « Gironde » → 534 communes |
| `whole_city` | toute une commune | `city`, `postalCodes[]`, `inseeCode` | « Paris » → ses 20 arrondissements |
| `city` | un seul code postal | `city`, `postalCode`, `inseeCode` | « Paris 15e » |

Une entrée sans `kind` vaut `city` — le format d'avant, donc les recherches
existantes continuent de fonctionner.

Chaque source couvre n'importe lequel de ces niveaux **en une seule requête**,
avec son propre identifiant : `filter[departments][]` pour Laforêt, un placeId
`AD04`/`AD06`/`AD08` pour SeLoger, un ou plusieurs zoneId pour bienici (une
région s'y obtient en combinant les zoneIds de ses départements, faute d'un
identifiant région natif côté bienici). Un périmètre n'est donc jamais
développé en liste de communes — ce qui serait de toute façon impossible,
Laforêt plafonnant vers 100 communes par requête (HTTP 414) et SeLoger vers 50
(HTTP 403), quand une région en compte plus de mille.

Le contrôle que les annonces sont bien dans le périmètre est fait côté serveur
par `core.criteria.matches_locations` (les sources élargissent parfois d'elles-mêmes :
Laforêt inclut la métropole autour d'une commune).

### Ajouter une source

1. Créer `parsers/ma_source.py`, hériter de `BaseParser`, définir `SOURCE_ID` et `SOURCE_NAME`
2. Restreindre `SUPPORTED_TRANSACTIONS` / `SUPPORTED_PROPERTY_TYPES` **seulement** si la source ne couvre pas tout — c'est ce qui permet de prévenir l'utilisateur avant un scrape au lieu d'échouer en cours de route
3. Implémenter `to_native(criteria)` (traduction du canonique) et `scrape(criteria)`
4. Importer le module dans `parsers/__init__.py`

Aucune modification du front, du schéma stocké ni de l'API n'est nécessaire : la
source apparaît dans le formulaire, et ses capacités y sont affichées
automatiquement à partir de ce qu'elle déclare.

## Structure

```
├── main.py          # App Flask (API + Frontend) + scheduler
├── storage.py       # Connexions PostgreSQL + accès aux repositories
├── notifier.py      # Notifications ntfy
├── repositories/    # CRUD par domaine (users, searches, listings, ...)
├── models/          # Dataclasses (Listing, Search, ...)
├── parsers/         # Parsers par source (SeLoger, Laforêt, bienici), enregistrés via BaseParser
├── scraper/         # Scraping bas niveau SeLoger (page classified-search) et bienici (API JSON)
├── services/        # Orchestration du scraping + résolution des lieux par source
├── routes/          # Blueprints Flask (api, web, admin, auth)
├── core/            # Vocabulaire des critères (criteria), géocodage (geocode), helpers
├── scrape_logs/     # Capture, stockage et export/import des logs de scrape
├── config/          # Chargement config (loader.py + config.yaml)
├── templates/       # Pages HTML (Jinja2)
├── static/          # CSS + JS du formulaire de recherche
├── tests/           # unit/ (pur ou doublé), functional/ (app réelle), integration/ (vrai Postgres)
│   ├── conftest.py  #   garde-fous autouse : logs isolés, réseau et DB coupés, temps figé
│   └── helpers/     #   factories.py (constructeurs) + fakes.py (doubles)
├── .github/         # Pipeline CI (workflows/ci.yml) + seuil de couverture
├── pyproject.toml   # Config pytest, coverage et ruff
├── Dockerfile       # Déploiement
├── Dockerfile.test  # Image de test (docker-compose.test.yml)
└── requirements.txt
```

## Intégration continue

`.github/workflows/ci.yml` — sur chaque push de `main` et chaque pull request.
Un seul check est à protéger côté GitHub : **`CI`**, qui agrège tous les autres.

| Job | Ce qu'il garantit |
|-----|-------------------|
| `lint` | `ruff check` sur tout le dépôt |
| `unit` | la suite hors intégration passe **sans Postgres** — donc n'en dépend pas —, et les tests d'intégration sont bien **sautés** sans `TEST_DATABASE_URL` (non-régression de l'incident de fuite en production) |
| `integration` | les tests d'intégration passent sur Postgres **16 et 17**, et ont réellement tourné (un `skipped` fait échouer le job : une suite verte qui n'exécute rien est un faux positif) |
| `coverage` | la suite complète dépasse le seuil de `.github/coverage-threshold` — ce seuil ne doit que monter |
| `docker` | l'image de production build, `scripts/migrate.py` s'exécute depuis elle, `/health` répond, et l'image n'embarque **ni `tests/` ni `.env`** |
| `compose` | le chemin `docker compose -f docker-compose.test.yml` documenté ci-dessus fonctionne |
| `audit` | `pip-audit` sur les dépendances, et ni `.env` ni dump `.sql` dans l'index git |
