from dataclasses import dataclass, field


COUNTRY_RUSSIA = "Россия"
COUNTRY_OPTIONS = ["Беларусь", "Китай", "Монголия"]
LOADING_OPTIONS = ["сзади", "сверху/сбоку"]
VAT_OPTIONS = [22, 0]
MAX_BACKHAUL_WEIGHT_KG = 21_500
DEFAULT_WEIGHT_KG = 20_000


@dataclass(frozen=True)
class CostConfig:
    """Demo economics. Replace these numbers with real company formulas."""

    fuel_consumption_l_per_100km: float = 32.0
    fuel_price_rub_per_l: float = 72.0
    maintenance_rub_per_km: float = 18.0
    driver_and_fixed_rub_per_km: float = 24.0
    tolls_rub_per_km: float = 4.0
    loading_top_side_extra_rub: float = 5_000.0
    international_extra_rub: float = 15_000.0
    international_foreign_km_factor: float = 1.15
    prohibited_country_pairs: set[tuple[str, str]] = field(default_factory=set)


DEFAULT_COST_CONFIG = CostConfig()

