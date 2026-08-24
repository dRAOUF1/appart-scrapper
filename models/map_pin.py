"""MapPin model — repère personnel posé par un utilisateur sur la carte.

Issue #26 : les repères sont GLOBAUX (rattachés à l'utilisateur, jamais à une
recherche) et vivent dans `map_pins`. La base garantit déjà la cohérence
géographique (NOT NULL + CHECK non-nul et bornes) ; ce dataclass ne fait que
porter les lignes telles que le repository les relit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass
class MapPin:
    """Un repère personnel sur la carte."""

    id: int
    user_id: int
    label: str
    latitude: float
    longitude: float
    note: str = ""
    icon: str = "📍"
    created_at: datetime | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> MapPin:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
