"""Configuration loader and validation for SeLoger Scraper."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from loguru import logger


class SearchConfig(BaseModel):
    """A single search: URL + ntfy topic."""
    url: str
    topic: str

    @field_validator("url", mode="before")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if "seloger.com" not in v:
            raise ValueError(f"URL invalide (doit contenir seloger.com): {v}")
        return v


class NtfyConfig(BaseModel):
    """ntfy notification configuration."""
    server: str = "https://ntfy.sh"
    priority: str = "default"


class BrowserConfig(BaseModel):
    """Browser / Selenium configuration."""
    headless: bool = True
    page_load_timeout: int = 30
    action_delay: float = 2.0


class StorageConfig(BaseModel):
    """SQLite storage configuration."""
    db_path: str = "listings.db"


class AppConfig(BaseModel):
    """Root application configuration."""
    searches: list[SearchConfig] = Field(..., min_length=1)
    interval_minutes: int = Field(default=5, ge=1)
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    log_level: str = "INFO"


def load_config(config_path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate configuration from a YAML file."""
    config_path = Path(config_path)
    if not config_path.exists():
        logger.error(f"Fichier de configuration introuvable : {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    try:
        config = AppConfig(**raw)
    except Exception as e:
        logger.error(f"Erreur de configuration : {e}")
        sys.exit(1)

    logger.info(f"Configuration chargee depuis {config_path}")
    return config
