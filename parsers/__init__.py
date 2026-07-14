"""
parsers — Modular parsers for real estate listing sources.

Each source (SeLoger, BienIci, LeBonCoin, etc.) has its own parser module
that inherits from BaseParser. Parsers are registered automatically and
looked up by source slug.

Adding a new source:
    1. Create parsers/my_source.py
    2. Subclass BaseParser, implement scrape(criteria, use_bff) -> list[Listing]
       (the only method the pipeline calls — see BaseParser docstring)
    3. Set SOURCE_ID and SOURCE_NAME class attributes
    4. Import the module in this __init__.py

That's it — the new source is immediately available via get_parser("my_source").
"""

from parsers.base import BaseParser, ParserRegistry
from parsers.seloger import SeLogerParser
from parsers.laforet import LaforetParser

# Register all parsers by importing them (class decorator handles registration)
# To add a new source, import it here:
# from parsers.bienici import BienIciParser
# from parsers.leboncoin import LeBonCoinParser


def get_parser(source: str) -> BaseParser:
    """Get a parser instance by source slug (e.g. 'seloger')."""
    return ParserRegistry.get(source)


def list_sources() -> list[dict]:
    """List all available sources with their metadata."""
    return ParserRegistry.list_sources()


__all__ = ["BaseParser", "get_parser", "list_sources", "SeLogerParser", "LaforetParser"]
