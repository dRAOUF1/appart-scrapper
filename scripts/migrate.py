"""Run the DDL migrations against DATABASE_URL.

Safe to run repeatedly and against an already-populated database: every
statement is idempotent (CREATE ... IF NOT EXISTS / ADD COLUMN ... IF NOT
EXISTS). Run this after every deploy that changes the schema — it is NOT
invoked automatically at app startup (main.py only verifies tables exist
and fails fast otherwise, to avoid running DDL implicitly against a live
production database).

Usage:
    python -m scripts.migrate            # demande confirmation si la cible est distante
    python -m scripts.migrate --yes      # sans confirmation (déploiement, CI)
    # or against a running deployment, e.g. on Render:
    #   open a shell on the service and run the same command
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from storage import Storage

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "db", ""}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    assume_yes = "--yes" in argv or "-y" in argv

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL manquant (ni variable d'env, ni .env).", file=sys.stderr)
        sys.exit(1)

    # `.env` est chargé ci-dessus : la cible peut très bien être la production
    # sans qu'on l'ait voulu. On l'affiche toujours, avant d'écrire quoi que ce soit.
    parsed = urlparse(database_url)
    host = (parsed.hostname or "").lower()
    target = f"{parsed.username or '?'}@{host}:{parsed.port or 5432}{parsed.path}"
    print(f"Cible des migrations : {target}")

    if host not in _LOCAL_HOSTS and not assume_yes:
        if not sys.stdin.isatty():
            print(
                f"Cible distante ({host}) et pas de terminal pour confirmer. "
                "Relancer avec --yes si c'est intentionnel.",
                file=sys.stderr,
            )
            sys.exit(1)
        answer = input(f"Appliquer les migrations sur {host} ? [oui/non] ").strip().lower()
        if answer not in {"oui", "o", "yes", "y"}:
            print("Annulé.", file=sys.stderr)
            sys.exit(1)

    Storage.run_migrations(database_url)
    print("Migrations appliquées avec succès.")


if __name__ == "__main__":
    main()
