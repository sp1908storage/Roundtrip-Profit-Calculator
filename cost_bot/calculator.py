from dataclasses import dataclass

from .config import CostConfig, DEFAULT_COST_CONFIG
from .models import Flight, LoadingType, RoundTrip, TransportStatus
from .validators import direction_is_allowed, route_has_land_road, validate_flight


@dataclass(frozen=True)
class FlightCost:
    flight: Flight
    revenue_rub: float
    fuel_rub: float
    maintenance_rub: float
    driver_and_fixed_rub: float
    tolls_rub: float
    loading_extra_rub: float
    international_extra_rub: float
    total_cost_rub: float
    profit_rub: float


@dataclass(frozen=True)
class RoundTripCost:
    flights: list[FlightCost]

    @property
    def total_revenue_rub(self) -> float:
        return sum(item.revenue_rub for item in self.flights)

    @property
    def total_cost_rub(self) -> float:
        return sum(item.total_cost_rub for item in self.flights)

    @property
    def total_profit_rub(self) -> float:
        return self.total_revenue_rub - self.total_cost_rub


def calculate_flight_cost(flight: Flight, config: CostConfig = DEFAULT_COST_CONFIG) -> FlightCost:
    issues = validate_flight(flight)
    fatal_messages = [issue.message for issue in issues if issue.fatal or issue.field != "client_short"]
    if fatal_messages:
        raise ValueError("; ".join(fatal_messages))
    if not route_has_land_road(flight):
        raise ValueError("Не удалось подтвердить наличие сухопутного маршрута.")
    if not direction_is_allowed(flight, config.prohibited_country_pairs):
        raise ValueError("Направление запрещено правилами компании.")

    distance_to_loading = flight.distance_to_loading_km or 0
    distance_to_unloading = flight.distance_to_unloading_km or 0
    total_km = distance_to_loading + distance_to_unloading

    foreign_km = 0.0
    if flight.status == TransportStatus.INTERNATIONAL:
        russian_km = flight.russian_territory_km or 0
        foreign_km = max(distance_to_unloading - russian_km, 0)

    domestic_km = total_km - foreign_km
    km_cost_base = domestic_km + foreign_km * config.international_foreign_km_factor

    fuel_rub = km_cost_base * config.fuel_consumption_l_per_100km / 100 * config.fuel_price_rub_per_l
    maintenance_rub = km_cost_base * config.maintenance_rub_per_km
    driver_and_fixed_rub = km_cost_base * config.driver_and_fixed_rub_per_km
    tolls_rub = domestic_km * config.tolls_rub_per_km
    loading_extra_rub = (
        config.loading_top_side_extra_rub if flight.loading_type == LoadingType.TOP_SIDE else 0.0
    )
    international_extra_rub = (
        config.international_extra_rub if flight.status == TransportStatus.INTERNATIONAL else 0.0
    )
    total_cost = (
        fuel_rub
        + maintenance_rub
        + driver_and_fixed_rub
        + tolls_rub
        + loading_extra_rub
        + international_extra_rub
    )
    revenue = flight.rate_with_vat_rub or 0.0

    return FlightCost(
        flight=flight,
        revenue_rub=revenue,
        fuel_rub=fuel_rub,
        maintenance_rub=maintenance_rub,
        driver_and_fixed_rub=driver_and_fixed_rub,
        tolls_rub=tolls_rub,
        loading_extra_rub=loading_extra_rub,
        international_extra_rub=international_extra_rub,
        total_cost_rub=total_cost,
        profit_rub=revenue - total_cost,
    )


def calculate_round_trip(round_trip: RoundTrip, config: CostConfig = DEFAULT_COST_CONFIG) -> RoundTripCost:
    return RoundTripCost([calculate_flight_cost(flight, config) for flight in round_trip.flights])

