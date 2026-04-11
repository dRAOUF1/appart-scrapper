# 🏠 SeLoger Tracker

Plateforme web pour suivre les annonces immobilières SeLoger. Envoyez les pages HTML de SeLoger à l'API, elle gère le parsing, le stockage, la déduplication et les notifications push via [ntfy.sh](https://ntfy.sh).

## Fonctionnalités

- **Multi-utilisateur** : chaque utilisateur a son propre espace et ses recherches
- **API REST** : envoyez du HTML via POST, recevez les annonces parsées en JSON
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

### 3. Envoyer du HTML

La page **Recherches** vous génère automatiquement le lien POST et des exemples de code.

```bash
curl -X POST https://votre-url/api/parse/<search_id> \
  -H "X-API-Token: <votre-token>" \
  -H "Content-Type: text/html" \
  --data-binary @page_seloger.html
```

### 4. Consulter les annonces

Sur la page **Annonces**, consultez toutes les annonces trouvées avec prix, surface, localisation, photos et liens directs vers SeLoger.

## API Endpoints

| Endpoint | Méthode | Auth | Description |
|----------|---------|------|-------------|
| `POST /api/users` | POST | — | Créer un utilisateur |
| `POST /api/users/login` | POST | — | Se connecter |
| `POST /api/searches` | POST | token | Créer une recherche |
| `GET /api/searches` | GET | token | Lister ses recherches |
| `DELETE /api/searches/<id>` | DELETE | token | Supprimer une recherche |
| `POST /api/parse/<search_id>` | POST | token | Envoyer du HTML |
| `GET /api/listings/<search_id>` | GET | token | Consulter les annonces |
| `GET /api/stats` | GET | token | Statistiques |

Auth via header : `X-API-Token: <token>`

## Structure

```
├── main.py          # App Flask (API + Frontend)
├── parser.py        # Parsing HTML SeLoger
├── storage.py       # SQLite multi-utilisateur
├── notifier.py      # Notifications ntfy
├── config.py        # Chargement config
├── config.yaml      # Configuration
├── templates/       # Pages HTML (Jinja2)
├── static/          # CSS
├── Dockerfile       # Déploiement
└── requirements.txt
```
