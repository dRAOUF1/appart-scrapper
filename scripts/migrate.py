"""Run the DDL migrations against DATABASE_URL.

Safe to run repeatedly and against an already-populated database: every
statement is idempotent (CREATE ... IF NOT EXISTS / ADD COLUMN ... IF NOT
EXISTS). Run this after every deploy that changes the schema — it is NOT
invoked automatically at app startup (main.py only verifies tables exist
and fails fast otherwise, to avoid running DDL implicitly against a live
production database).

Usage:
    python -m scripts.migrate
    # or against a running deployment, e.g. on Render:
    #   open a shell on the service and run the same command
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from storage import Storage


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL manquant (ni variable d'env, ni .env).", file=sys.stderr)
        sys.exit(1)

    Storage.run_migrations(database_url)
    print("Migrations appliquées avec succès.")


if __name__ == "__main__":
    main()
