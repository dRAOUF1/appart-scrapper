"""Small helpers shared by the route blueprints."""

from __future__ import annotations


def to_int(value, default: int) -> int:
    """Parse value as int, falling back to default on any invalid input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
