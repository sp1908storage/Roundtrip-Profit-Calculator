from .models import RoundTrip
from .calculator import RoundTripCost


def append_result(round_trip: RoundTrip, result: RoundTripCost) -> None:
    """Placeholder for Google Sheets integration.

    Recommended production behavior:
    - store one row per flight;
    - store a shared round_trip_id for grouped forward/backhaul flights;
    - store normalized input, validation status, cost breakdown, profit, manager comment;
    - keep raw user text separately for audit.
    """
    raise NotImplementedError("Google Sheets integration is not configured yet.")

