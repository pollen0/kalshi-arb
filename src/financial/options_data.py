"""
Options Data Client for Implied Volatility and Risk-Neutral Probabilities

Fetches options chains from Yahoo Finance to extract:
1. ATM implied volatility
2. Put-call skew
3. Risk-neutral probability distribution implied by option prices

This provides much better fair value estimates than historical volatility alone.
"""

import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, Tuple, List
import yfinance as yf
from scipy import stats
from scipy.optimize import brentq
import numpy as np
import pandas as pd


def safe_float(val, default=0.0) -> float:
    """Safely convert value to float, handling NaN and None"""
    if val is None:
        return default
    if isinstance(val, float) and math.isnan(val):
        return default
    if pd.isna(val):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0) -> int:
    """Safely convert value to int, handling NaN and None"""
    return int(safe_float(val, float(default)))


@dataclass
class OptionQuote:
    """Single option quote"""
    strike: float
    expiry: datetime
    option_type: str  # "call" or "put"
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    implied_vol: Optional[float] = None


@dataclass
class OptionsSnapshot:
    """Complete options chain snapshot"""
    underlying_symbol: str
    underlying_price: float
    timestamp: datetime
    expiry: datetime
    time_to_expiry_years: float

    calls: List[OptionQuote] = field(default_factory=list)
    puts: List[OptionQuote] = field(default_factory=list)

    # Derived metrics
    atm_call_iv: Optional[float] = None
    atm_put_iv: Optional[float] = None
    atm_iv: Optional[float] = None  # Average of call and put
    put_call_skew: Optional[float] = None  # Put IV - Call IV (usually positive)

    # 25-delta skew (more meaningful measure)
    skew_25delta: Optional[float] = None

    # Risk-neutral probability distribution
    implied_probabilities: dict = field(default_factory=dict)  # strike -> probability


