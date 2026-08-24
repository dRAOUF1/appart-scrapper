"""Authentication decorators and session helpers."""

from __future__ import annotations

import os
from functools import wraps
from typing import TYPE_CHECKING

from flask import current_app, flash, g, redirect, render_template, request, session, url_for

if TYPE_CHECKING:  # pragma: no cover - import réservé aux annotations
    from repositories.base import BaseRepository

# ---------------------------------------------------------------------------
# Impersonation lecture seule (issue #22)
#
# Pendant qu'un admin consulte le compte d'un utilisateur « comme lui », la
# session porte trois clés dédiées :
#   - `impersonator_user_id` / `impersonator_username` : l'admin RÉEL, figés à
#     l'entrée — c'est depuis eux que la sortie restaure la session, même si la
#     ligne de la CIBLE a été supprimée entre-temps ;
#   - et elle écrase `user_id` / `username` avec ceux de la cible : tous les
#     décorateurs existants (@require_login) résolvent alors naturellement le
#     dashboard, les recherches et les annonces DE LA CIBLE.
#
# La présence de `impersonator_user_id` est LE marqueur d'état : le guard des
# mutations (routes/web.py), l'inaccessibilité de l'admin (ci-dessous) et la
# bannière du layout (templates/base.html) lisent tous cette même clé.
# ---------------------------------------------------------------------------

CLE_IMPERSONATEUR_ID = "impersonator_user_id"
CLE_IMPERSONATEUR_USERNAME = "impersonator_username"
CLE_IMPERSONE_USERNAME = "impersonated_username"


def est_impersonation_active() -> bool:
    """Vrai si la session courante est une vue administrateur lecture seule."""
    return CLE_IMPERSONATEUR_ID in session


def demarrer_impersonation(sess, admin: dict, cible: dict) -> None:
    """Bascule la session vers la cible, en mémorisant l'admin réel.

    Fonction pure sur le mapping passé (la vraie session Flask en production,
    un dict en test) pour rester testable sans requête.
    """
    sess[CLE_IMPERSONATEUR_ID] = admin["id"]
    sess[CLE_IMPERSONATEUR_USERNAME] = admin["username"]
    sess[CLE_IMPERSONE_USERNAME] = cible["username"]
    # L'écrasement est DERNIER : si un jour un helper oublie les clés
    # précédentes, la session reste au moins cohérente (identité = cible).
    sess["user_id"] = cible["id"]
    sess["username"] = cible["username"]


def terminer_impersonation(sess, storage: BaseRepository) -> dict | None:
    """Restaure la session de l'admin qui avait lancé la vue.

    Retourne la ligne admin restaurée, ou None s'il n'existe plus en base
    (compte supprimé pendant la consultation) : dans ce cas la session est
    PURGÉE intégralement plutôt que laissée à moitié restaurée — on ne peut
    pas reconstituer une identité valide, il faut repasser par /login.
    """
    admin_restaure = storage.users.get_user_by_id(sess.get(CLE_IMPERSONATEUR_ID))
    for cle in (CLE_IMPERSONATEUR_ID, CLE_IMPERSONATEUR_USERNAME, CLE_IMPERSONE_USERNAME):
        sess.pop(cle, None)
    if not admin_restaure:
        sess.clear()
        return None
    sess["user_id"] = admin_restaure["id"]
    sess["username"] = admin_restaure["username"]
    return admin_restaure


def _reponse_lecture_seule() -> tuple[str, int]:
    """La page 403 française commune aux deux guards d'impersonation.

    Le message nomme l'issue de fond (lecture seule) et l'issue de forme
    (l'action demandée n'est PAS exécutée) ; le rendu passe par le layout
    standard, donc la bannière et son bouton de sortie restent accessibles.
    """
    return (
        render_template(
            "lecture_seule.html",
            action=request.method,
        ),
        403,
    )


def require_login(f):
    """Decorator: require logged-in user via session for web endpoints."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        user = current_app.storage.users.get_user_by_id(session["user_id"])
        if not user:
            session.clear()
            return redirect(url_for("web.login"))
        g.user = user
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    """Decorator: require admin user (ADMIN_USERNAME env var match)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Issue #22, choix documenté : pendant une impersonation, TOUTES les
        # routes admin sont inaccessibles SAUF la sortie. Un admin qui voit le
        # compte d'un utilisateur ne peut ni piloter le scraping ni toucher aux
        # autres comptes sous peine d'actions commises « au nom » de la vue
        # impersonnée ; la seule échappatoire est de sortir proprement. Le
        # contrôle explicite ici donne ce message précis (et protège si un jour
        # plusieurs comptes admin existaient) — la sortie d'impersonation vit
        # sur sa propre route, SANS ce décorateur justement.
        if est_impersonation_active():
            return _reponse_lecture_seule()
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        user = current_app.storage.users.get_user_by_id(session["user_id"])
        if not user:
            session.clear()
            return redirect(url_for("web.login"))
        admin_username = os.environ.get("ADMIN_USERNAME")
        if not admin_username or user["username"] != admin_username:
            flash("Accès refusé", "error")
            return redirect(url_for("web.dashboard"))
        g.user = user
        return f(*args, **kwargs)
    return wrapper
