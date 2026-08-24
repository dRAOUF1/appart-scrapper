"""Réglages applicatifs éditables depuis l'admin (issue #21).

Les valeurs vivent dans la table `app_settings` (clé/valeur, via
settings_repo) et sont LUES AU MOMENT DE L'USAGE — le même pattern que la
pause du scheduler (#17) : un changement fait dans l'UI est effectif au
prochain tour concerné, sans redémarrage ni cache process.

Ce module centralise ce qui ne doit JAMAIS diverger entre les endroits qui
lisent un réglage :
  - les clés canoniques (une coquille d'orthographe rendrait le formulaire
    admin inopérant sans erreur visible) ;
  - les défauts historiques du code (`delete_old_listings(days=4)`,
    `purge_old_logs(days=30)`), pour que « pas de réglage » reste exactement
    le comportement d'avant l'issue ;
  - la validation de saisie (messages français destinés au toast) et la
    lecture tolérante côté usage.
"""

from __future__ import annotations

# Clé app_settings : nombre de jours de rétention des annonces purgées par
# l'action « Nettoyer annonces » (admin comme web).
CLE_RETENTION_ANNONCES = "retention_listings_days"
# Défaut historique : routes /admin/cleanup et /cleanup appelaient
# delete_old_listings(days=4) avant que la valeur soit éditable.
DEFAUT_RETENTION_ANNONCES = 4

# Clé app_settings : nombre de jours de conservation des logs d'audit admin.
CLE_PURGE_LOGS_AUDIT = "purge_logs_days"
# Défaut historique : purge_old_logs(days=30) dans AdminRepository et la route.
DEFAUT_PURGE_LOGS_AUDIT = 30

# Bornes communes aux deux réglages (en jours) : assez larges pour être
# utiles, assez serrées pour qu'une saisie absurde ne vide jamais une table
# entière par accident ni n'en fige la croissance pour toujours.
BORNE_MIN_JOURS = 1
BORNE_MAX_JOURS = 365


def valider_jours(brut: str, libelle: str) -> int:
    """Valide une saisie de jours venant du formulaire admin.

    Entier strictement positif, plafonné à BORNE_MAX_JOURS. Lève ValueError
    avec un message FRANÇAIS destiné à l'utilisateur (toast) : une borne
    refusée doit dire pourquoi, jamais être avalée en silence.
    """
    texte = (brut or "").strip()
    if not texte:
        raise ValueError(f"{libelle} : saisie vide")
    try:
        valeur = int(texte)
    except ValueError:
        raise ValueError(f"{libelle} : « {texte} » n'est pas un nombre entier") from None
    if valeur <= 0:
        raise ValueError(
            f"{libelle} : la valeur doit être strictement positive (reçu {valeur})"
        )
    if valeur > BORNE_MAX_JOURS:
        raise ValueError(
            f"{libelle} : {valeur} jours dépasse le maximum autorisé ({BORNE_MAX_JOURS})"
        )
    return valeur


def lire_jours(get_setting, cle: str, defaut: int) -> int:
    """Lit un réglage en jours AU MOMENT DE L'USAGE, tolérant aux accidents.

    `get_setting` est injecté par l'appelant (le getter du repository des
    réglages, passé en paramètre) : ce module reste une logique pure, sans
    accès au repository — le garde « consommateurs de app_settings » (#17)
    continue de ne compter que main.py et l'admin.

    Valeur absente, non entière ou hors bornes → retour au défaut historique
    (fail-open, comme la lecture de la pause #17) : une donnée corrompue dans
    app_settings ne doit jamais bloquer une purge, et une base injoignable non
    plus. Toute routine qui consomme un réglage DOIT repasser ici à chaque run
    — c'est ce qui rend l'édition dans l'UI effective au cycle suivant.
    """
    try:
        brut = get_setting(cle, "")
    except Exception:
        return defaut
    try:
        valeur = int(str(brut).strip())
    except (TypeError, ValueError):
        return defaut
    if valeur < BORNE_MIN_JOURS or valeur > BORNE_MAX_JOURS:
        return defaut
    return valeur
