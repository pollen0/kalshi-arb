"""
Market Discovery Module

Discovers all active markets across different expiration times:
- Equity indices (SPX, NDX): H1000 (10am), H1200 (12pm), H1600 (4pm EST)
- Treasury: EOD only (no time suffix)
- Forex: "10" suffix (10am EST) - NOT "H1000"

Also handles auto-rollover when contracts expire.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Set
from enum import Enum
from zoneinfo import ZoneInfo

from ..core.client import KalshiClient
from ..core.models import Market


class AssetClass(Enum):
    """Asset classes with different expiration patterns"""
    EQUITY = "equity"       # SPX, NDX - H1000, H1200, H1600
    TREASURY = "treasury"   # 10Y - EOD only
    FOREX = "forex"         # EUR/USD, USD/JPY - "10" suffix
    COMMODITY = "commodity" # WTI Crude Oil - EOD only
    CRYPTO = "crypto"       # BTC, ETH, SOL, DOGE, XRP - 15min, hourly, daily


@dataclass
class ExpirationSlot:
    """Represents a market expiration time slot"""
    series_prefix: str      # e.g., "KXINXU", "KXNASDAQ100U"
    event_ticker: str       # Full event ticker e.g., "KXINXU-26FEB04H1200"
    date: datetime          # Date of expiration
    time_suffix: str        # e.g., "H1200", "H1600", "10", ""
    market_count: int = 0   # Number of markets in this slot
    close_time: Optional[datetime] = None  # When markets close

    @property
    def hours_until_expiry(self) -> float:
        """Calculate hours until this slot expires"""
        if not self.close_time:
            return float('inf')
        now = datetime.now(timezone.utc)
        delta = self.close_time - now
        return delta.total_seconds() / 3600

    @property
    def is_expired(self) -> bool:
        """Check if this slot has expired"""
        return self.hours_until_expiry <= 0

    @property
    def is_tradeable(self) -> bool:
        """Check if this slot is tradeable (>5 minutes to expiry)"""
        return self.hours_until_expiry > 0.083  # 5 minutes

    @property
    def display_name(self) -> str:
        """Human-readable name for UI"""
        date_str = self.date.strftime("%b %d")
        if self.time_suffix:
            if self.time_suffix.startswith("H"):
                # H1200 -> 12:00pm, H1600 -> 4:00pm
                hour = int(self.time_suffix[1:3])
                ampm = "am" if hour < 12 else "pm"
                display_hour = hour if hour <= 12 else hour - 12
                time_str = f"{display_hour}:00{ampm}"
            else:
                # "10" -> 10:00am
                time_str = f"{self.time_suffix}:00am"
            return f"{self.series_prefix} {date_str} {time_str}"
        return f"{self.series_prefix} {date_str} EOD"


class MarketDiscovery:
    """
    Discovers and manages all available market expiration slots.

    Scans Kalshi for all active event tickers matching known patterns
    and tracks their expiration times.
    """

    # Time suffixes by asset class
    TIME_PATTERNS: Dict[AssetClass, List[str]] = {
        AssetClass.EQUITY: ["H1000", "H1200", "H1600"],  # 10am, 12pm, 4pm EST
        AssetClass.TREASURY: [""],                        # EOD only
        AssetClass.FOREX: ["10"],                         # 10am EST (note: no "H" prefix)
        AssetClass.COMMODITY: [""],                       # EOD only
        AssetClass.CRYPTO: [""],                          # Dynamic — handled via client.py
    }

    # Series prefix to asset class mapping
    SERIES_ASSET_CLASS: Dict[str, AssetClass] = {
        # Equity - SPX
        "KXINXU": AssetClass.EQUITY,      # SPX Above/Below
        "KXINX": AssetClass.EQUITY,       # SPX Range
        # Equity - NDX
        "KXNASDAQ100U": AssetClass.EQUITY,  # NDX Above/Below
        "KXNASDAQ100": AssetClass.EQUITY,   # NDX Range
        # Treasury
        "KXTNOTED": AssetClass.TREASURY,
        # Forex
        "KXEURUSD": AssetClass.FOREX,
        "KXUSDJPY": AssetClass.FOREX,
        # Commodity
        "KXWTI": AssetClass.COMMODITY,
        "KXWTIW": AssetClass.COMMODITY,
        # Crypto — BTC
        "KXBTC": AssetClass.CRYPTO,
        "KXBTC15M": AssetClass.CRYPTO,
        "KXBTCD": AssetClass.CRYPTO,
        # Crypto — ETH
        "KXETH": AssetClass.CRYPTO,
        "KXETH15M": AssetClass.CRYPTO,
        "KXETHD": AssetClass.CRYPTO,
        # Crypto — SOL
        "KXSOL": AssetClass.CRYPTO,
        "KXSOL15M": AssetClass.CRYPTO,
        "KXSOLD": AssetClass.CRYPTO,
        # Crypto — DOGE
        "KXDOGE": AssetClass.CRYPTO,
        "KXDOGE15M": AssetClass.CRYPTO,
        "KXDOGED": AssetClass.CRYPTO,
        # Crypto — XRP
        "KXXRP": AssetClass.CRYPTO,
        "KXXRP15M": AssetClass.CRYPTO,
        "KXXRPD": AssetClass.CRYPTO,
    }

    # Expiration times in EST (approximate)
    EXPIRATION_TIMES_EST: Dict[str, int] = {
        "H1000": 10,  # 10am EST
        "H1200": 12,  # 12pm EST
        "H1600": 16,  # 4pm EST
        "10": 10,     # 10am EST (forex)
        "": 17,       # 5pm EST (EOD)
    }

    def __init__(self, client: KalshiClient):
        self.client = client
        self._slot_cache: Dict[str, ExpirationSlot] = {}
        self._last_discovery: Optional[datetime] = None
        self._cache_ttl_seconds = 60  # Rediscover every 60 seconds

    def _generate_event_tickers(
        self,
        series_prefix: str,
        days_ahead: int = 3
    ) -> List[str]:
        """
        Generate possible event tickers for a series prefix.

        Args:
            series_prefix: e.g., "KXINXU"
            days_ahead: How many days ahead to check

        Returns:
            List of possible event tickers to probe
        """
        asset_class = self.SERIES_ASSET_CLASS.get(series_prefix)
        if not asset_class:
            return []

        time_patterns = self.TIME_PATTERNS.get(asset_class, [""])

        now = datetime.now(timezone.utc)
        tickers = []

        for day_offset in range(days_ahead + 1):
            date = now + timedelta(days=day_offset)
            date_str = date.strftime("%y%b%d").upper()  # e.g., "26FEB04"

            for time_suffix in time_patterns:
                event_ticker = f"{series_prefix}-{date_str}{time_suffix}"
                tickers.append(event_ticker)

        return tickers

    def _estimate_close_time(
        self,
        date: datetime,
        time_suffix: str
    ) -> datetime:
        """
        Estimate the close time for an expiration slot.

        Args:
            date: The date of expiration
            time_suffix: Time suffix (H1200, H1600, 10, "")

        Returns:
            Estimated close time in UTC
        """
        # Get hour in Eastern Time
        hour_et = self.EXPIRATION_TIMES_EST.get(time_suffix, 17)

        # Create datetime in America/New_York timezone (handles EST/EDT automatically)
        eastern = ZoneInfo("America/New_York")
        close_et = date.replace(
            hour=hour_et,
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=eastern
        )
        # Convert to UTC
        close_utc = close_et.astimezone(timezone.utc)

        return close_utc

    def discover_slot(
        self,
        series_prefix: str,
        event_ticker: str
    ) -> Optional[ExpirationSlot]:
        """
        Discover markets for a specific event ticker and create a slot.

        Args:
            series_prefix: e.g., "KXINXU"
            event_ticker: e.g., "KXINXU-26FEB04H1200"

        Returns:
            ExpirationSlot if markets exist, None otherwise
        """
        try:
            markets = self.client.get_markets(event_ticker=event_ticker)

            if not markets:
                return None

            # Parse date and time suffix from event ticker
            # Format: PREFIX-YYMMMDDHHH or PREFIX-YYMMMDD
            parts = event_ticker.split("-")
            if len(parts) < 2:
                return None

            date_part = parts[1]  # e.g., "26FEB04H1200" or "26FEB04"

            # Extract time suffix
            time_suffix = ""
            for pattern in ["H1000", "H1200", "H1600", "10"]:
                if date_part.endswith(pattern):
                    time_suffix = pattern
                    date_part = date_part[:-len(pattern)]
                    break

            # Parse date
            try:
                # date_part is now "26FEB04"
                date = datetime.strptime(date_part, "%y%b%d")
                date = date.replace(tzinfo=timezone.utc)
            except ValueError:
                # Try alternate formats
                return None

            # Get close time from first market or estimate
            close_time = None
            for m in markets:
                if m.close_time:
                    close_time = m.close_time
                    break

            if not close_time:
                close_time = self._estimate_close_time(date, time_suffix)

            slot = ExpirationSlot(
                series_prefix=series_prefix,
                event_ticker=event_ticker,
                date=date,
                time_suffix=time_suffix,
                market_count=len(markets),
                close_time=close_time,
            )

            return slot

        except Exception as e:
            print(f"[DISCOVERY] Error discovering {event_ticker}: {e}")
            return None

    def discover_all_slots(
        self,
        series_prefixes: List[str] = None,
        days_ahead: int = 3,
        force_refresh: bool = False,
    ) -> List[ExpirationSlot]:
        """
        Find all active expiration slots across all series.

        Args:
            series_prefixes: List of prefixes to check (default: all known)
            days_ahead: How many days ahead to look
            force_refresh: Force rediscovery even if cache is valid

        Returns:
            List of ExpirationSlot objects sorted by expiry time
        """
        # Check cache
        now = datetime.now(timezone.utc)
        if (
            not force_refresh
            and self._last_discovery
            and (now - self._last_discovery).total_seconds() < self._cache_ttl_seconds
        ):
            return list(self._slot_cache.values())

        if series_prefixes is None:
            series_prefixes = list(self.SERIES_ASSET_CLASS.keys())

        discovered_slots: Dict[str, ExpirationSlot] = {}

        for series_prefix in series_prefixes:
            event_tickers = self._generate_event_tickers(series_prefix, days_ahead)

            for event_ticker in event_tickers:
                # Skip if we already discovered this slot
                if event_ticker in discovered_slots:
                    continue

                slot = self.discover_slot(series_prefix, event_ticker)
                if slot and slot.market_count > 0:
                    discovered_slots[event_ticker] = slot
                    print(f"[DISCOVERY] Found {slot.market_count} markets: {slot.display_name}")

        # Update cache
        self._slot_cache = discovered_slots
        self._last_discovery = now

        # Sort by expiry time
        slots = list(discovered_slots.values())
        slots.sort(key=lambda s: s.close_time or datetime.max.replace(tzinfo=timezone.utc))

        return slots

    def get_slots_for_series(self, series_prefix: str) -> List[ExpirationSlot]:
        """
        Get all active slots for a specific series.

        Args:
            series_prefix: e.g., "KXINXU"

        Returns:
            List of slots sorted by expiry time
        """
        if not self._slot_cache:
            self.discover_all_slots()

        slots = [
            slot for slot in self._slot_cache.values()
            if slot.series_prefix == series_prefix and not slot.is_expired
        ]
        slots.sort(key=lambda s: s.close_time or datetime.max.replace(tzinfo=timezone.utc))
        return slots

    def get_next_slot(self, series_prefix: str) -> Optional[ExpirationSlot]:
        """
        Get the next tradeable slot for a series.

        Prefers slots with:
        1. More than 5 minutes until expiry
        2. Soonest expiry time

        Args:
            series_prefix: e.g., "KXINXU"

        Returns:
            Next tradeable ExpirationSlot or None
        """
        slots = self.get_slots_for_series(series_prefix)

        for slot in slots:
            if slot.is_tradeable:
                return slot

        return None

    def get_markets_for_slot(self, slot: ExpirationSlot) -> List[Market]:
        """
        Fetch all markets for a specific expiration slot.

        Args:
            slot: ExpirationSlot to fetch markets for

        Returns:
            List of Market objects
        """
        try:
            return self.client.get_markets(
                event_ticker=slot.event_ticker
            )
        except Exception as e:
            print(f"[DISCOVERY] Error fetching markets for {slot.event_ticker}: {e}")
            return []


class MarketRollover:
    """
    Handles automatic rollover to next contract when current expires.

    Tracks the "active" slot for each series and automatically
    switches to the next slot when:
    - Current slot expires
    - Current slot has less than 5 minutes remaining
    """

    def __init__(self, discovery: MarketDiscovery):
        self.discovery = discovery
        self._active_slots: Dict[str, ExpirationSlot] = {}
        self._rollover_callbacks: List[callable] = []

    def on_rollover(self, callback: callable):
        """
        Register a callback for rollover events.

        Callback signature: (series_prefix: str, old_slot: ExpirationSlot, new_slot: ExpirationSlot)
        """
        self._rollover_callbacks.append(callback)

    def _notify_rollover(
        self,
        series_prefix: str,
        old_slot: Optional[ExpirationSlot],
        new_slot: ExpirationSlot
    ):
        """Notify all callbacks of a rollover event"""
        for callback in self._rollover_callbacks:
            try:
                callback(series_prefix, old_slot, new_slot)
            except Exception as e:
                print(f"[ROLLOVER] Callback error: {e}")

    def get_active_slot(self, series_prefix: str) -> Optional[ExpirationSlot]:
        """
        Get the current active slot for a series.

        If no slot is active, automatically selects the next tradeable slot.

        Args:
            series_prefix: e.g., "KXINXU"

        Returns:
            Current active ExpirationSlot or None
        """
        current = self._active_slots.get(series_prefix)

        # Check if current slot is still valid
        if current and current.is_tradeable:
            return current

        # Need to find a new slot
        return self.check_and_rollover(series_prefix)

    def check_and_rollover(self, series_prefix: str) -> Optional[ExpirationSlot]:
        """
        Check if current slot is expired/expiring and rollover to next.

        Args:
            series_prefix: e.g., "KXINXU"

        Returns:
            The new active slot if rollover occurred, else current slot
        """
        current = self._active_slots.get(series_prefix)

        # Check if we need to rollover
        needs_rollover = (
            current is None or
            not current.is_tradeable
        )

        if not needs_rollover:
            return current

        # Find next available slot
        next_slot = self.discovery.get_next_slot(series_prefix)

        if next_slot:
            old_slot = current
            self._active_slots[series_prefix] = next_slot

            # Log rollover
            if old_slot:
                print(f"[ROLLOVER] {series_prefix}: {old_slot.event_ticker} -> {next_slot.event_ticker}")
            else:
                print(f"[ROLLOVER] {series_prefix}: None -> {next_slot.event_ticker}")

            # Notify callbacks
            self._notify_rollover(series_prefix, old_slot, next_slot)

            return next_slot

        # No slots available
        if series_prefix in self._active_slots:
            del self._active_slots[series_prefix]

        return None

    def get_all_active_slots(self) -> Dict[str, ExpirationSlot]:
        """Get all currently active slots"""
        return dict(self._active_slots)

    def get_tradeable_slots(self) -> List[ExpirationSlot]:
        """
        Get all slots that are currently tradeable across all series.

        Returns:
            List of tradeable ExpirationSlots sorted by expiry time
        """
        all_slots = self.discovery.discover_all_slots()
        tradeable = [slot for slot in all_slots if slot.is_tradeable]
        return tradeable

    def set_active_slot(self, series_prefix: str, slot: ExpirationSlot):
        """
        Manually set the active slot for a series.

        Used when user wants to trade a specific expiration.

        Args:
            series_prefix: e.g., "KXINXU"
            slot: The slot to make active
        """
        old_slot = self._active_slots.get(series_prefix)
        self._active_slots[series_prefix] = slot

        if old_slot != slot:
            print(f"[ROLLOVER] Manual switch {series_prefix}: {slot.event_ticker}")
            self._notify_rollover(series_prefix, old_slot, slot)


# Convenience functions

def get_all_expiration_slots(client: KalshiClient) -> List[ExpirationSlot]:
    """
    Quick function to discover all expiration slots.

    Args:
        client: KalshiClient instance

    Returns:
        List of all active ExpirationSlots
    """
    discovery = MarketDiscovery(client)
    return discovery.discover_all_slots()


def get_markets_by_expiration(
    client: KalshiClient,
    series_prefix: str,
    time_suffix: str = None,
) -> List[Market]:
    """
    Get markets for a series with optional time filter.

    Args:
        client: KalshiClient instance
        series_prefix: e.g., "KXINXU"
        time_suffix: Optional time suffix filter (e.g., "H1200")

    Returns:
        List of markets
    """
    discovery = MarketDiscovery(client)
    slots = discovery.get_slots_for_series(series_prefix)

    if time_suffix:
        slots = [s for s in slots if s.time_suffix == time_suffix]

    all_markets = []
    for slot in slots:
        markets = discovery.get_markets_for_slot(slot)
        all_markets.extend(markets)

    return all_markets
