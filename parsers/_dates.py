"""Normalisation des dates de publication pour toutes les sources.

Issue #12 : ``listings.creation_date`` porte la date de PUBLICATION de
l'annonce chez sa source — pas la date de récupération par le scraper
(portée par ``first_seen``/``found_at`` et ``search_listings.found_at``).

Ce module est LA référence canonique du format de stockage :
  - une date exploitable est toujours en ISO-8601 UTC à la seconde :
    ``YYYY-MM-DDTHH:MM:SS+00:00`` ;
  - sinon la sentinelle littérale ``unknown`` (jamais de chaîne vide,
    qui se triait AVANT les vraies dates en TEXT et restait ambiguë).

La migration de données de ``storage.py`` s'appuie sur ce même contrat
(elle importe ce module) pour normaliser les valeurs héritées : un seul
endroit définit ce qui est parsable, donc aucun risque de dérive entre
l'écriture (parsers) et la reprise historique (migration).

Formats d'entrée observés dans les payloads sources (cf. tests unitaires
de chaque parser) : SeLoger « YYYY-MM-DD », bienici ISO+Z avec
millisecondes, orpi/foncia ISO avec décalage explicite, guyhoquet naïf
avec espace comme séparateur. Fonction purement stdlib : importable sans
dépendance réseau ni base.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Sentinelle explicite « la source ne fournit aucune date de publication ».
DATE_INCONNUE = "unknown"


def normaliser_creation_date(valeur: str | None) -> str:
    """Normalise une date de publication source vers l'ISO-8601 UTC canonique.

    Formats d'entrée acceptés :
      - ``None``, chaîne vide, ``unknown`` → sentinelle « unknown » ;
      - « YYYY-MM-DD » (SeLoger) → minuit UTC ;
      - ISO 8601 avec suffixe Z et millisecondes optionnelles (bienici) ;
      - ISO 8601 avec décalage explicite (orpi, foncia) → converti en UTC ;
      - ISO 8601 naïf, séparateur T ou espace (guyhoquet) → supposé UTC.

    Tout ce qui ne se parse pas (garbage, format inconnu) renvoie
    « unknown » plutôt que de lever : ni un scrape ni la migration de
    données ne doivent échouer sur une valeur héritée exotique.

    Sortie : « YYYY-MM-DDTHH:MM:SS+00:00 » (à la seconde, microsecondes
    tronquées pour l'homogénéité du tri lexical) ou « unknown ».
    """
    if valeur is None:
        return DATE_INCONNUE
    texte = valeur.strip()
    if not texte or texte == DATE_INCONNUE:
        return DATE_INCONNUE
    try:
        dt = datetime.fromisoformat(texte.replace("Z", "+00:00"))
    except ValueError:
        return DATE_INCONNUE
    if dt.tzinfo is None:
        # Naïf : on suppose UTC (aucune source ne documente un fuseau local).
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.replace(microsecond=0).isoformat()
