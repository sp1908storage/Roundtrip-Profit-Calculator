from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import DEFAULT_WEIGHT_KG


class Direction(str, Enum):
    FORWARD = "direct"
    BACKHAUL = "backhaul"


class TransportStatus(str, Enum):
    DOMESTIC = "Внутрироссийская"
    INTERNATIONAL = "Международная"


class LoadingType(str, Enum):
    REAR = "сзади"
    TOP_SIDE = "сверху/сбоку"


@dataclass
class Flight:
    direction: Direction
    client_short: str | None = None
    loading_address: str | None = None
    distance_to_loading_km: float | None = None
    unloading_address: str | None = None
    rate_with_vat_rub: float | None = None
    status: TransportStatus | None = None
    country: str | None = None
    vat_percent: int | None = None
    distance_to_unloading_km: float | None = None
    russian_territory_km: float | None = None
    cargo_weight_kg: float = DEFAULT_WEIGHT_KG
    loading_type: LoadingType = LoadingType.REAR

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction.value,
            "client_short": self.client_short,
            "loading_address": self.loading_address,
            "distance_to_loading_km": self.distance_to_loading_km,
            "unloading_address": self.unloading_address,
            "rate_with_vat_rub": self.rate_with_vat_rub,
            "status": self.status.value if self.status else None,
            "country": self.country,
            "vat_percent": self.vat_percent,
            "distance_to_unloading_km": self.distance_to_unloading_km,
            "russian_territory_km": self.russian_territory_km,
            "cargo_weight_kg": self.cargo_weight_kg,
            "loading_type": self.loading_type.value,
        }


@dataclass
class RoundTrip:
    forward_flights: list[Flight] = field(default_factory=list)
    backhaul_flights: list[Flight] = field(default_factory=list)

    @property
    def flights(self) -> list[Flight]:
        return [*self.forward_flights, *self.backhaul_flights]

