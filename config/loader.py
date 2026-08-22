"""Configuration loader for the SeLoger API platform."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml
from loguru import logger
from pydantic import BaseModel, Field


class NtfyConfig(BaseModel):
    """ntfy notification defaults."""
    server: str = "https://ntfy.sh"
    priority: str = "default"


class DatabaseConfig(BaseModel):
    """PostgreSQL database configuration."""
    database_url: str = "postgresql://postgres:postgres@localhost:5432/appart"


class AppConfig(BaseModel):
    """Root application configuration."""
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    log_level: str = "INFO"


def load_config(config_path: str | Path = "config/config.yaml") -> AppConfig:
    """Load and validate configuration from a YAML file.

    Environment variable DATABASE_URL overrides config.yaml.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        logger.warning(f"Config introuvable ({config_path}), utilisation des défauts")
        raw = {}
    else:
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    # Override database_url with env var if present
    env_db_url = os.environ.get("DATABASE_URL")
    if env_db_url:
        raw["database"] = {"database_url": env_db_url}
        logger.info("DATABASE_URL chargé depuis les variables d'environnement")

    try:
        config = AppConfig(**raw)
    except Exception as e:
        logger.error(f"Erreur de configuration : {e}")
        sys.exit(1)

    logger.info(f"Configuration chargée depuis {config_path}")
    return config
