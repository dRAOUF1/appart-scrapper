"""
parsers — Modular parsers for real estate listing sources.

Each source (SeLoger, BienIci, LeBonCoin, etc.) has its own parser module
that inherits from BaseParser. Parsers are registered automatically and
looked up by source slug.

Adding a new source:
    1. Create parsers/my_source.py
    2. Subclass BaseParser, implement scrape(criteria) -> list[Listing]
       (the only method the pipeline calls — see BaseParser docstring).
       `criteria` arrive au vocabulaire canonique (core.criteria) : la
       traduction vers le format de la source se fait dans to_native().
    3. Set SOURCE_ID and SOURCE_NAME class attributes
    4. Import the module in this __init__.py

That's it — the new source is immediately available via get_parser("my_source").
"""

from parsers.base import BaseParser, ParserRegistry
from parsers.bienici import BienIciParser
from parsers.century21 import Century21Parser
from parsers.laforet import LaforetParser
from parsers.seloger import SeLogerParser

# Register all parsers by importing them (class decorator handles registration)
# To add a new source, import it here:
# from parsers.leboncoin import LeBonCoinParser


def get_parser(source: str, storage=None) -> BaseParser:
    """Get a parser instance by source slug (e.g. 'seloger').

    `storage` est à passer dès qu'on peut : les sources qui ont besoin de la
    base (SeLoger et son cache d'identifiants de lieu) ne peuvent pas le lire
    depuis Flask, le scraping tournant sur un thread de fond.
    """
    return ParserRegistry.get(source, storage=storage)


def remember_manual_overrides(sources: list[str], criteria: dict, storage=None) -> None:
    """Laisse chaque source capitaliser ce que l'utilisateur a saisi à la main
    pour elle (voir BaseParser.remember_manual_override).

    À appeler une fois après l'enregistrement d'une recherche. N'échoue
    jamais : ce n'est qu'une optimisation.
    """
    for source in sources:
        try:
            ParserRegistry.get(source, storage=storage).remember_manual_override(criteria)
        except Exception:
            pass


def list_sources() -> list[dict]:
    """List all available sources with their metadata."""
    return ParserRegistry.list_sources()


__all__ = [
    "BaseParser", "get_parser", "list_sources", "remember_manual_overrides",
    "SeLogerParser", "LaforetParser", "BienIciParser", "Century21Parser",
]
