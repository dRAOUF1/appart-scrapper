# Confort de dev — deux façons de lancer l'app :
#
#   make dev   Instance locale : Postgres Docker dédié (créé au besoin,
#              persistant), migrations appliquées automatiquement, jamais
#              connectée à la production.
#   make prod  Instance branchée sur DATABASE_URL du .env — donc sur la
#              production si c'est ce qu'il contient. Confirmation explicite
#              avant de démarrer (voir scripts/migrate.py, même principe).
#
# `make help` liste toutes les cibles.

.PHONY: help dev dev-stop dev-reset prod docker-build docker-push test-js

VENV_ACTIVATE := venv/bin/activate
DEV_DB_CONTAINER := appart-dev-pg
DEV_DB_VOLUME := appart-dev-pg-data
DEV_DB_URL := postgresql://devuser:devpass@localhost:55433/appartdev
DEV_ENV_FILE := .env.dev
DEV_PORT := 5050

DOCKER_USER ?= draouf1
DOCKER_IMAGE := $(DOCKER_USER)/appart-scrapper
DOCKER_TAG := latest

help:
	@echo "make dev          — instance locale (DB Docker dédiée, jamais la prod)"
	@echo "make dev-stop     — arrête le conteneur Postgres de dev (garde les données)"
	@echo "make dev-reset    — supprime aussi les données de dev (repart de zéro)"
	@echo "make prod         — instance branchée sur .env (ATTENTION : la prod si .env pointe dessus)"
	@echo "make docker-build — build de l'image de production : $(DOCKER_IMAGE):$(DOCKER_TAG)"
	@echo "make docker-push  — login + build + push vers Docker Hub (DOCKER_USER et DOCKER_TAG surchargeables)"
	@echo "make test-js      — suite node --test de static/search_form.js (zéro dépendance)"

## Instance locale : DB Postgres Docker dédiée, jamais la production.
dev:
	@docker inspect $(DEV_DB_CONTAINER) >/dev/null 2>&1 || docker run -d --name $(DEV_DB_CONTAINER) \
		-e POSTGRES_USER=devuser -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=appartdev \
		-p 55433:5432 -v $(DEV_DB_VOLUME):/var/lib/postgresql/data postgres:16-alpine >/dev/null
	@docker start $(DEV_DB_CONTAINER) >/dev/null 2>&1 || true
	@echo "Attente de Postgres ($(DEV_DB_CONTAINER))..."
	@until docker exec $(DEV_DB_CONTAINER) pg_isready -U devuser -d appartdev >/dev/null 2>&1; do sleep 1; done
	@test -f $(DEV_ENV_FILE) || { \
		echo "SECRET_KEY=$$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > $(DEV_ENV_FILE); \
		echo "ADMIN_USERNAME=admin" >> $(DEV_ENV_FILE); \
		echo "$(DEV_ENV_FILE) créé (généré une seule fois, persiste entre les lancements)."; \
	}
	@echo "Démarrage sur http://localhost:$(DEV_PORT) — connectez-vous avec le nom d'utilisateur 'admin'."
	@bash -c '\
		set -a; source $(DEV_ENV_FILE); set +a; \
		source $(VENV_ACTIVATE); \
		DATABASE_URL="$(DEV_DB_URL)" python -m scripts.migrate --yes; \
		DATABASE_URL="$(DEV_DB_URL)" PORT=$(DEV_PORT) python main.py \
	'

## Arrête le conteneur Postgres de dev — les données restent dans le volume.
dev-stop:
	docker stop $(DEV_DB_CONTAINER)

## Supprime le conteneur ET ses données de dev (repart de zéro au prochain `make dev`).
dev-reset:
	docker rm -f $(DEV_DB_CONTAINER) 2>/dev/null || true
	docker volume rm $(DEV_DB_VOLUME) 2>/dev/null || true

## Instance branchée sur .env — DATABASE_URL n'est PAS surchargée : si .env
## pointe vers la production (c'est son usage normal ailleurs dans le repo,
## voir scripts/migrate.py), cette commande scrape et notifie en PRODUCTION,
## scheduler compris. Confirmation explicite avant de démarrer.
prod:
	@test -f .env || { echo ".env introuvable — rien à lancer."; exit 1; }
	@echo "Cible : $$(grep '^DATABASE_URL=' .env | sed -E 's#(://[^:]+):[^@]+@#\1:***@#')"
	@echo "⚠️  Cette instance va scraper et notifier EN PRODUCTION (scheduler actif toutes les 30s) si l'URL ci-dessus est bien la prod."
	@read -p "Continuer ? [oui/non] " ans; [ "$$ans" = "oui" ] || { echo "Annulé."; exit 1; }
	@bash -c 'source $(VENV_ACTIVATE) && python main.py'

## Build de l'image de production. Surchargeable :
##   DOCKER_USER=moncompte DOCKER_TAG=v1.0.0 make docker-build
docker-build:
	docker build -t $(DOCKER_IMAGE):$(DOCKER_TAG) .

## Suite JS (node --test natif, Node >= 18, zéro dépendance npm) : couvre
## static/search_form.js (écart n°1 de l'audit de tests). NB : sur Node >= 21
## un répertoire en argument n'est plus récursé, on passe donc le glob.
test-js:
	node --test "tests/js/**/*.test.mjs"

## Login + build + push vers Docker Hub.
## `docker login` est interactif (identifiant + mot de passe/token).
docker-push: docker-build
	docker login
	docker push $(DOCKER_IMAGE):$(DOCKER_TAG)
