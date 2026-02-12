"""
Fair value calculation for financial range markets
"""

import math
from datetime import datetime, timezone


def normal_cdf(x: float) -> float:
    """Standard normal cumulative distribution function"""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def calculate_range_probability(
    current_price: float,
    lower_bound: float,
    upper_bound: float,
    volatility: float,
    hours_to_expiry: float
) -> float:
    """
    Calculate probability of price being in range at expiry.

    Uses normal distribution centered at current price.

    Args:
        current_price: Current futures/index price
        lower_bound: Lower range bound
        upper_bound: Upper range bound (use float('inf') for "above X")
        volatility: Annualized volatility (e.g., 0.20 for 20%)
        hours_to_expiry: Hours until market closes

    Returns:
        Probability (0-1) of price being in range
    """
    if hours_to_expiry <= 0:
        if lower_bound <= current_price <= upper_bound:
            return 1.0
        return 0.0

    # Convert to trading days then years
    trading_days = hours_to_expiry / 6.5
    t = trading_days / 252

    # Expected move in dollar terms
    sigma_dollars = current_price * volatility * math.sqrt(t)

    if sigma_dollars < 0.01:
        if lower_bound <= current_price <= upper_bound:
            return 1.0
        return 0.0

    # Handle tail markets
    if upper_bound == float('inf'):
        z_lower = (lower_bound - current_price) / sigma_dollars
        return 1 - normal_cdf(z_lower)

    if lower_bound <= 0:
        z_upper = (upper_bound - current_price) / sigma_dollars
        return normal_cdf(z_upper)

    # Standard range: P(L < S < U)
    z_lower = (lower_bound - current_price) / sigma_dollars
    z_upper = (upper_bound - current_price) / sigma_dollars

    return max(0, min(1, normal_cdf(z_upper) - normal_cdf(z_lower)))


def hours_until(close_time: datetime) -> float:
    """Calculate hours until a datetime"""
    if not close_time:
        return 24

    now = datetime.now(timezone.utc)
    if close_time.tzinfo is None:
        close_time = close_time.replace(tzinfo=timezone.utc)

    delta = close_time - now
    return max(0, delta.total_seconds() / 3600)


# Default volatility assumptions
DEFAULT_VOLATILITY = {
    "nasdaq": 0.20,  # 20% annual
    "spx": 0.16,     # 16% annual
}
