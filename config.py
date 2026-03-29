"""Configuration loader for the SeLoger API platform."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from loguru import logger


class NtfyConfig(BaseModel):
    """ntfy notification defaults."""
    server: str = "https://ntfy.sh"
    priority: str = "default"


class StorageConfig(BaseModel):
    """SQLite storage configuration."""
    db_path: str = "listings.db"


class AppConfig(BaseModel):
    """Root application configuration."""
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    log_level: str = "INFO"


def load_config(config_path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate configuration from a YAML file."""
    config_path = Path(config_path)
    if not config_path.exists():
        logger.warning(f"Config introuvable ({config_path}), utilisation des défauts")
        return AppConfig()

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    try:
        config = AppConfig(**raw)
    except Exception as e:
        logger.error(f"Erreur de configuration : {e}")
        sys.exit(1)

    logger.info(f"Configuration chargée depuis {config_path}")
    return config
