"""
Template pour créer un nouveau parser de source immobilière.

GUIDE: Ajouter une nouvelle source en 3 étapes
=================================================

1. Copier ce fichier → parsers/ma_source.py
2. Remplir les champs SOURCE_ID, SOURCE_NAME, SOURCE_DESCRIPTION
3. Importer le nouveau parser dans parsers/__init__.py

C'est tout ! Le parser est automatiquement enregistré et disponible via
l'API (GET /api/sources) et le formulaire de création de recherche.

Exemple : pour ajouter BienIci
    - Créer parsers/bienici.py (en s'inspirant de ce fichier)
    - Ajouter dans parsers/__init__.py :
        from parsers.bienici import BienIciParser
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from loguru import logger

from parsers.base import BaseParser
from storage import Listing


class TemplateParser(BaseParser):
    """
    Parser template — remplacer par le nom de la source.
    Ne PAS modifier SOURCE_ID à vide ou le parser ne sera pas enregistré.
    """

    SOURCE_ID = ""            # ex: "bienici" — slug unique, pas d'espaces ni majuscules
    SOURCE_NAME = ""          # ex: "BienIci" — nom affiché dans l'UI
    SOURCE_DESCRIPTION = ""   # ex: "BienIci.com — Annonces immobilières"

    def parse(self, html: str) -> list[Listing]:
        """
        Implémenter ici l'extraction des annonces depuis le HTML de la source.

        Args:
            html: Contenu HTML brut de la page de résultats de recherche.

        Returns:
            Liste de Listing extraits de la page.
        """
        soup = BeautifulSoup(html, "lxml")
        listings: list[Listing] = []

        # --- Exemple d'extraction ---
        # for card in soup.select(".listing-card"):
        #     url = card.select_one("a")["href"]
        #     price = card.select_one(".price").get_text(strip=True)
        #     listings.append(Listing(
        #         listing_id=f"src_{hash(url)}",
        #         url=url,
        #         price=price,
        #         source=self.SOURCE_ID,   # ← toujours renseigner source
        #     ))

        logger.info(f"[{self.SOURCE_NAME}] {len(listings)} annonces extraites")
        return listings
