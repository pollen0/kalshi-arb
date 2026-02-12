"""
Sophisticated Fair Value Model for Financial Range Markets

Improvements over basic model:
1. Log-normal distribution (prices can't go negative)
2. Skew adjustment based on recent momentum
3. Fat tails / excess kurtosis for crash risk
4. Time-of-day adjustments (overnight vs intraday volatility)
5. VIX-based dynamic volatility scaling
6. Mean reversion tendency for extreme moves

Based on: Black-Scholes assumptions + empirical adjustments
"""

import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional, Tuple
from zoneinfo import ZoneInfo
from scipy import stats  # For student's t distribution

# US Eastern timezone for market hours
ET = ZoneInfo("America/New_York")


@dataclass
class FairValueResult:
    """Complete fair value calculation result"""
    probability: float          # Main fair value (0-1)
    confidence_low: float       # Lower bound of confidence interval
    confidence_high: float      # Upper bound of confidence interval
    model_vol: float           # Volatility used in calculation
    effective_hours: float     # Time-adjusted hours to expiry
    skew_adjustment: float     # Skew factor applied
    model_type: str            # Which model was used
    vol_source: str = "default"  # Where volatility came from: options_implied, historical, vix_scaled, default


class SophisticatedFairValue:
    """
    Advanced fair value calculator with multiple adjustments.

    Key features:
    - Log-normal distribution for realistic price modeling
    - Student's t-distribution for fat tails
    - Momentum-based skew
    - Overnight/intraday volatility differentiation
    - VIX scaling
    """

    # Market hours (Eastern Time)
    MARKET_CLOSE_HOUR = 16  # 4:00 PM ET

    # Default base volatility assumptions (annualized) - used if historical not available
    BASE_VOLATILITY = {
        "nasdaq": 0.22,      # NDX is more volatile
        "ndx": 0.22,
        "spx": 0.16,
        "treasury10y": 0.15, # ~15% annualized vol for yields (in percentage point terms)
        "usdjpy": 0.08,      # Forex typically 8-12%
        "eurusd": 0.08,
    }

    # Asset class types (affects how we model price changes)
    ASSET_CLASS = {
        "nasdaq": "equity",
        "ndx": "equity",
        "spx": "equity",
        "treasury10y": "yield",  # Yields use absolute changes
        "usdjpy": "forex",
        "eurusd": "forex",
    }

    # Overnight volatility multiplier (overnight moves are typically larger)
    OVERNIGHT_VOL_MULTIPLIER = 1.3

    # Fat tail adjustment (degrees of freedom for Student's t)
    # Lower = fatter tails. Markets typically have df ~ 4-6
    TAIL_DF = 5

    # Trading hours per day by asset class
    TRADING_HOURS_PER_DAY = {
        "equity": 6.5,      # 9:30 AM - 4:00 PM ET
        "futures": 23.0,    # Nearly 24-hour trading
        "forex": 24.0,      # 24-hour market
        "yield": 6.5,       # Similar to equity hours
    }

    def __init__(self,
                 vix_price: Optional[float] = None,
                 recent_returns: Optional[list[float]] = None,
                 use_fat_tails: bool = True,
                 historical_vols: Optional[dict] = None,
                 options_client=None,
                 options_skew: Optional[dict] = None):
        """
        Initialize the fair value calculator.

        Args:
            vix_price: Current VIX level (if available)
            recent_returns: List of recent returns for momentum calculation
            use_fat_tails: Whether to use Student's t instead of normal
            historical_vols: Dict of historical volatilities by index (e.g., {"nasdaq": 0.18})
            options_client: OptionsDataClient instance for implied vol
            options_skew: Dict of put-call skew by index (e.g., {"nasdaq": 0.02})
        """
        self.vix_price = vix_price
        self.recent_returns = recent_returns or []
        self.use_fat_tails = use_fat_tails
        self.historical_vols = historical_vols or {}
        self.options_client = options_client
        self.options_implied_vols: dict = {}  # Cached implied vols
        self.options_skew = options_skew or {}

    def set_options_client(self, client):
        """Set options data client for implied volatility"""
        self.options_client = client

    def update_options_data(self, index: str, hours_to_expiry: float = 8.0):
        """Update options-implied volatility and skew for an index"""
        if not self.options_client:
            return

        try:
            snapshot = self.options_client.get_options_snapshot(index, hours_to_expiry)
            if snapshot:
                if snapshot.atm_iv:
                    self.options_implied_vols[index.lower()] = snapshot.atm_iv
                if snapshot.put_call_skew:
                    self.options_skew[index.lower()] = snapshot.put_call_skew
        except Exception as e:
            print(f"[FAIR_VALUE] Error updating options data for {index}: {e}")

    def _get_vix_adjusted_vol(self, base_vol: float, index: str) -> float:
        """
        Adjust volatility based on VIX level.

        VIX represents 30-day implied vol for SPX.
        Scale our base vol proportionally.
        """
        if not self.vix_price:
            return base_vol

        # VIX is quoted as annualized vol * 100
        # Normal VIX is ~15-20, high is 25+, panic is 40+
        vix_vol = self.vix_price / 100

        if index == "spx":
            # SPX vol should track VIX closely
            return vix_vol
        elif index == "nasdaq":
            # NDX is typically ~1.3x SPX volatility
            return vix_vol * 1.3
        else:
            return base_vol

    def _get_momentum_skew(self, index: str) -> float:
        """
        Calculate skew adjustment based on recent momentum.

        If market has been trending up, adjust probabilities for
        mean reversion (slight downward bias).
        If market has been trending down, adjust for potential
        continued momentum or bounce.

        Returns: skew factor (-0.1 to +0.1)
        """
        if len(self.recent_returns) < 5:
            return 0.0

        # Calculate recent trend (last 5 periods)
        recent_sum = sum(self.recent_returns[-5:])

        # Strong uptrend -> slight negative skew (mean reversion)
        # Strong downtrend -> slight positive skew (bounce potential)
        # Clamp to reasonable range
        skew = -recent_sum * 0.5  # Dampened mean reversion factor
        return max(-0.1, min(0.1, skew))

    def _get_effective_time(self, hours_to_expiry: float,
                            close_time: Optional[datetime] = None) -> Tuple[float, float]:
        """
        Calculate effective time to expiry accounting for:
        - Market hours vs overnight
        - Weekend effects

        All market hour comparisons use Eastern Time (ET).

        Returns: (effective_hours, vol_multiplier)
        """
        if hours_to_expiry <= 0:
            return 0, 1.0

        # Convert current time to ET for market hours comparison
        now_et = datetime.now(ET)
        current_hour_et = now_et.hour + now_et.minute / 60

        # Check if we're currently outside equity market hours (9:30 AM - 4:00 PM ET)
        market_open_et = 9.5   # 9:30 AM ET
        market_close_et = 16.0  # 4:00 PM ET
        is_weekend = now_et.weekday() >= 5

        is_outside_market_hours = (
            is_weekend or
            current_hour_et < market_open_et or
            current_hour_et >= market_close_et
        )

        if is_outside_market_hours:
            # We're in overnight/weekend period — apply higher vol multiplier
            return hours_to_expiry, self.OVERNIGHT_VOL_MULTIPLIER

        # During market hours: check if the expiry spans overnight
        if close_time:
            close_et = close_time.astimezone(ET) if close_time.tzinfo else close_time
            # If expiry is on a different day or after next morning open,
            # some of the remaining time is overnight
            if close_et.date() > now_et.date():
                return hours_to_expiry, self.OVERNIGHT_VOL_MULTIPLIER

        # During market hours, expiry today — use standard time
        return hours_to_expiry, 1.0

    def _lognormal_range_prob(self, current_price: float,
                               lower_bound: float,
                               upper_bound: float,
                               volatility: float,
                               time_years: float,
                               drift: float = 0.0) -> float:
        """
        Calculate range probability using log-normal distribution.

        This is more accurate than normal distribution because:
        - Prices can't go negative
        - Returns are roughly normally distributed
        - Matches Black-Scholes assumptions

        Args:
            drift: Log-space drift term (skew). Positive = bullish shift.
        """
        if time_years <= 0:
            if lower_bound <= current_price <= upper_bound:
                return 1.0
            return 0.0

        # Log-normal parameters
        sigma = volatility * math.sqrt(time_years)
        mu = drift * math.sqrt(time_years)  # Skew applied as drift in log-space

        if sigma < 0.001:
            if lower_bound <= current_price <= upper_bound:
                return 1.0
            return 0.0

        # Log of bounds relative to current price, shifted by drift
        if upper_bound == float('inf'):
            # Above X market
            if lower_bound <= 0:
                return 1.0
            z_lower = (math.log(lower_bound / current_price) - mu) / sigma
            return 1 - stats.norm.cdf(z_lower)

        if lower_bound <= 0:
            # Below X market
            z_upper = (math.log(upper_bound / current_price) - mu) / sigma
            return stats.norm.cdf(z_upper)

        # Range market
        z_lower = (math.log(lower_bound / current_price) - mu) / sigma
        z_upper = (math.log(upper_bound / current_price) - mu) / sigma

        return stats.norm.cdf(z_upper) - stats.norm.cdf(z_lower)

    def _fat_tail_range_prob(self, current_price: float,
                              lower_bound: float,
                              upper_bound: float,
                              volatility: float,
                              time_years: float,
                              drift: float = 0.0) -> float:
        """
        Calculate range probability using Student's t distribution in LOG-SPACE.

        This accounts for fat tails (extreme moves more likely than normal)
        while maintaining consistency with log-normal model (prices can't go negative).

        Uses the same log-space transformation as _lognormal_range_prob for
        mathematical consistency.

        Args:
            drift: Log-space drift term (skew). Positive = bullish shift.
        """
        if time_years <= 0:
            if lower_bound <= current_price <= upper_bound:
                return 1.0
            return 0.0

        # Use log-space sigma (same as log-normal)
        sigma = volatility * math.sqrt(time_years)
        mu = drift * math.sqrt(time_years)  # Skew applied as drift in log-space

        if sigma < 0.001:
            if lower_bound <= current_price <= upper_bound:
                return 1.0
            return 0.0

        # Scale factor for t-distribution to match normal variance
        # df=5 -> scale = sigma * sqrt(3/5) ≈ 0.775 * sigma
        scale = sigma * math.sqrt((self.TAIL_DF - 2) / self.TAIL_DF)

        if upper_bound == float('inf'):
            if lower_bound <= 0:
                return 1.0
            # Log-space z-score shifted by drift (consistent with log-normal)
            z_lower = (math.log(lower_bound / current_price) - mu) / scale
            return 1 - stats.t.cdf(z_lower, df=self.TAIL_DF)

        if lower_bound <= 0:
            z_upper = (math.log(upper_bound / current_price) - mu) / scale
            return stats.t.cdf(z_upper, df=self.TAIL_DF)

        # Log-space z-scores shifted by drift
        z_lower = (math.log(lower_bound / current_price) - mu) / scale
        z_upper = (math.log(upper_bound / current_price) - mu) / scale

        return stats.t.cdf(z_upper, df=self.TAIL_DF) - stats.t.cdf(z_lower, df=self.TAIL_DF)

    def calculate(self,
                  current_price: float,
                  lower_bound: float,
                  upper_bound: float,
                  hours_to_expiry: float,
                  index: str = "nasdaq",
                  close_time: Optional[datetime] = None) -> FairValueResult:
        """
        Calculate sophisticated fair value for a range market.

        Args:
            current_price: Current futures/index price (or yield for treasury)
            lower_bound: Lower range bound
            upper_bound: Upper range bound (float('inf') for above markets)
            hours_to_expiry: Hours until market closes
            index: "nasdaq", "spx", "treasury10y", "usdjpy", "eurusd"
            close_time: Actual close time (for overnight adjustment)

        Returns:
            FairValueResult with probability and metadata
        """
        index_lower = index.lower()
        asset_class = self.ASSET_CLASS.get(index_lower, "equity")
        vol_source = "default"

        # Get volatility in order of preference:
        # 1. Options-implied volatility (most accurate for short-term)
        # 2. Historical volatility
        # 3. VIX-adjusted base volatility
        # 4. Default base volatility

        if index_lower in self.options_implied_vols and self.options_implied_vols[index_lower]:
            vol = self.options_implied_vols[index_lower]
            vol_source = "options_implied"
        elif index_lower in self.historical_vols and self.historical_vols[index_lower]:
            vol = self.historical_vols[index_lower]
            vol_source = "historical"
        else:
            base_vol = self.BASE_VOLATILITY.get(index_lower, 0.18)
            # Only apply VIX adjustment for equities
            if asset_class == "equity" and self.vix_price:
                vol = self._get_vix_adjusted_vol(base_vol, index_lower)
                vol_source = "vix_scaled"
            else:
                vol = base_vol
                vol_source = "default"

        # Get time adjustment
        effective_hours, vol_mult = self._get_effective_time(hours_to_expiry, close_time)
        vol *= vol_mult

        # Convert to years using asset class-specific trading hours
        hours_per_day = self.TRADING_HOURS_PER_DAY.get(asset_class, 6.5)

        if asset_class == "forex":
            # Forex: 24-hour market, use actual hours
            time_years = hours_to_expiry / (24 * 365)
        else:
            # Equities, yields: use trading hours per day
            trading_days = effective_hours / hours_per_day
            time_years = trading_days / 252

        # Compute skew as drift: prefer options skew, fall back to momentum skew
        # Drift is applied inside the distribution (shifts the log-mean)
        # rather than added to the final probability.
        options_skew_val = self.options_skew.get(index_lower)
        if options_skew_val is not None:
            # Options put-call skew: positive = puts more expensive = downside risk
            # Negative drift because high put premium = bearish
            skew = -options_skew_val * 0.5
        else:
            skew = self._get_momentum_skew(index)

        # Calculate base probability with skew applied as drift
        if self.use_fat_tails:
            prob = self._fat_tail_range_prob(
                current_price, lower_bound, upper_bound, vol, time_years,
                drift=skew
            )
            model_type = "student_t"
        else:
            prob = self._lognormal_range_prob(
                current_price, lower_bound, upper_bound, vol, time_years,
                drift=skew
            )
            model_type = "lognormal"

        # Clamp probability to valid range (handles NaN, infinity, and out-of-bounds)
        if not (0 <= prob <= 1):
            prob = max(0.0, min(1.0, prob)) if not math.isnan(prob) else 0.5
        prob = max(0.01, min(0.99, prob))

        # Calculate confidence interval (Monte Carlo would be better but slow)
        # Rough estimate: +/- 5% of probability
        confidence_range = max(0.02, prob * 0.1)
        conf_low = max(0.01, prob - confidence_range)
        conf_high = min(0.99, prob + confidence_range)

        return FairValueResult(
            probability=prob,
            confidence_low=conf_low,
            confidence_high=conf_high,
            model_vol=vol,
            effective_hours=effective_hours,
            skew_adjustment=skew,
            model_type=model_type,
            vol_source=vol_source,
        )