class OptionsDataClient:
    """
    Fetches and analyzes options data for implied volatility and probabilities.

    Uses Yahoo Finance which provides delayed but free options data.
    For production, consider upgrading to real-time options feed.
    """

    # Underlying symbols for options
    SYMBOLS = {
        "nasdaq": "QQQ",    # Most liquid NASDAQ-100 options
        "spx": "SPY",       # Most liquid S&P 500 options
        "ndx_index": "^NDX",  # For index-level reference
        "spx_index": "^GSPC", # For index-level reference
    }

    # Approximate scale factor: index_price / etf_price
    # SPY is ~1/10 of SPX, QQQ is ~1/40 of NDX
    # Used to convert index-level bounds to ETF-level strikes
    INDEX_TO_ETF_SCALE = {
        "nasdaq": 1 / 40,   # QQQ ≈ NDX / 40
        "spx": 1 / 10,      # SPY ≈ SPX / 10
    }

    # Risk-free rate assumption (update periodically)
    RISK_FREE_RATE = 0.05  # 5% as of 2024

    def __init__(self, cache_ttl: float = 60.0):
        """
        Initialize options client.

        Args:
            cache_ttl: Cache time-to-live in seconds (options don't need ultra-fast updates)
        """
        self._cache = {}
        self._cache_ttl = cache_ttl
        self._iv_surface_cache = {}

    def _black_scholes_price(self, S: float, K: float, T: float, r: float,
                              sigma: float, option_type: str) -> float:
        """Calculate Black-Scholes option price"""
        if T <= 0 or sigma <= 0:
            # At expiry, return intrinsic value
            if option_type == "call":
                return max(0, S - K)
            else:
                return max(0, K - S)

        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        if option_type == "call":
            price = S * stats.norm.cdf(d1) - K * math.exp(-r * T) * stats.norm.cdf(d2)
        else:
            price = K * math.exp(-r * T) * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)

        return price

    def _implied_vol_from_price(self, S: float, K: float, T: float, r: float,
                                 market_price: float, option_type: str) -> Optional[float]:
        """Calculate implied volatility from option price using Brent's method"""
        if T <= 0 or market_price <= 0:
            return None

        # Intrinsic value check
        if option_type == "call":
            intrinsic = max(0, S - K)
        else:
            intrinsic = max(0, K - S)

        if market_price < intrinsic:
            return None

        def objective(sigma):
            return self._black_scholes_price(S, K, T, r, sigma, option_type) - market_price

        try:
            # Search between 1% and 500% volatility
            iv = brentq(objective, 0.01, 5.0, xtol=1e-6)
            return iv
        except (ValueError, RuntimeError):
            return None

    def _get_nearest_expiry(self, ticker: yf.Ticker, target_hours: float = 8.0) -> Optional[str]:
        """Get the nearest options expiry, preferring 0DTE if available"""
        try:
            expiries = ticker.options
            if not expiries:
                return None

            now = datetime.now(timezone.utc)
            target_time = now + timedelta(hours=target_hours)

            best_expiry = None
            best_diff = float('inf')

            for exp_str in expiries:
                try:
                    # Parse expiry date (format: YYYY-MM-DD)
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d")
                    # Set to market close (4 PM ET = 21:00 UTC)
                    exp_datetime = exp_date.replace(hour=21, minute=0, tzinfo=timezone.utc)

                    # Skip expired options
                    if exp_datetime < now:
                        continue

                    diff = abs((exp_datetime - target_time).total_seconds())
                    if diff < best_diff:
                        best_diff = diff
                        best_expiry = exp_str

                except ValueError:
                    continue

            return best_expiry

        except Exception as e:
            print(f"[OPTIONS] Error getting expiries: {e}")
            return None

    def get_options_snapshot(self, index: str, hours_to_expiry: float = 8.0) -> Optional[OptionsSnapshot]:
        """
        Get options chain snapshot for an index.

        Args:
            index: "nasdaq" or "spx"
            hours_to_expiry: Target hours to expiry (finds nearest expiry)

        Returns:
            OptionsSnapshot with all derived metrics
        """
        symbol = self.SYMBOLS.get(index.lower())
        if not symbol:
            return None

        # Check cache
        cache_key = f"{symbol}_{hours_to_expiry}"
        if cache_key in self._cache:
            snapshot, ts = self._cache[cache_key]
            if (datetime.now(timezone.utc) - ts).total_seconds() < self._cache_ttl:
                return snapshot

        try:
            ticker = yf.Ticker(symbol)

            # Get underlying price
            info = ticker.fast_info
            spot_price = info.get('lastPrice') or info.get('regularMarketPrice')
            if not spot_price:
                print(f"[OPTIONS] Could not get spot price for {symbol}")
                return None

            # Get nearest expiry
            expiry_str = self._get_nearest_expiry(ticker, hours_to_expiry)
            if not expiry_str:
                print(f"[OPTIONS] No valid expiry found for {symbol}")
                return None

            # Parse expiry
            exp_date = datetime.strptime(expiry_str, "%Y-%m-%d")
            exp_datetime = exp_date.replace(hour=21, minute=0, tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)
            time_to_expiry = (exp_datetime - now).total_seconds() / (365.25 * 24 * 3600)

            if time_to_expiry <= 0:
                return None

            # Get options chain
            chain = ticker.option_chain(expiry_str)

            calls = []
            puts = []

            # Process calls
            for _, row in chain.calls.iterrows():
                strike = safe_float(row.get('strike', 0))
                bid = safe_float(row.get('bid', 0))
                ask = safe_float(row.get('ask', 0))
                last = safe_float(row.get('lastPrice', 0))
                volume = safe_int(row.get('volume', 0))
                oi = safe_int(row.get('openInterest', 0))

                # Calculate mid price for IV calculation
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last

                iv = None
                if mid > 0 and strike > 0:
                    iv = self._implied_vol_from_price(
                        spot_price, strike, time_to_expiry,
                        self.RISK_FREE_RATE, mid, "call"
                    )

                calls.append(OptionQuote(
                    strike=strike,
                    expiry=exp_datetime,
                    option_type="call",
                    bid=bid,
                    ask=ask,
                    last=last,
                    volume=volume,
                    open_interest=oi,
                    implied_vol=iv,
                ))

            # Process puts
            for _, row in chain.puts.iterrows():
                strike = safe_float(row.get('strike', 0))
                bid = safe_float(row.get('bid', 0))
                ask = safe_float(row.get('ask', 0))
                last = safe_float(row.get('lastPrice', 0))
                volume = safe_int(row.get('volume', 0))
                oi = safe_int(row.get('openInterest', 0))

                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last

                iv = None
                if mid > 0 and strike > 0:
                    iv = self._implied_vol_from_price(
                        spot_price, strike, time_to_expiry,
                        self.RISK_FREE_RATE, mid, "put"
                    )

                puts.append(OptionQuote(
                    strike=strike,
                    expiry=exp_datetime,
                    option_type="put",
                    bid=bid,
                    ask=ask,
                    last=last,
                    volume=volume,
                    open_interest=oi,
                    implied_vol=iv,
                ))

            # Create snapshot
            snapshot = OptionsSnapshot(
                underlying_symbol=symbol,
                underlying_price=spot_price,
                timestamp=now,
                expiry=exp_datetime,
                time_to_expiry_years=time_to_expiry,
                calls=calls,
                puts=puts,
            )

            # Calculate ATM implied volatility
            self._calculate_atm_iv(snapshot)

            # Calculate skew
            self._calculate_skew(snapshot)

            # Calculate implied probability distribution
            self._calculate_implied_distribution(snapshot)

            # Cache result
            self._cache[cache_key] = (snapshot, datetime.now(timezone.utc))

            return snapshot

        except Exception as e:
            print(f"[OPTIONS] Error fetching options for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _calculate_atm_iv(self, snapshot: OptionsSnapshot):
        """Calculate ATM implied volatility from nearest strikes"""
        spot = snapshot.underlying_price

        # Find ATM strikes (within 1% of spot)
        atm_tolerance = spot * 0.01

        atm_call_ivs = []
        atm_put_ivs = []

        for call in snapshot.calls:
            if call.implied_vol and abs(call.strike - spot) < atm_tolerance:
                if call.volume > 0 or call.open_interest > 100:
                    atm_call_ivs.append(call.implied_vol)

        for put in snapshot.puts:
            if put.implied_vol and abs(put.strike - spot) < atm_tolerance:
                if put.volume > 0 or put.open_interest > 100:
                    atm_put_ivs.append(put.implied_vol)

        if atm_call_ivs:
            snapshot.atm_call_iv = sum(atm_call_ivs) / len(atm_call_ivs)
        if atm_put_ivs:
            snapshot.atm_put_iv = sum(atm_put_ivs) / len(atm_put_ivs)

        if snapshot.atm_call_iv and snapshot.atm_put_iv:
            snapshot.atm_iv = (snapshot.atm_call_iv + snapshot.atm_put_iv) / 2
        elif snapshot.atm_call_iv:
            snapshot.atm_iv = snapshot.atm_call_iv
        elif snapshot.atm_put_iv:
            snapshot.atm_iv = snapshot.atm_put_iv

    def _calculate_skew(self, snapshot: OptionsSnapshot):
        """Calculate put-call skew (measures crash risk premium)"""
        # ATM skew
        if snapshot.atm_put_iv and snapshot.atm_call_iv:
            snapshot.put_call_skew = snapshot.atm_put_iv - snapshot.atm_call_iv

        # 25-delta skew (OTM put IV - OTM call IV)
        # Approximate 25-delta as 5% OTM
        spot = snapshot.underlying_price
        otm_range = spot * 0.05

        otm_put_ivs = []
        otm_call_ivs = []

        for put in snapshot.puts:
            if put.implied_vol and (spot - otm_range * 2) < put.strike < (spot - otm_range):
                if put.volume > 0 or put.open_interest > 100:
                    otm_put_ivs.append(put.implied_vol)

        for call in snapshot.calls:
            if call.implied_vol and (spot + otm_range) < call.strike < (spot + otm_range * 2):
                if call.volume > 0 or call.open_interest > 100:
                    otm_call_ivs.append(call.implied_vol)

        if otm_put_ivs and otm_call_ivs:
            avg_otm_put = sum(otm_put_ivs) / len(otm_put_ivs)
            avg_otm_call = sum(otm_call_ivs) / len(otm_call_ivs)
            snapshot.skew_25delta = avg_otm_put - avg_otm_call

    def _calculate_implied_distribution(self, snapshot: OptionsSnapshot):
        """
        Calculate risk-neutral probability distribution from option prices.

        Uses the Breeden-Litzenberger result: the second derivative of call prices
        with respect to strike gives the risk-neutral density.

        For practical purposes, we approximate using butterfly spreads.
        """
        calls_by_strike = {c.strike: c for c in snapshot.calls if c.implied_vol}

        if len(calls_by_strike) < 5:
            return

        strikes = sorted(calls_by_strike.keys())
        spot = snapshot.underlying_price
        T = snapshot.time_to_expiry_years
        r = self.RISK_FREE_RATE

        # Calculate probability for each strike range
        probabilities = {}

        for i in range(1, len(strikes) - 1):
            K_low = strikes[i - 1]
            K_mid = strikes[i]
            K_high = strikes[i + 1]

            # Get call prices
            c_low = calls_by_strike[K_low]
            c_mid = calls_by_strike[K_mid]
            c_high = calls_by_strike[K_high]

            # Use mid prices
            p_low = (c_low.bid + c_low.ask) / 2 if c_low.bid > 0 else c_low.last
            p_mid = (c_mid.bid + c_mid.ask) / 2 if c_mid.bid > 0 else c_mid.last
            p_high = (c_high.bid + c_high.ask) / 2 if c_high.bid > 0 else c_high.last

            if p_low <= 0 or p_mid <= 0 or p_high <= 0:
                continue

            # Butterfly spread value approximates probability density
            # Probability ≈ e^(rT) * (C(K-) - 2*C(K) + C(K+)) / (dK)^2 * dK
            dK = (K_high - K_low) / 2
            butterfly_value = p_low - 2 * p_mid + p_high

            if butterfly_value > 0:
                prob_density = math.exp(r * T) * butterfly_value / (dK ** 2)
                prob = prob_density * dK  # Probability in this strike range

                # Store probability for the range around this strike
                probabilities[K_mid] = min(1.0, max(0.0, prob))

        # Normalize probabilities to sum to 1 (approximately)
        total_prob = sum(probabilities.values())
        if total_prob > 0:
            probabilities = {k: v / total_prob for k, v in probabilities.items()}

        snapshot.implied_probabilities = probabilities

    def get_implied_range_probability(self, index: str, lower_bound: float,
                                       upper_bound: float, hours_to_expiry: float) -> Optional[float]:
        """
        Get implied probability of price being in range using options data.

        This uses the risk-neutral distribution implied by option prices,
        which is more accurate than historical volatility models.

        IMPORTANT: lower_bound and upper_bound are in index-level terms
        (e.g., SPX ~5500, NDX ~20000), but options data uses ETF-level
        strikes (SPY ~550, QQQ ~500). We scale bounds to ETF level before
        comparing, or dynamically compute the ratio from the snapshot.
        """
        snapshot = self.get_options_snapshot(index, hours_to_expiry)
        if not snapshot or not snapshot.implied_probabilities:
            return None

        # Scale index-level bounds to ETF-level strikes.
        # Use the static approximate ratio, but if the snapshot has a spot
        # price we can sanity-check / dynamically adjust.
        scale = self.INDEX_TO_ETF_SCALE.get(index.lower(), 1.0)

        # Dynamic calibration: if the caller's bounds look like they're
        # already at ETF level (within 2x of the ETF spot), don't rescale.
        spot = snapshot.underlying_price
        test_bound = lower_bound if lower_bound > 0 else upper_bound
        if test_bound != float('inf') and spot > 0:
            ratio = test_bound / spot
            if 0.5 < ratio < 2.0:
                # Bounds already appear to be at ETF level — no scaling needed
                scale = 1.0

        scaled_lower = lower_bound * scale
        scaled_upper = upper_bound * scale if upper_bound != float('inf') else float('inf')

        # Sum probabilities for strikes in the scaled range
        total_prob = 0
        if scaled_upper == float('inf'):
            # "Above X" market: sum all probabilities at or above lower_bound
            for strike, prob in snapshot.implied_probabilities.items():
                if strike >= scaled_lower:
                    total_prob += prob
        else:
            # Range or "below X" market: sum probabilities in [lower, upper]
            for strike, prob in snapshot.implied_probabilities.items():
                if scaled_lower <= strike <= scaled_upper:
                    total_prob += prob

        return total_prob if total_prob > 0 else None

    def get_atm_implied_vol(self, index: str, hours_to_expiry: float = 8.0) -> Optional[float]:
        """Get ATM implied volatility for an index"""
        snapshot = self.get_options_snapshot(index, hours_to_expiry)
        return snapshot.atm_iv if snapshot else None

    def get_skew(self, index: str, hours_to_expiry: float = 8.0) -> Optional[float]:
        """Get put-call skew (positive = puts more expensive = crash risk premium)"""
        snapshot = self.get_options_snapshot(index, hours_to_expiry)
        return snapshot.put_call_skew if snapshot else None

    def get_volatility_for_model(self, index: str, hours_to_expiry: float = 8.0) -> Tuple[Optional[float], str]:
        """
        Get best available volatility estimate and its source.

        Returns:
            (volatility, source) tuple where source is "options_implied", "vix", etc.
        """
        # Try options-implied first
        snapshot = self.get_options_snapshot(index, hours_to_expiry)
        if snapshot and snapshot.atm_iv:
            return (snapshot.atm_iv, "options_implied")

        return (None, "none")


# Global client instance
_options_client: Optional[OptionsDataClient] = None


def get_options_client() -> OptionsDataClient:
    """Get or create global options data client"""
    global _options_client
    if _options_client is None:
        _options_client = OptionsDataClient()
    return _options_client


# Test code
if __name__ == "__main__":
    print("=== Options Data Client Test ===\n")

    client = OptionsDataClient()

    for index in ["nasdaq", "spx"]:
        print(f"\n--- {index.upper()} ---")
        snapshot = client.get_options_snapshot(index, hours_to_expiry=8.0)

        if snapshot:
            print(f"Underlying: {snapshot.underlying_symbol} @ ${snapshot.underlying_price:.2f}")
            print(f"Expiry: {snapshot.expiry}")
            print(f"Time to expiry: {snapshot.time_to_expiry_years * 365:.1f} days")
            print(f"ATM IV (call): {snapshot.atm_call_iv * 100:.1f}%" if snapshot.atm_call_iv else "ATM Call IV: N/A")
            print(f"ATM IV (put):  {snapshot.atm_put_iv * 100:.1f}%" if snapshot.atm_put_iv else "ATM Put IV: N/A")
            print(f"ATM IV (avg):  {snapshot.atm_iv * 100:.1f}%" if snapshot.atm_iv else "ATM IV: N/A")
            print(f"Put-Call Skew: {snapshot.put_call_skew * 100:+.2f}%" if snapshot.put_call_skew else "Skew: N/A")
            print(f"25-Delta Skew: {snapshot.skew_25delta * 100:+.2f}%" if snapshot.skew_25delta else "25D Skew: N/A")
            print(f"Implied distribution points: {len(snapshot.implied_probabilities)}")
        else:
            print("Failed to get options snapshot")
