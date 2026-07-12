# 🏠 SeLoger Tracker

Plateforme web pour suivre les annonces immobilières SeLoger. Le scraping se fait côté serveur (via les API SeLoger), qui gère le parsing, le stockage, la déduplication et les notifications push via [ntfy.sh](https://ntfy.sh).

## Fonctionnalités

- **Multi-utilisateur** : chaque utilisateur a son propre espace et ses recherches
- **Scraping automatique** : un scheduler interne relance chaque recherche active selon son intervalle configuré
- **API REST** : gérer utilisateurs, recherches et annonces en JSON
- **Notifications push** : alertes instantanées via ntfy pour chaque nouvelle annonce
- **Frontend web** : dashboard, gestion des recherches, consultation des annonces
- **Déduplication intelligente** : les annonces sont stockées une seule fois, même entre utilisateurs

## Démarrage rapide

### Installation locale

```bash
pip install -r requirements.txt
python main.py
```

Le serveur démarre sur `http://localhost:10000`.

### Déploiement Render

1. Créer un **Web Service** sur Render
2. Utiliser **Docker** comme Runtime
3. Le Dockerfile gère tout automatiquement

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

## API Endpoints

| Endpoint | Méthode | Auth | Description |
|----------|---------|------|-------------|
| `/api/users` | POST | — | Créer un utilisateur |
| `/api/users/login` | POST | — | Se connecter |
| `/api/sources` | GET | — | Lister les sources disponibles |
| `/api/searches` | POST | token | Créer une recherche |
| `/api/searches` | GET | token | Lister ses recherches |
| `/api/searches/<id>` | DELETE | token | Supprimer une recherche |
| `/api/searches/<id>/criteria` | PUT | token | Modifier les critères |
| `/api/searches/<id>/toggle-active` | POST | token | Activer/désactiver une recherche |
| `/api/scrape/<search_id>` | POST | token | Déclencher un scraping manuel |
| `/api/listings/<search_id>` | GET | token | Consulter les annonces |
| `/api/stats` | GET | token | Statistiques |

Auth via header : `X-API-Token: <token>`

## Structure

```
├── main.py          # App Flask (API + Frontend) + scheduler
├── storage.py       # Connexions PostgreSQL + accès aux repositories
├── repositories/    # CRUD par domaine (users, searches, listings, ...)
├── models/          # Dataclasses (Listing, Search, ...)
├── parsers/         # Parsers par source (SeLoger, ...), enregistrés via BaseParser
├── scraper/         # Scraping SeLoger (API BFF + classified-search)
├── services/        # Orchestration du scraping (ScrapeService)
├── routes/          # Blueprints Flask (api, web, admin, auth)
├── notifier.py       # Notifications ntfy
├── config.py        # Chargement config
├── config.yaml      # Configuration
├── templates/       # Pages HTML (Jinja2)
├── static/          # CSS
├── Dockerfile       # Déploiement
└── requirements.txt
```