# Legacy compatible function
def calculate_range_probability_v2(
    current_price: float,
    lower_bound: float,
    upper_bound: float,
    volatility: float,
    hours_to_expiry: float,
    vix_price: Optional[float] = None,
    use_fat_tails: bool = True,
) -> float:
    """
    Calculate probability with sophisticated model.
    Drop-in replacement for calculate_range_probability.
    """
    model = SophisticatedFairValue(
        vix_price=vix_price,
        use_fat_tails=use_fat_tails,
    )

    # Determine index from price (rough heuristic)
    index = "nasdaq" if current_price > 15000 else "spx"

    result = model.calculate(
        current_price=current_price,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        hours_to_expiry=hours_to_expiry,
        index=index,
    )

    return result.probability


# Convenience function matching old interface
def hours_until(close_time: datetime) -> float:
    """Calculate hours until a datetime"""
    if not close_time:
        return 24

    now = datetime.now(timezone.utc)
    if close_time.tzinfo is None:
        close_time = close_time.replace(tzinfo=timezone.utc)

    delta = close_time - now
    return max(0, delta.total_seconds() / 3600)


# Test the model
if __name__ == "__main__":
    print("=== Sophisticated Fair Value Model Test ===\n")

    model = SophisticatedFairValue(vix_price=18, use_fat_tails=True)

    # Test case: NQ at 21,500, various strikes
    nq = 21500
    hours = 8  # 8 hours to expiry

    print(f"NQ Price: {nq}, Hours to expiry: {hours}, VIX: 18\n")

    test_cases = [
        (21000, float('inf'), "Above 21,000"),
        (21500, float('inf'), "Above 21,500 (ATM)"),
        (22000, float('inf'), "Above 22,000"),
        (21400, 21500, "21,400-21,500 range"),
        (21500, 21600, "21,500-21,600 range (ATM)"),
    ]

    for lower, upper, desc in test_cases:
        result = model.calculate(nq, lower, upper, hours, "nasdaq")
        print(f"{desc}:")
        print(f"  Probability: {result.probability*100:.1f}%")
        print(f"  Confidence:  [{result.confidence_low*100:.1f}%, {result.confidence_high*100:.1f}%]")
        print(f"  Model vol:   {result.model_vol*100:.1f}%")
        print(f"  Model type:  {result.model_type}")
        print()
