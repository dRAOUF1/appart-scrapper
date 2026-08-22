"""Authentication decorators and session helpers."""

from __future__ import annotations

import os
from functools import wraps

from flask import current_app, flash, g, redirect, session, url_for


def require_token(f):
    """Decorator: require valid X-API-Token header for API endpoints."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        from flask import jsonify, request
        token = request.headers.get("X-API-Token", "")
        if not token:
            return jsonify({"error": "Header X-API-Token manquant"}), 401
        user = current_app.storage.users.get_user_by_token(token)
        if not user:
            return jsonify({"error": "Token invalide"}), 401
        g.user = user
        return f(*args, **kwargs)
    return wrapper


def require_login(f):
    """Decorator: require logged-in user via session for web endpoints."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        user = current_app.storage.users.get_user_by_token(session.get("api_token", ""))
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
        if "user_id" not in session:
            return redirect(url_for("web.login"))
        user = current_app.storage.users.get_user_by_token(session.get("api_token", ""))
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
