# 🏠 SeLoger Scraper

Surveille les annonces immobilières sur [SeLoger.com](https://www.seloger.com/) et t'envoie une notification push dès qu'une nouvelle annonce apparaît dans tes critères de recherche.

## ⚡ Fonctionnalités

- 🔍 **Surveillance automatique** — Scanne tes recherches SeLoger à intervalle régulier
- 📱 **Notifications push** — Alerte instantanée via [ntfy](https://ntfy.sh) (Android/iOS/Desktop)
- 🛡️ **Anti-détection** — Utilise `undetected-chromedriver` pour éviter le blocage
- 💾 **Mémoire** — Base SQLite pour ne jamais te notifier deux fois la même annonce
- 🔄 **Multi-recherches** — Surveille plusieurs URLs de recherche en parallèle

## 📦 Installation

### Prérequis

- Python 3.10+
- Google Chrome (ou Chromium) installé

### Setup

```bash
cd /home/raouf/Bureau/seloger_scrapper

# Créer un environnement virtuel
python3 -m venv venv
source venv/bin/activate

# Installer les dépendances
pip install -r requirements.txt
```

## ⚙️ Configuration

Édite le fichier `config.yaml` :

### 1. Ajouter tes URLs de recherche

1. Va sur [seloger.com](https://www.seloger.com/)
2. Fais ta recherche avec tes critères (ville, prix, surface, etc.)
3. Copie l'URL de la page de résultats
4. Colle-la dans `config.yaml` :

```yaml
search_urls:
  - "https://www.seloger.com/list.htm?projects=2&types=1..."
  - "https://www.seloger.com/list.htm?projects=1&types=2..."  # Deuxième recherche
```

### 2. Configurer les notifications

1. Installe l'app **ntfy** sur ton téléphone ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/app/ntfy/id1625396347))
2. Choisis un nom de topic unique dans `config.yaml` :

```yaml
ntfy:
  topic: "mon-topic-secret-12345"  # Choisis quelque chose d'unique !
```

3. Abonne-toi à ce topic dans l'app ntfy

### 3. Régler la fréquence

```yaml
interval_minutes: 5  # Scan toutes les 5 minutes
```

## 🚀 Utilisation

### Lancer la surveillance continue

```bash
python main.py
```

### Faire un seul scan (debug)

```bash
python main.py --once
```

### Tester les notifications

```bash
python main.py --test-notif
```

### Utiliser un fichier config alternatif

```bash
python main.py --config ma_config.yaml
```

## 📁 Structure du projet

```
seloger_scrapper/
├── config.yaml        # Configuration (URLs, notifications, etc.)
├── config.py          # Chargeur de configuration
├── scraper.py         # Scraper Selenium + undetected-chromedriver
├── storage.py         # Stockage SQLite des annonces
├── notifier.py        # Notifications push via ntfy
├── main.py            # Point d'entrée principal
├── requirements.txt   # Dépendances Python
├── listings.db        # Base de données (créée automatiquement)
└── README.md          # Ce fichier
```

## 🔧 Dépannage

| Problème | Solution |
|----------|----------|
| Chrome ne se lance pas | Vérifie que Chrome/Chromium est installé : `google-chrome --version` |
| Aucune annonce trouvée | SeLoger a peut-être changé ses sélecteurs CSS. Essaie en mode non-headless : `headless: false` dans config.yaml |
| Notifications non reçues | Vérifie le topic ntfy, lance `python main.py --test-notif` |
| Blocage anti-bot | Augmente `action_delay` dans config.yaml (ex: 5 secondes) |
| Erreur de timeout | Augmente `page_load_timeout` dans config.yaml |

## ⚠️ Avertissement

Ce scraper est conçu pour un usage personnel de surveillance d'annonces. Respecte les conditions d'utilisation de SeLoger.com. N'abuse pas de la fréquence des scans.
