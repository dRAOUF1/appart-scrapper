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
| `/api/sources` | GET | — | Lister les sources disponibles |
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

La suite est divisée en deux :
- **Tests unitaires** (`tests/`, mocks, aucune dépendance externe) :
  ```bash
  pip install -r requirements-dev.txt
  pytest tests/ -m "not integration"
  ```
- **Tests d'intégration** (`tests/integration/`, vrai Postgres) : sautés automatiquement si `DATABASE_URL` n'est pas défini. Pour les exécuter réellement :
  ```bash
  docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
  ```
  Ce compose démarre un Postgres jetable et lance toute la suite (unitaires + intégration) dedans.

## Structure

```
├── main.py          # App Flask (API + Frontend) + scheduler
├── storage.py       # Connexions PostgreSQL + accès aux repositories
├── notifier.py      # Notifications ntfy
├── repositories/    # CRUD par domaine (users, searches, listings, ...)
├── models/          # Dataclasses (Listing, Search, ...)
├── parsers/         # Parsers par source (SeLoger, ...), enregistrés via BaseParser
├── scraper/         # Scraping SeLoger (API BFF + classified-search)
├── services/        # Orchestration du scraping (ScrapeService)
├── routes/          # Blueprints Flask (api, web, admin, auth)
├── core/            # Helpers transverses (scrape_control, web_utils, schemas)
├── scrape_logs/     # Capture, stockage et export/import des logs de scrape
├── config/          # Chargement config (loader.py + config.yaml)
├── templates/       # Pages HTML (Jinja2)
├── static/          # CSS
├── tests/           # Tests unitaires (+ tests/integration/ pour les tests DB réels)
├── Dockerfile       # Déploiement
├── Dockerfile.test  # Image de test (docker-compose.test.yml)
└── requirements.txt
```
