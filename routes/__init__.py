"""Flask route blueprints."""

from routes.admin import admin_bp
from routes.api import api_bp
from routes.web import web_bp

__all__ = ["api_bp", "web_bp", "admin_bp"]
