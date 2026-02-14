"""
Auto-Trading System

Automatically places and manages limit orders based on:
- Fair value from futures
- Orderbook liquidity
- Dynamic price adjustments
- Learned fill probabilities from historical data
"""

import json
import os
import time
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, List
from ..core.client import KalshiClient
from ..core.models import Market, Orderbook, Side, Position
from .fill_tracker import FillTracker, get_tracker
from .risk_manager import RiskManager, get_risk_manager
from .position_manager import PositionManager, get_position_manager

# Persistent daily loss tracking file
_DAILY_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "daily_state.json")


class ErrorType(Enum):
    """Categories of errors for tracking"""
    NETWORK = "network"           # Connection/timeout errors
    API_REJECTION = "api_rejection"  # Kalshi rejected the request
    RATE_LIMIT = "rate_limit"     # Hit rate limits
    VALIDATION = "validation"     # Bad data/validation errors
    ORDER_FAILED = "order_failed" # Order placement failed
    CANCEL_FAILED = "cancel_failed"  # Cancel operation failed
    UNKNOWN = "unknown"           # Uncategorized errors


@dataclass
class ErrorRecord:
    """Record of a single error"""
    timestamp: datetime
    error_type: ErrorType
    message: str
    ticker: Optional[str] = None


@dataclass
class ActiveOrder:
    """Tracks an active limit order"""
    order_id: str
    ticker: str
    side: str  # "yes" or "no"
    price: int
    size: int  # Original order size
    fair_value_at_placement: float
    placed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    filled_count: int = 0  # How many contracts have been filled (for partial fill tracking)
    remaining_count: int = 0  # How many are still resting (updated on sync)
    is_exit: bool = False  # True if this is an exit/sell order (excluded from fill rate breaker)

    def __post_init__(self):
        # Initialize remaining_count to full size if not set
        if self.remaining_count == 0:
            self.remaining_count = self.size


@dataclass
class TraderConfig:
    """Configuration for auto-trader"""
    min_edge: float = 0.02           # 2% minimum edge to place order
    min_edge_to_keep: float = 0.005  # Cancel if edge drops BELOW this (0.5%)
    position_size: int = 10          # Base contracts per order
    max_positions_per_market: int = 1  # Only 1 order per market at a time
    max_total_orders: int = 100      # Max concurrent orders
    max_capital_pct: float = 0.8     # Use max 80% of balance for orders + positions
    price_buffer_cents: int = 1      # Place X cents below fair value
    rebalance_threshold: float = 0.005  # Rebalance if fair value moves 0.5%
    min_volume: int = 0              # Minimum market volume to trade (0 = allow all, for MM mode)
    min_volume_tight_spread: int = 10  # Minimum volume for tight-spread mode (must have activity)
    max_fair_value_extreme: float = 0.97  # Skip if fair value > 97% or < 3%
    min_price_cents: int = 3         # Don't place orders below 3c
    price_adjust_threshold: int = 2  # Adjust order if optimal price differs by 2c+

    # Position-aware sizing
    max_position_per_market: int = 100   # Max total contracts per market
    position_scale_down: bool = True     # Reduce order size as position grows
    max_position_pct_per_market: float = 0.10  # Max 10% of capital in any single market

    # MARKET MAKING MODE (for wide-spread markets)
    market_making_enabled: bool = True   # Enable market making on wide spreads
    market_making_min_spread: int = 10   # Min spread (ask-bid) to trigger market making (lowered to include range markets)
    market_making_edge_buffer: float = 0.12  # Place bid at 12% discount from FV (tighter than retail)
    market_making_edge_cents: int = 6       # Absolute cap: max 6c discount from FV
    market_making_max_fv: float = 0.92   # Allow market making up to 92% FV (on that side)
    market_making_min_fv: float = 0.03   # Allow market making down to 3% FV

    # SAFETY LIMITS
    # max_daily_loss_pct is now sourced from RiskConfig (default 5%) for consistency.
    # Do NOT override here — use RiskConfig to change the limit.
    max_daily_loss_pct: Optional[float] = None      # Will be loaded from RiskConfig in __init__
    min_balance: float = 50.0            # Stop if balance below $50
    sanity_check_prices: bool = True     # Validate fair values before trading

    # ORDER MANAGEMENT
    max_order_age_seconds: int = 180     # Cancel orders older than 3 minutes (stay fresher than retail stale orders)

    # DATA QUALITY
    data_delay_buffer: float = 0.002     # Extra 0.2% edge for data delay (real-time feed)

    # TRANSACTION COSTS
    # These are subtracted from calculated edge before deciding to trade.
    # Kalshi removed trading fees in 2024, but we still model friction:
    #   - entry_fee_pct: fee on entry (currently 0 on Kalshi)
    #   - exit_cost_pct: estimated cost to exit a position (half-spread slippage)
    #   - total round-trip cost = entry_fee_pct + exit_cost_pct
    entry_fee_pct: float = 0.0           # Kalshi entry fee as fraction (0 = no fee)
    exit_cost_pct: float = 0.01          # Estimated exit slippage (1% = ~1c, conservative since most settle)

    # ERROR TRACKING (with time decay)
    error_window_seconds: int = 60       # Time window for error counting
    max_errors_in_window: int = 50       # Max total errors in window before halt
    max_network_errors: int = 10         # Max network errors in window
    max_rate_limit_errors: int = 5       # Max rate limit errors in window (more serious)
    max_order_failures: int = 30         # Max order placement failures in window (rejections are expected)

    # FAIR VALUE VALIDATION
    fv_deviation_warn_threshold: float = 0.20   # Warn if FV deviates >20% from market mid
    fv_deviation_reject_threshold: float = 0.40 # Reject if FV deviates >40% from market mid

    # NEAR-EXPIRY TRADING
    near_expiry_min_minutes: int = 3     # Allow trading up to 3 min before expiry (down from 5)

    # VIX-BASED ORDER SCALING
    vix_scale_orders: bool = True        # Scale max orders with VIX
    vix_high_threshold: float = 25.0     # VIX > 25 → 1.5x orders
    vix_extreme_threshold: float = 35.0  # VIX > 35 → 2.0x orders

    # EVENT TRADING
    trade_through_events: bool = True    # Continue trading during FOMC/CPI (vol boost handles risk)

    # DRY RUN MODE
    dry_run: bool = False                # If True, log orders but don't execute


@dataclass
class PositionInfo:
    """Current position in a market"""
    ticker: str
    side: str       # "yes" or "no"
    quantity: int   # Contracts held
    avg_price: float  # Average entry price (0-1)
    pnl: float = 0  # Realized + unrealized P&L
    fv_at_entry: Optional[float] = None  # Fair value when position was first opened


class AutoTrader:
    """
    Automated trading system that:
    1. Places limit orders when edge exists
    2. Cancels orders when edge disappears
    3. Adjusts orders when underlying price moves
    4. Respects position limits and capital constraints
    5. Learns fill probabilities from historical data
    6. Position-aware order sizing (reduces size as exposure grows)
    """

    def __init__(self, client: KalshiClient, config: TraderConfig = None,
                 tracker: FillTracker = None, risk_manager: RiskManager = None,
                 position_manager: PositionManager = None):
        self.client = client
        self.config = config or TraderConfig()
        self.tracker = tracker or get_tracker()
        self.risk_manager = risk_manager or get_risk_manager()
        self.position_manager = position_manager or get_position_manager()

        # Unify risk limits from RiskConfig (single source of truth)
        from ..core.config import get_risk_config
        risk_config = get_risk_config()
        if self.config.max_daily_loss_pct is None:
            self.config.max_daily_loss_pct = risk_config.max_daily_loss_pct
        self.active_orders: dict[str, ActiveOrder] = {}  # order_id -> ActiveOrder
        self.market_orders: dict[str, str] = {}  # ticker -> order_id (1 per market)
        self.market_making_orders: dict[str, dict[str, str]] = {}  # ticker -> {"yes": order_id, "no": order_id}
        self.running = False
        self._lock = threading.Lock()

        # Cache markets for fill detection
        self._markets_cache: dict[str, Market] = {}

        # Position tracking
        self.positions: dict[str, PositionInfo] = {}  # ticker -> PositionInfo
        self._last_position_sync = datetime.min.replace(tzinfo=timezone.utc)
        self._last_exit_check = datetime.min.replace(tzinfo=timezone.utc)
        self._pending_exits: dict[str, dict] = {}  # ticker -> exit escalation info

        # Safety state — load persisted daily state so restarts can't bypass loss limits
        self._starting_balance: Optional[float] = None
        self._starting_date: Optional[str] = None
        self._last_known_balance: Optional[float] = None
        self._last_balance_time: Optional[datetime] = None  # When balance was last successfully fetched
        self._consecutive_balance_failures: int = 0
        self._peak_equity: Optional[float] = None  # Track peak for drawdown limit
        self._max_drawdown_pct: float = risk_config.max_drawdown_pct
        self._halted = False
        self._halt_reason = ""
        self._load_daily_state()

        # Error tracking with time decay (deque of ErrorRecords)
        self._error_history: deque[ErrorRecord] = deque(maxlen=100)  # Keep last 100 errors

        # Fill rate circuit breaker: >5 ENTRY fills in 60s = pause 120s
        # Exit fills are tracked separately and do NOT trigger the breaker
        self._fill_timestamps: deque[float] = deque(maxlen=50)       # Entry fills only
        self._exit_fill_timestamps: deque[float] = deque(maxlen=50)  # Exit fills (informational)
        self._fill_pause_until: Optional[float] = None

        # VIX price for dynamic order scaling (set by app.py from futures data)
        self._vix_price: Optional[float] = None

        # Balance cache per trading loop iteration (avoids multiple API calls per loop)
        self._loop_balance: Optional[float] = None

        # Diagnostic logging timer
        self._last_diag_log: datetime = datetime.min.replace(tzinfo=timezone.utc)

        # Closed markets cache — skip tickers that returned "market_closed" to avoid
        # burning through the order failure budget on markets that are legitimately closed.
        # Cleared every 10 minutes so reopened markets get retried.
        self._closed_markets: set[str] = set()
        self._closed_markets_cleared_at: float = time.time()

    def _load_daily_state(self):
        """Load persisted daily starting equity and peak equity (prevents loss-limit bypass on restart)."""
        try:
            state_file = os.path.abspath(_DAILY_STATE_FILE)
            if os.path.exists(state_file):
                with open(state_file, 'r') as f:
                    data = json.load(f)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if data.get("date") == today:
                    self._starting_balance = data["starting_equity"]
                    self._starting_date = data["date"]
                    print(f"[SAFETY] Loaded persisted daily state: starting equity=${data['starting_equity']:.2f} from {today}")
                    # Peak equity resets daily — drawdown is measured within the day
                    if data.get("peak_equity"):
                        self._peak_equity = data["peak_equity"]
                        print(f"[SAFETY] Loaded peak equity: ${self._peak_equity:.2f}")
                else:
                    print(f"[SAFETY] Persisted state is from {data.get('date')}, today is {today} — will reset")
        except Exception as e:
            print(f"[SAFETY] Could not load daily state: {e}")

    def _save_daily_state(self):
        """Persist daily starting equity and peak equity to disk (atomic write)."""
        if self._starting_balance is None:
            return
        try:
            state_file = os.path.abspath(_DAILY_STATE_FILE)
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            data = {
                "date": today,
                "starting_equity": self._starting_balance,
                "peak_equity": self._peak_equity,
            }
            tmp_file = state_file + ".tmp"
            with open(tmp_file, 'w') as f:
                json.dump(data, f)
            os.replace(tmp_file, state_file)  # Atomic on POSIX
        except Exception as e:
            print(f"[SAFETY] Could not save daily state: {e}")

    def sync_positions(self):
        """Fetch current positions from Kalshi"""
        try:
            raw_positions = self.client.get_positions()
            if raw_positions is None:
                print("[SYNC] Position fetch failed — keeping previous state")
                return

            with self._lock:
                # Preserve fv_at_entry from existing positions before clearing
                old_fv_at_entry = {
                    t: p.fv_at_entry for t, p in self.positions.items()
                    if p.fv_at_entry is not None
                }
                self.positions.clear()
                for pos in raw_positions:
                    ticker = pos.ticker
                    self.positions[ticker] = PositionInfo(
                        ticker=ticker,
                        side=pos.side.value if hasattr(pos.side, 'value') else str(pos.side),
                        quantity=pos.quantity,
                        avg_price=pos.avg_price,
                        pnl=pos.realized_pnl + (pos.unrealized_pnl or 0),
                        fv_at_entry=old_fv_at_entry.get(ticker),
                    )
                # Update timestamp inside lock to prevent race conditions
                self._last_position_sync = datetime.now(timezone.utc)

        except Exception as e:
            self.record_error(e, message="Sync positions")

    def get_position(self, ticker: str) -> Optional[PositionInfo]:
        """Get current position for a market"""
        with self._lock:
            return self.positions.get(ticker)

    def _update_position_on_fill(self, ticker: str, side: str, fill_qty: int, fill_price_cents: int):
        """Optimistically update local position tracking when a fill is detected.

        Called from sync_orders so that position-dependent logic (sizing, exits)
        sees the latest state immediately, rather than waiting for the next
        sync_positions call (which may be up to 60s later).

        Must be called with self._lock held.
        """
        pos = self.positions.get(ticker)
        fill_price = fill_price_cents / 100.0

        # Look up current fair value for fv_at_entry tracking
        current_fv = None
        cached_market = self._markets_cache.get(ticker)
        if cached_market and cached_market.fair_value is not None:
            current_fv = cached_market.fair_value

        if pos:
            if pos.side == side:
                # Same direction — accumulate
                total_qty = pos.quantity + fill_qty
                # Weighted average price
                avg = (pos.avg_price * pos.quantity + fill_price * fill_qty) / total_qty
                pos.quantity = total_qty
                pos.avg_price = avg
                # Keep original fv_at_entry (from first fill)
            else:
                # Opposite direction — reduce
                remaining = pos.quantity - fill_qty
                if remaining > 0:
                    pos.quantity = remaining
                elif remaining == 0:
                    del self.positions[ticker]
                else:
                    # Flipped side (unlikely in normal operation)
                    self.positions[ticker] = PositionInfo(
                        ticker=ticker,
                        side=side,
                        quantity=-remaining,
                        avg_price=fill_price,
                        fv_at_entry=current_fv,
                    )
        else:
            # New position
            self.positions[ticker] = PositionInfo(
                ticker=ticker,
                side=side,
                quantity=fill_qty,
                avg_price=fill_price,
                fv_at_entry=current_fv,
            )

    def get_total_exposure(self) -> float:
        """Calculate total $ exposure across all positions (mark-to-market)"""
        total = 0
        with self._lock:
            for pos in self.positions.values():
                # Use current market prices (MTM) instead of cost basis
                market = self._markets_cache.get(pos.ticker)
                if market and pos.side == "yes":
                    mtm_price = (market.yes_bid or 0) / 100
                elif market and pos.side == "no":
                    mtm_price = (100 - (market.yes_ask or 100)) / 100 if market.yes_ask else pos.avg_price
                else:
                    mtm_price = pos.avg_price
                total += pos.quantity * mtm_price
        return total

    # ========== SAFETY CHECKS ==========

    def _prune_old_errors(self):
        """Remove errors older than the tracking window"""
        cutoff = datetime.now(timezone.utc).timestamp() - self.config.error_window_seconds
        while self._error_history and self._error_history[0].timestamp.timestamp() < cutoff:
            self._error_history.popleft()

    def _count_errors_by_type(self) -> dict[ErrorType, int]:
        """Count recent errors by type (within window)"""
        self._prune_old_errors()
        counts = {et: 0 for et in ErrorType}
        for error in self._error_history:
            counts[error.error_type] += 1
        return counts

    def get_portfolio_value(self) -> float:
        """
        Calculate total portfolio value = cash balance + position MTM values.
        Uses current market prices (not entry prices) for accurate equity.

        NOTE: This method only computes a value — it does NOT trigger halts.
        Staleness/failure halts are handled by check_safety() to avoid
        re-entrant _halt calls and multiple cancel_all_orders bursts.
        """
        # Use cached balance from trading_loop if available (avoids double API call / double failure counting)
        if self._loop_balance is not None:
            balance = self._loop_balance
        else:
            balance = self.client.get_balance()
            if balance is None:
                self._consecutive_balance_failures += 1
                balance = self._last_known_balance or 0
            else:
                self._consecutive_balance_failures = 0
                self._last_known_balance = balance
                self._last_balance_time = datetime.now(timezone.utc)

        # Add mark-to-market value of all positions using current prices
        position_value = 0
        with self._lock:
            for pos in self.positions.values():
                if pos.quantity <= 0:
                    continue

                market = self._markets_cache.get(pos.ticker)
                if market and (market.yes_bid or market.yes_ask):
                    # Use current market prices for MTM (exit prices)
                    if pos.side == "yes":
                        # YES exits at YES bid
                        exit_price = (market.yes_bid or 0) / 100
                    else:
                        # NO exits at NO bid = (100 - YES ask) / 100
                        if market.yes_ask:
                            exit_price = (100 - market.yes_ask) / 100
                        else:
                            # yes_ask missing/zero — fall back to entry price
                            exit_price = pos.avg_price
                    position_value += pos.quantity * exit_price
                else:
                    # Fallback to avg_price only when no market data available
                    position_value += pos.quantity * pos.avg_price

        return balance + position_value

    def check_safety(self) -> tuple[bool, str]:
        """
        Run all safety checks before trading.
        Returns (is_safe, reason_if_not_safe)
        """
        if self._halted:
            return False, f"HALTED: {self._halt_reason}"

        # Check balance staleness/failure (moved here from get_portfolio_value
        # so _halt is only called from check_safety — avoids re-entrant halts)
        if self._consecutive_balance_failures >= 2:
            self._halt("Balance API failed 2+ times consecutively — cannot verify equity")
            return False, self._halt_reason
        if self._last_balance_time:
            stale_seconds = (datetime.now(timezone.utc) - self._last_balance_time).total_seconds()
            if stale_seconds > 60:
                self._halt(f"Balance data is {stale_seconds:.0f}s stale — cannot verify equity")
                return False, self._halt_reason

        # Calculate total equity (cash + positions)
        equity = self.get_portfolio_value()
        balance = self._last_known_balance or 0

        # Initialize starting equity on first check (or new day)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._starting_balance is None or self._starting_date != today:
            self._starting_balance = equity
            self._starting_date = today
            self._save_daily_state()
            print(f"[SAFETY] Starting equity for {today}: ${equity:.2f} (cash: ${balance:.2f})")

        # Check minimum balance using EQUITY (cash + positions), not just cash.
        # Cash alone can be low when capital is deployed in positions/orders.
        if equity < self.config.min_balance:
            self._halt("Total equity too low: ${:.2f}".format(equity))
            return False, self._halt_reason

        # Check daily loss limit based on EQUITY (not just cash)
        # This way, buying positions doesn't count as a "loss"
        if self._starting_balance > 0:
            daily_pnl_pct = (equity - self._starting_balance) / self._starting_balance
            if daily_pnl_pct < -self.config.max_daily_loss_pct:
                self._halt(f"Daily loss limit hit: {daily_pnl_pct*100:.1f}%")
                return False, self._halt_reason

        # Check cumulative drawdown from peak equity
        if self._peak_equity is None:
            self._peak_equity = equity
        if equity > self._peak_equity:
            self._peak_equity = equity
            self._save_daily_state()  # Persist new peak
        if self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - equity) / self._peak_equity
            if drawdown_pct > self._max_drawdown_pct:
                self._halt(f"Max drawdown breached: {drawdown_pct*100:.1f}% from peak ${self._peak_equity:.2f}")
                return False, self._halt_reason

        # Check error rates (with time decay)
        error_counts = self._count_errors_by_type()
        total_errors = sum(error_counts.values())

        # Check total error threshold
        if total_errors >= self.config.max_errors_in_window:
            self._halt(f"Too many errors in {self.config.error_window_seconds}s: {total_errors}")
            return False, self._halt_reason

        # Check network errors (indicates connectivity issues)
        if error_counts[ErrorType.NETWORK] >= self.config.max_network_errors:
            self._halt(f"Network errors: {error_counts[ErrorType.NETWORK]} in {self.config.error_window_seconds}s")
            return False, self._halt_reason

        # Check rate limit errors (need to back off)
        if error_counts[ErrorType.RATE_LIMIT] >= self.config.max_rate_limit_errors:
            self._halt(f"Rate limited: {error_counts[ErrorType.RATE_LIMIT]} times in {self.config.error_window_seconds}s")
            return False, self._halt_reason

        # Check order failures
        order_failures = error_counts[ErrorType.ORDER_FAILED] + error_counts[ErrorType.CANCEL_FAILED]
        if order_failures >= self.config.max_order_failures:
            self._halt(f"Order failures: {order_failures} in {self.config.error_window_seconds}s")
            return False, self._halt_reason

        return True, ""

    def _halt(self, reason: str):
        """Halt trading with reason"""
        self._halted = True
        self._halt_reason = reason
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Use last known balance for the halt log — do NOT call get_portfolio_value()
        # or get_balance() here to avoid re-entrant API calls during an emergency.
        balance = self._last_known_balance or 0
        print(f"\n{'='*60}")
        print(f"[{timestamp}] TRADING HALTED")
        print(f"    Reason: {reason}")
        print(f"    Last known balance: ${balance:.2f}")
        if self._starting_balance and self._starting_balance > 0:
            pnl = balance - self._starting_balance
            pnl_pct = pnl / self._starting_balance * 100
            print(f"    Daily P&L (approx): ${pnl:.2f} ({pnl_pct:.1f}%)")
        print(f"    Active Orders: {len(self.active_orders)}")
        print(f"    Positions: {len(self.positions)}")
        print(f"{'='*60}\n")

        # Cancel all open orders
        self.cancel_all_orders("Safety halt")

    def cancel_all_orders(self, reason: str = "Manual"):
        """Cancel ALL orders in parallel (fetches from API to include orphaned orders).

        Uses ThreadPoolExecutor to cancel multiple orders simultaneously,
        which is critical during emergency halts when speed matters.
        """
        # First, sync to discover any orders we don't know about
        try:
            api_orders = self.client.get_orders(status="resting")
            api_order_ids = {o.get("order_id") for o in api_orders if o.get("order_id")}
            known_order_ids = set(self.active_orders.keys())

            # Combine both sets to cancel everything
            all_order_ids = api_order_ids | known_order_ids
            print(f"[SAFETY] Found {len(api_order_ids)} API orders, {len(known_order_ids)} tracked, {len(all_order_ids)} total to cancel")

        except Exception as e:
            print(f"[SAFETY] Error fetching orders: {e}, cancelling known orders only")
            all_order_ids = set(self.active_orders.keys())

        if not all_order_ids:
            print(f"[SAFETY] No orders to cancel: {reason}")
            return 0

        cancelled = 0
        failed = 0

        def _cancel_one(order_id: str) -> bool:
            return self.cancel_order(order_id, reason)

        # Cap at 10 workers to stay within Kalshi's 10 writes/sec rate limit
        max_workers = min(10, len(all_order_ids))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_cancel_one, oid): oid for oid in all_order_ids}
            for future in as_completed(futures):
                try:
                    if future.result():
                        cancelled += 1
                    else:
                        failed += 1
                except Exception as e:
                    failed += 1
                    print(f"[SAFETY] Exception cancelling {futures[future]}: {e}")

        print(f"[SAFETY] Cancelled {cancelled}/{len(all_order_ids)} orders ({failed} failed): {reason}")
        return cancelled

    def reset_halt(self):
        """Reset halt state and daily tracking (use with caution)"""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        prev_reason = self._halt_reason
        old_starting = self._starting_balance

        self._halted = False
        self._halt_reason = ""
        self._error_history.clear()

        # Also reset daily tracking so we don't immediately halt again
        self._starting_balance = self.get_portfolio_value()
        self._peak_equity = self._starting_balance  # Reset peak so drawdown starts fresh
        self._save_daily_state()

        print(f"\n[{timestamp}] HALT STATE RESET")
        print(f"    Previous halt reason: {prev_reason}")
        print(f"    Previous starting equity: ${old_starting:.2f}" if old_starting else "    Previous: None")
        print(f"    New starting equity: ${self._starting_balance:.2f}")
        print(f"    Peak equity reset to: ${self._peak_equity:.2f}")
        print(f"    Current balance: ${self.client.get_balance() or 0:.2f}")
        print(f"    Current equity: ${self.get_portfolio_value():.2f}")
        print(f"    Error history cleared")

    def reset_daily_tracking(self):
        """Reset daily P&L tracking (call at start of new day)"""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        old_starting = self._starting_balance

        self._starting_balance = self.get_portfolio_value()  # Use equity, not just cash
        self._error_history.clear()

        print(f"\n[{timestamp}] DAILY TRACKING RESET")
        print(f"    Previous starting equity: ${old_starting:.2f}" if old_starting else "    Previous: None")
        print(f"    New starting equity: ${self._starting_balance:.2f}")

    def _classify_error(self, error: Exception, context: str = "") -> ErrorType:
        """Classify an exception into an error type"""
        error_str = str(error).lower()

        # Network errors
        if any(x in error_str for x in ["connection", "timeout", "refused", "reset", "network"]):
            return ErrorType.NETWORK

        # Rate limiting
        if any(x in error_str for x in ["rate limit", "429", "too many requests", "throttle"]):
            return ErrorType.RATE_LIMIT

        # API rejections
        if any(x in error_str for x in ["400", "401", "403", "invalid", "unauthorized", "forbidden"]):
            return ErrorType.API_REJECTION

        # Validation errors
        if any(x in error_str for x in ["validation", "parse", "format", "type error"]):
            return ErrorType.VALIDATION

        # Context-based classification
        if "order" in context.lower():
            return ErrorType.ORDER_FAILED
        if "cancel" in context.lower():
            return ErrorType.CANCEL_FAILED

        return ErrorType.UNKNOWN

    def record_error(self, error: Exception = None, error_type: ErrorType = None,
                     message: str = "", ticker: str = None):
        """
        Record an error with categorization and time tracking.

        Args:
            error: The exception (will be classified automatically)
            error_type: Override automatic classification
            message: Description of what was happening
            ticker: Market ticker if relevant
        """
        if error_type is None and error is not None:
            error_type = self._classify_error(error, message)
        elif error_type is None:
            error_type = ErrorType.UNKNOWN

        record = ErrorRecord(
            timestamp=datetime.now(timezone.utc),
            error_type=error_type,
            message=message or (str(error) if error else "Unknown error"),
            ticker=ticker,
        )
        self._error_history.append(record)

        # Log the error
        print(f"[ERROR] {error_type.value}: {record.message[:80]}")

        # Check if this pushes us over any threshold (immediate check)
        error_counts = self._count_errors_by_type()
        total = sum(error_counts.values())

        if total >= self.config.max_errors_in_window:
            self._halt(f"Error threshold: {total} errors in {self.config.error_window_seconds}s")

    def record_order_failure(self, ticker: str, reason: str):
        """Record a failed order placement"""
        self.record_error(
            error_type=ErrorType.ORDER_FAILED,
            message=f"Order failed on {ticker}: {reason}",
            ticker=ticker,
        )

    def record_cancel_failure(self, order_id: str, reason: str):
        """Record a failed cancel operation"""
        self.record_error(
            error_type=ErrorType.CANCEL_FAILED,
            message=f"Cancel failed for {order_id}: {reason}",
        )

    def get_error_summary(self) -> dict:
        """Get summary of recent errors"""
        counts = self._count_errors_by_type()
        recent = list(self._error_history)[-5:]  # Last 5 errors

        return {
            "window_seconds": self.config.error_window_seconds,
            "total_in_window": sum(counts.values()),
            "by_type": {et.value: count for et, count in counts.items() if count > 0},
            "recent": [
                {
                    "time": e.timestamp.isoformat(),
                    "type": e.error_type.value,
                    "message": e.message[:50],
                    "ticker": e.ticker,
                }
                for e in recent
            ],
        }

    def validate_fair_value(self, market: Market) -> bool:
        """
        Sanity check fair value before using it.

        Uses configurable thresholds:
        - fv_deviation_warn_threshold: log warning but allow (default 20%)
        - fv_deviation_reject_threshold: reject the fair value entirely (default 40%)
        """
        if not self.config.sanity_check_prices:
            return True

        fv = market.fair_value
        if fv is None:
            return False

        # Check for NaN or infinity
        if not (0 <= fv <= 1):
            return False

        # Check that fair value is somewhat reasonable given market prices.
        # Only apply in tight-spread markets where the mid is informative.
        # In wide-spread / empty-book markets the "market mid" is meaningless
        # (stale default or single-sided quote) — skip the deviation check
        # and let the pricing logic handle it.
        if market.yes_bid and market.yes_ask:
            spread = market.yes_ask - market.yes_bid
            if spread < self.config.market_making_min_spread:
                market_mid = (market.yes_bid + market.yes_ask) / 200  # Convert to 0-1
                diff = abs(fv - market_mid)

                if diff > self.config.fv_deviation_reject_threshold:
                    return False

        return True

    def is_near_expiry(self, market: Market, min_minutes: int = 5) -> bool:
        """
        Check if a market is too close to expiry to trade safely.

        Args:
            market: The market to check
            min_minutes: Minimum minutes before expiry to allow trading

        Returns:
            True if market is within min_minutes of expiry
        """
        if not market.close_time:
            return False

        from .fair_value_v2 import hours_until
        hours = hours_until(market.close_time)
        if hours is None:
            return False

        minutes_remaining = hours * 60
        return minutes_remaining < min_minutes

    def get_expiry_adjusted_config(self, market: Market) -> tuple[float, int]:
        """
        Get adjusted min_edge and position_size based on time to expiry.

        Near expiry:
        - Require higher edge (prices can move fast)
        - Use smaller position sizes

        Args:
            market: The market to check

        Returns:
            Tuple of (adjusted_min_edge, adjusted_position_size)
        """
        from .fair_value_v2 import hours_until
        base_edge = self.config.min_edge
        base_size = self.config.position_size

        if not market.close_time:
            return (base_edge, base_size)

        hours = hours_until(market.close_time)
        if hours is None:
            return (base_edge, base_size)

        # Within 30 minutes: increase edge requirement by 50%, reduce size by 50%
        if hours < 0.5:
            return (base_edge * 1.5, max(1, base_size // 2))

        # Within 1 hour: increase edge requirement by 25%
        if hours < 1.0:
            return (base_edge * 1.25, base_size)

        # Normal trading
        return (base_edge, base_size)

    def calculate_position_aware_size(self, market: Market, side: str) -> int:
        """
        Calculate order size accounting for existing position.

        Rules:
        1. If no position: use full position_size
        2. If position in same direction: reduce size as we approach max
        3. If position in opposite direction: allow full size (reduces exposure)
        4. Never exceed max_position_per_market total
        """
        base_size = self.config.position_size

        pos = self.get_position(market.ticker)
        if not pos:
            return base_size

        # Calculate current position value
        current_qty = pos.quantity
        max_qty = self.config.max_position_per_market

        # If position is in opposite direction, we're reducing exposure
        if pos.side != side:
            return base_size

        # Position is in same direction - check limits
        remaining_capacity = max(0, max_qty - current_qty)
        if remaining_capacity == 0:
            return 0  # At max position

        if not self.config.position_scale_down:
            return min(base_size, remaining_capacity)

        # Scale down size as position grows
        # At 0% of max: 100% size
        # At 50% of max: 75% size
        # At 75% of max: 50% size
        # At 90% of max: 25% size
        utilization = current_qty / max_qty
        scale_factor = max(0.25, 1 - utilization * 0.75)

        scaled_size = int(base_size * scale_factor)
        return min(scaled_size, remaining_capacity)

    def calculate_min_required_edge(self, market: Market, side: str, price: int) -> float:
        """
        Calculate minimum required edge accounting for:
        1. Base minimum edge (config)
        2. Expected fill probability (lower fill prob = higher required edge)
        3. Learned adverse selection (fills that happened moved against us)
        4. Data delay buffer
        5. Transaction costs (entry fee + estimated exit slippage)

        Formula: Required Edge = Base Edge + Adverse Selection + Fill Prob Adj
                                 + Data Delay + Transaction Costs

        The idea: If fill prob is 50%, half our limit orders don't fill. The ones that
        do fill are more likely to be when price moved toward us (adverse selection).
        We need higher edge to compensate for this selection effect.
        """
        # Get expected fill probability
        fill_prob = self.tracker.predict_fill_probability(market, price, side)

        # Get learned adverse selection from tracker
        # Sign convention: negative = getting picked off (fills when price moves against us)
        #                  positive = favorable fills (fills when price moves in our favor)
        stats = self.tracker.get_stats()
        avg_adverse = stats.avg_adverse_selection

        # CRITICAL FIX: Only penalize when adverse selection is negative (getting picked off)
        # When negative (e.g., -0.02), we ADD 0.02 to required edge
        # When positive (favorable), we don't reduce required edge (be conservative)
        adverse_selection_penalty = max(0, -avg_adverse)  # Convert negative to positive penalty

        # Base required edge (from config)
        base_edge = self.config.min_edge

        # Additional edge for low fill probability
        # If fill prob = 1.0, no adjustment
        # If fill prob = 0.5, add ~0.5% extra edge
        # If fill prob = 0.2, add ~2% extra edge
        # Light adjustment: unfilled orders just don't fill, they don't cost money
        fill_prob_adjustment = max(0, (1 / max(fill_prob, 0.1) - 1) * 0.005)

        # Transaction costs: entry fee + estimated exit slippage
        # We always need to eventually exit (either at settlement or by selling),
        # so round-trip cost must be covered by edge.
        transaction_cost = self.config.entry_fee_pct + self.config.exit_cost_pct

        # Total required edge
        required_edge = (base_edge
                        + adverse_selection_penalty
                        + fill_prob_adjustment
                        + self.config.data_delay_buffer
                        + transaction_cost)

        return required_edge

    def calculate_optimal_price(self, market: Market, side: str) -> Optional[tuple[int, float, float]]:
        """
        Calculate optimal limit price for a side.
        Returns (price_cents, edge, fill_probability) or None if no good opportunity.

        Supports two modes:
        1. TIGHT SPREAD: Place orders near fair value when spreads are reasonable
        2. MARKET MAKING: Place deep limit orders on wide spreads to provide liquidity
        """
        if market.fair_value is None:
            return None

        fair_yes = market.fair_value
        fair_no = 1 - fair_yes

        # Calculate spread to determine mode
        yes_bid = market.yes_bid or 0
        yes_ask = market.yes_ask or 100
        spread = yes_ask - yes_bid

        # Check if we should use market making mode (wide spread liquidity provision)
        use_market_making = (
            self.config.market_making_enabled and
            spread >= self.config.market_making_min_spread
        )

        # Orderbook depth from embedded orderbook (if available)
        ob = market.orderbook
        depth_yes_bids = sum(l.quantity for l in ob.yes_bids) if ob and ob.yes_bids else 0
        depth_yes_asks = sum(l.quantity for l in ob.yes_asks) if ob and ob.yes_asks else 0

        if side == "yes":
            fair_cents = round(fair_yes * 100)
            current_bid = yes_bid
            current_ask = yes_ask

            # MARKET MAKING MODE: Place deep bids on wide-spread markets
            if use_market_making:
                # Only market make YES if fair value suggests it could settle YES
                if fair_yes < self.config.market_making_min_fv:
                    return None  # FV too low, YES unlikely to pay out

                # Calculate market making bid
                # For illiquid markets (spread >= 50), use % buffer only (no 6c cap)
                # For liquid-ish markets, cap at 6c to stay competitive
                if spread >= 50:
                    # No liquidity — use percentage-based pricing only
                    edge_target = int(fair_cents * (1 - self.config.market_making_edge_buffer))
                else:
                    edge_target = max(
                        fair_cents - self.config.market_making_edge_cents,
                        int(fair_cents * (1 - self.config.market_making_edge_buffer)),
                    )
                # Then ensure we at least beat the existing bid for queue priority
                mm_bid = max(
                    self.config.min_price_cents,
                    edge_target,
                    current_bid + 1 if current_bid > 0 else self.config.min_price_cents
                )

                # Ensure bid is reasonable
                if mm_bid < self.config.min_price_cents or mm_bid >= current_ask:
                    return None

                edge = fair_yes - (mm_bid / 100)

                # Market making has low fill probability but high edge
                fill_prob = self.tracker.predict_fill_probability(market, mm_bid, "yes")
                fill_prob = max(0.03, min(fill_prob, 0.15))

                # Tiered edge thresholds based on spread width (lowered for better fill rate)
                txn_cost = self.config.entry_fee_pct + self.config.exit_cost_pct
                min_mm_edge = (0.02 if spread < 20 else 0.03 if spread < 40 else 0.04) + txn_cost
                if edge >= min_mm_edge:
                    return (mm_bid, edge, fill_prob)
                return None

            # TIGHT SPREAD MODE: Normal trading near fair value
            # Require minimum volume in tight-spread mode
            if (market.volume or 0) < self.config.min_volume_tight_spread:
                return None
            # Skip extreme fair values
            if fair_yes < (1 - self.config.max_fair_value_extreme):
                return None
            if fair_yes > self.config.max_fair_value_extreme:
                return None

            # Calculate our bid price
            # Use the actual fair value in cents (not truncated) for comparisons
            fair_value_cents = fair_yes * 100  # e.g., 15.8 for 15.8% FV
            if current_bid == 0:
                optimal_bid = max(self.config.min_price_cents, fair_cents - self.config.price_buffer_cents)
            elif fair_value_cents >= current_ask and current_ask > 0:
                # Fair value meets or exceeds the ask — there's edge even paying the ask.
                # Bid aggressively at ask - 1 to sit near the top of the book.
                optimal_bid = current_ask - 1
            else:
                # Fair value between bid and ask — join the bid queue
                optimal_bid = current_bid + 1

            if optimal_bid >= fair_cents:
                optimal_bid = max(self.config.min_price_cents, fair_cents - 1)

            if optimal_bid >= current_ask:
                # If we'd cross the ask, sit at ask - 1 (still profitable if FV >= ask)
                if fair_value_cents >= current_ask and current_ask > self.config.min_price_cents:
                    optimal_bid = current_ask - 1
                else:
                    return None

            if optimal_bid < self.config.min_price_cents or optimal_bid > 98:
                return None

            edge = fair_yes - (optimal_bid / 100)

            # Use learned fill probability from historical data
            fill_prob = self.tracker.predict_fill_probability(market, optimal_bid, "yes")

            # Skip if fill probability too low (relaxed: limit orders have no downside)
            if fill_prob < 0.03:
                return None

            # Calculate dynamic minimum edge (accounts for fill prob, adverse selection)
            min_required_edge = self.calculate_min_required_edge(market, "yes", optimal_bid)
            if edge < min_required_edge:
                return None

            return (optimal_bid, edge, fill_prob)

        else:  # NO side
            fair_cents = round(fair_no * 100)
            # CRITICAL FIX: Derive NO prices from YES prices
            # NO bid = 100 - YES ask (buying YES at ask = selling NO at bid)
            # NO ask = 100 - YES bid (selling YES at bid = buying NO at ask)
            current_bid = (100 - yes_ask) if yes_ask else 0
            current_ask = (100 - yes_bid) if yes_bid else 100

            # MARKET MAKING MODE: Place deep bids on wide-spread markets
            if use_market_making:
                # Only market make NO if fair value suggests it could settle NO
                if fair_no < self.config.market_making_min_fv:
                    return None  # FV too low, NO unlikely to pay out

                # Calculate market making bid for NO side
                if spread >= 50:
                    # No liquidity — use percentage-based pricing only
                    edge_target = int(fair_cents * (1 - self.config.market_making_edge_buffer))
                else:
                    edge_target = max(
                        fair_cents - self.config.market_making_edge_cents,
                        int(fair_cents * (1 - self.config.market_making_edge_buffer)),
                    )
                # Then ensure we at least beat the existing bid for queue priority
                mm_bid = max(
                    self.config.min_price_cents,
                    edge_target,
                    current_bid + 1 if current_bid > 0 else self.config.min_price_cents
                )

                # Ensure bid is reasonable
                if mm_bid < self.config.min_price_cents or mm_bid >= current_ask:
                    return None

                edge = fair_no - (mm_bid / 100)

                # Market making has low fill probability but high edge
                fill_prob = self.tracker.predict_fill_probability(market, mm_bid, "no")
                fill_prob = max(0.03, min(fill_prob, 0.15))

                # Tiered edge thresholds based on spread width (lowered for better fill rate)
                txn_cost = self.config.entry_fee_pct + self.config.exit_cost_pct
                min_mm_edge = (0.02 if spread < 20 else 0.03 if spread < 40 else 0.04) + txn_cost
                if edge >= min_mm_edge:
                    return (mm_bid, edge, fill_prob)
                return None

            # TIGHT SPREAD MODE: Normal trading
            # Require minimum volume in tight-spread mode
            if (market.volume or 0) < self.config.min_volume_tight_spread:
                return None
            if fair_no < (1 - self.config.max_fair_value_extreme):
                return None
            if fair_no > self.config.max_fair_value_extreme:
                return None

            fair_value_cents = fair_no * 100  # e.g., 84.2 for 84.2% FV
            if current_bid == 0:
                optimal_bid = max(self.config.min_price_cents, fair_cents - self.config.price_buffer_cents)
            elif fair_value_cents >= current_ask and current_ask > 0:
                # Fair value meets or exceeds the ask — bid aggressively near the ask
                optimal_bid = current_ask - 1
            else:
                optimal_bid = current_bid + 1

            if optimal_bid >= fair_cents:
                optimal_bid = max(self.config.min_price_cents, fair_cents - 1)

            if optimal_bid >= current_ask:
                if fair_value_cents >= current_ask and current_ask > self.config.min_price_cents:
                    optimal_bid = current_ask - 1
                else:
                    return None

            if optimal_bid < self.config.min_price_cents or optimal_bid > 98:
                return None

            edge = fair_no - (optimal_bid / 100)

            # Use learned fill probability from historical data
            fill_prob = self.tracker.predict_fill_probability(market, optimal_bid, "no")

            # Skip if fill probability too low (relaxed: limit orders have no downside)
            if fill_prob < 0.03:
                return None

            # Calculate dynamic minimum edge (accounts for fill prob, adverse selection)
            min_required_edge = self.calculate_min_required_edge(market, "no", optimal_bid)
            if edge < min_required_edge:
                return None

            return (optimal_bid, edge, fill_prob)

    def should_place_order(self, market: Market) -> Optional[tuple[str, int, float, float]]:
        """
        Determine if we should place an order on this market.
        Returns (side, price, edge, fill_prob) or None.
        """
        if market.fair_value is None:
            return None

        # Reject stale fair values (>30 seconds old)
        if market.fair_value_time:
            fv_age = (datetime.now(timezone.utc) - market.fair_value_time).total_seconds()
            if fv_age > 30:
                return None

        # Skip if we already have an order on this market
        if market.ticker in self.market_orders:
            return None

        # Check both sides and pick the better one (by expected value = edge * fill_prob)
        yes_result = self.calculate_optimal_price(market, "yes")
        no_result = self.calculate_optimal_price(market, "no")

        if yes_result and no_result:
            # Pick the side with higher expected value
            yes_ev = yes_result[1] * yes_result[2]  # edge * fill_prob
            no_ev = no_result[1] * no_result[2]
            if yes_ev >= no_ev:
                return ("yes", yes_result[0], yes_result[1], yes_result[2])
            else:
                return ("no", no_result[0], no_result[1], no_result[2])
        elif yes_result:
            return ("yes", yes_result[0], yes_result[1], yes_result[2])
        elif no_result:
            return ("no", no_result[0], no_result[1], no_result[2])

        return None

    def get_mm_opportunities(self, market: Market) -> list[tuple[str, int, float, float]]:
        """
        Get market-making opportunities for BOTH sides of a wide-spread market.
        Returns list of (side, price, edge, fill_prob) — up to 2 entries (YES and NO).
        Only returns sides that don't already have an MM order.
        """
        if market.fair_value is None:
            return []
        if market.fair_value_time:
            fv_age = (datetime.now(timezone.utc) - market.fair_value_time).total_seconds()
            if fv_age > 15:
                return []

        yes_bid = market.yes_bid or 0
        yes_ask = market.yes_ask or 100
        spread = yes_ask - yes_bid
        if spread < self.config.market_making_min_spread:
            return []

        results = []
        existing_mm = self.market_making_orders.get(market.ticker, {})

        for side in ("yes", "no"):
            if side in existing_mm:
                continue  # Already have MM order on this side
            result = self.calculate_optimal_price(market, side)
            if result:
                results.append((side, result[0], result[1], result[2]))

        return results

    def place_mm_order(self, market: Market, side: str, price: int) -> Optional[str]:
        """Place a market-making order and track it separately from directional orders."""
        order_id = self.place_order(market, side, price)
        if order_id:
            with self._lock:
                if market.ticker not in self.market_making_orders:
                    self.market_making_orders[market.ticker] = {}
                self.market_making_orders[market.ticker][side] = order_id
                # Remove from market_orders so it doesn't block directional orders
                if market.ticker in self.market_orders and self.market_orders[market.ticker] == order_id:
                    del self.market_orders[market.ticker]
        return order_id

    def cancel_mm_order(self, order_id: str, ticker: str, side: str, reason: str = "") -> bool:
        """Cancel an MM order and clean up tracking."""
        success = self.cancel_order(order_id, reason)
        if success:
            with self._lock:
                if ticker in self.market_making_orders:
                    self.market_making_orders[ticker].pop(side, None)
                    if not self.market_making_orders[ticker]:
                        del self.market_making_orders[ticker]
        return success

    def should_cancel_order(self, order: ActiveOrder, market: Market) -> tuple[bool, str]:
        """
        Determine if an existing order should be cancelled.
        Returns (should_cancel, reason).
        """
        # Check order age first (stale orders get picked off)
        now = datetime.now(timezone.utc)
        order_age = (now - order.placed_at).total_seconds()
        if order_age > self.config.max_order_age_seconds:
            return (True, f"Stale order ({order_age:.0f}s > {self.config.max_order_age_seconds}s)")

        if market.fair_value is None:
            return (True, "No fair value")

        # Check if edge is still sufficient (with data delay buffer)
        edge_threshold = self.config.min_edge_to_keep + self.config.data_delay_buffer
        if order.side == "yes":
            current_edge = market.fair_value - (order.price / 100)
        else:
            current_edge = (1 - market.fair_value) - (order.price / 100)

        if current_edge < self.config.min_edge_to_keep:
            return (True, f"Edge dropped to {current_edge*100:.1f}%")

        # Check if we should adjust price (recalculate optimal)
        result = self.calculate_optimal_price(market, order.side)
        if result:
            optimal_price, _, _ = result
            price_diff = abs(optimal_price - order.price)
            if price_diff >= self.config.price_adjust_threshold:
                return (True, f"Price adjust needed: {order.price}c -> {optimal_price}c")

        return (False, "")

    def get_max_exposure(self) -> float:
        """Get maximum allowed exposure based on account balance"""
        balance = self._loop_balance or self._last_known_balance or 0
        return balance * self.config.max_capital_pct

    def get_max_position_value_per_market(self) -> float:
        """Get maximum allowed position value per single market"""
        balance = self._loop_balance or self._last_known_balance or 0
        return balance * self.config.max_position_pct_per_market

    def place_order(self, market: Market, side: str, price: int) -> Optional[str]:
        """Place a limit order and track it with position-aware sizing"""
        try:
            # Calculate position-aware order size
            order_size = self.calculate_position_aware_size(market, side)

            if order_size <= 0:
                print(f"[TRADER] Skipped {market.ticker[:30]}: max position reached")
                return None

            # Check total exposure limit (dynamic based on balance)
            total_exposure = self.get_total_exposure()
            max_exposure = self.get_max_exposure()
            order_exposure = order_size * price / 100  # Convert to dollars

            if total_exposure + order_exposure > max_exposure:
                # Reduce order size to fit within limit
                remaining_exposure = max_exposure - total_exposure
                if remaining_exposure <= 0:
                    print(f"[TRADER] Skipped: exposure limit (${total_exposure:.0f}/${max_exposure:.0f})")
                    return None
                order_size = min(order_size, int(remaining_exposure / (price / 100)))
                if order_size <= 0:
                    return None

            # Check per-market exposure limit
            pos = self.get_position(market.ticker)
            current_market_exposure = (pos.quantity * pos.avg_price) if pos else 0
            new_market_exposure = current_market_exposure + order_exposure
            max_per_market = self.get_max_position_value_per_market()

            if new_market_exposure > max_per_market:
                remaining = max_per_market - current_market_exposure
                if remaining <= 0:
                    return None
                order_size = min(order_size, int(remaining / (price / 100)))
                if order_size <= 0:
                    return None

            # Run comprehensive risk manager checks (asset-class limits, etc.)
            try:
                # Use cached positions (synced every 10s) instead of API call per order
                cached_positions = [
                    Position(
                        ticker=pi.ticker,
                        market_title=pi.ticker,
                        side=Side.YES if pi.side == "yes" else Side.NO,
                        quantity=pi.quantity,
                        avg_price=pi.avg_price,
                    )
                    for pi in self.positions.values()
                ]
                equity = self.get_portfolio_value()
                can_place, reason = self.risk_manager.check_new_order(
                    ticker=market.ticker,
                    side=side,
                    size=order_size,
                    price=price,
                    positions=cached_positions,
                    total_equity=equity,
                    pending_orders=list(self.active_orders.values()),
                )
                if not can_place:
                    print(f"[RISK] Blocked: {market.ticker[:30]} — {reason}")
                    return None
            except Exception as e:
                print(f"[RISK] Risk check error (allowing order): {e}")

            # DRY RUN MODE - log but don't execute
            if self.config.dry_run:
                edge = (market.fair_value - price/100) if side == "yes" else ((1 - market.fair_value) - price/100)
                print(f"[DRY RUN] Would place: {side.upper()} {order_size}x @ {price}c on {market.ticker[:40]}")
                print(f"          Fair value: {market.fair_value:.2%}, Edge: {edge:.1%}, Cost: ${order_size * price / 100:.2f}")

                # Track as simulated order
                sim_order_id = f"SIM-{market.ticker}-{int(time.time())}"
                with self._lock:
                    self.active_orders[sim_order_id] = ActiveOrder(
                        order_id=sim_order_id,
                        ticker=market.ticker,
                        side=side,
                        price=price,
                        size=order_size,
                        fair_value_at_placement=market.fair_value,
                    )
                    self.market_orders[market.ticker] = sim_order_id
                    self._markets_cache[market.ticker] = market
                return sim_order_id

            result = self.client.place_order(
                ticker=market.ticker,
                side=side,
                action="buy",
                count=order_size,
                price=price,
            )

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            if result.get("error"):
                error_msg = result.get('message', 'Unknown error')
                # "market_closed" is non-fatal — just skip this market going forward
                if "market_closed" in error_msg:
                    self._closed_markets.add(market.ticker)
                    print(f"[{timestamp}] Market closed, skipping: {market.ticker}")
                else:
                    self.record_order_failure(market.ticker, error_msg)
                    print(f"[{timestamp}] ORDER FAILED: {side.upper()} {order_size}x @ {price}c on {market.ticker}")
                    print(f"    Error: {error_msg}")
                return None

            order_id = result.get("order", {}).get("order_id")
            if order_id:
                with self._lock:
                    self.active_orders[order_id] = ActiveOrder(
                        order_id=order_id,
                        ticker=market.ticker,
                        side=side,
                        price=price,
                        size=order_size,
                        fair_value_at_placement=market.fair_value,
                    )
                    self.market_orders[market.ticker] = order_id
                    self._markets_cache[market.ticker] = market

                # Record order for fill probability tracking
                self.tracker.record_order(
                    order_id=order_id,
                    ticker=market.ticker,
                    side=side,
                    price=price,
                    size=order_size,
                    market=market,
                    orderbook=market.orderbook,
                )

                # Detailed logging for cross-referencing
                pos = self.get_position(market.ticker)
                pos_info = f", existing_pos: {pos.side} x{pos.quantity}" if pos else ""
                edge = (market.fair_value - price/100) if side == "yes" else ((1 - market.fair_value) - price/100) if market.fair_value else 0

                print(f"[{timestamp}] ORDER PLACED")
                print(f"    ID: {order_id}")
                print(f"    Market: {market.ticker}")
                print(f"    Title: {market.title[:50]}")
                print(f"    Side: {side.upper()}, Size: {order_size}, Price: {price}c")
                print(f"    Fair Value: {market.fair_value:.1%}, Edge: {edge:.1%}{pos_info}")
                print(f"    Bid/Ask: {market.yes_bid}c / {market.yes_ask}c")

                return order_id

        except Exception as e:
            self.record_error(e, message=f"Place order on {market.ticker}", ticker=market.ticker)

        return None

    def cancel_order(self, order_id: str, reason: str = "") -> bool:
        """Cancel an order and remove from tracking"""
        try:
            # DRY RUN MODE - just remove from tracking
            if self.config.dry_run or order_id.startswith("SIM-"):
                with self._lock:
                    if order_id in self.active_orders:
                        order = self.active_orders[order_id]
                        del self.active_orders[order_id]
                        if order.ticker in self.market_orders:
                            del self.market_orders[order.ticker]
                        # Clean up MM tracking
                        if order.ticker in self.market_making_orders:
                            self.market_making_orders[order.ticker].pop(order.side, None)
                            if not self.market_making_orders[order.ticker]:
                                del self.market_making_orders[order.ticker]
                        print(f"[DRY RUN] Would cancel: {order.side.upper()} @ {order.price}c ({reason})")
                return True

            result = self.client.cancel_order(order_id)

            if result.get("error"):
                self.record_cancel_failure(order_id, result.get("message", "Unknown"))
                return False

            with self._lock:
                if order_id in self.active_orders:
                    order = self.active_orders[order_id]
                    del self.active_orders[order_id]
                    if order.ticker in self.market_orders:
                        del self.market_orders[order.ticker]
                    # Clean up MM tracking
                    if order.ticker in self.market_making_orders:
                        self.market_making_orders[order.ticker].pop(order.side, None)
                        if not self.market_making_orders[order.ticker]:
                            del self.market_making_orders[order.ticker]
                    print(f"[TRADER] Cancelled: {order.side.upper()} @ {order.price}c ({reason})")

            # Record cancellation for fill probability tracking
            self.tracker.record_cancel(order_id, reason)

            return True

        except Exception as e:
            self.record_error(e, message=f"Cancel order {order_id}", ticker=None)
            return False

    def sync_orders(self, markets: list = None):
        """
        Sync our tracked orders with actual open orders from Kalshi.

        Handles:
        1. Partial fills - detect when filled_count increases on resting orders
        2. Complete fills - order removed from resting list
        3. Cancellations - distinguish from fills by checking order status
        """
        # Update markets cache and risk manager
        if markets:
            for m in markets:
                self._markets_cache[m.ticker] = m
            self.risk_manager.update_markets_cache(markets)

        # DRY RUN MODE - skip API sync
        if self.config.dry_run:
            return

        try:
            open_orders = self.client.get_orders(status="resting")
            # Build lookup: order_id -> order details (including filled_count)
            open_order_map = {o.get("order_id"): o for o in open_orders}
            open_ids = set(open_order_map.keys())

            with self._lock:
                # PHASE 0: Discover orders from API that we don't know about
                # This handles app restarts, orphaned orders, multiple sessions
                discovered = 0
                for kalshi_order in open_orders:
                    oid = kalshi_order.get("order_id")
                    if oid and oid not in self.active_orders:
                        side = kalshi_order.get("side", "yes")
                        price = kalshi_order.get("yes_price") if side == "yes" else kalshi_order.get("no_price")
                        if price is None:
                            price = kalshi_order.get("yes_price") or kalshi_order.get("no_price") or 0

                        # Try to use the API's created_time for accurate age tracking.
                        # If not available, set placed_at to half the max_order_age in
                        # the past so the order will be re-evaluated within one more
                        # cycle rather than appearing brand-new and sitting forever.
                        placed_at = None
                        created_time_str = kalshi_order.get("created_time") or kalshi_order.get("created_at")
                        if created_time_str:
                            try:
                                if isinstance(created_time_str, str):
                                    ct = created_time_str.replace("Z", "+00:00")
                                    placed_at = datetime.fromisoformat(ct)
                                    if placed_at.tzinfo is None:
                                        placed_at = placed_at.replace(tzinfo=timezone.utc)
                            except (ValueError, TypeError):
                                pass
                        if placed_at is None:
                            # Fallback: assume order has been resting for half the max age.
                            # This ensures it will be cancelled within one more max_order_age
                            # window rather than getting a fresh timer.
                            half_max_age = self.config.max_order_age_seconds / 2
                            placed_at = datetime.now(timezone.utc) - timedelta(seconds=half_max_age)

                        # Detect exit orders (sell action) so fills don't trigger entry circuit breaker
                        order_action = kalshi_order.get("action", "buy")
                        is_exit_order = (order_action == "sell")

                        self.active_orders[oid] = ActiveOrder(
                            order_id=oid,
                            ticker=kalshi_order.get("ticker", ""),
                            side=side,
                            price=price,
                            size=kalshi_order.get("count", 0),
                            fair_value_at_placement=0.0,  # Unknown for discovered orders
                            placed_at=placed_at,
                            filled_count=kalshi_order.get("filled_count", 0),
                            remaining_count=kalshi_order.get("remaining_count", kalshi_order.get("count", 0)),
                            is_exit=is_exit_order,
                        )
                        discovered += 1

                if discovered > 0:
                    print(f"[SYNC] Discovered {discovered} orders from API (total: {len(self.active_orders)} tracked)")

                # PHASE 1: Check for PARTIAL FILLS on still-resting orders
                for oid, order in list(self.active_orders.items()):
                    if oid in open_order_map:
                        kalshi_order = open_order_map[oid]
                        kalshi_filled = kalshi_order.get("filled_count", 0)
                        kalshi_remaining = kalshi_order.get("remaining_count", order.size)

                        # Check if any new fills occurred
                        new_fills = kalshi_filled - order.filled_count
                        if new_fills > 0:
                            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                            market = self._markets_cache.get(order.ticker)

                            # Record fill for circuit breaker (exit fills excluded)
                            if order.is_exit:
                                self._exit_fill_timestamps.append(time.time())
                            else:
                                self._fill_timestamps.append(time.time())

                            # Record partial fill
                            self.tracker.record_partial_fill(oid, new_fills, order.size, market)

                            cost = new_fills * order.price / 100
                            print(f"[{timestamp}] PARTIAL FILL")
                            print(f"    ID: {oid}")
                            print(f"    Market: {order.ticker}")
                            print(f"    Side: {order.side.upper()}, Filled: {new_fills}/{order.size}, Price: {order.price}c")
                            print(f"    Cost: ${cost:.2f}, Total filled: {kalshi_filled}/{order.size}")

                            # Update our tracking
                            order.filled_count = kalshi_filled
                            order.remaining_count = kalshi_remaining

                            # Immediately update position so sizing/exit logic sees latest state
                            self._update_position_on_fill(order.ticker, order.side, new_fills, order.price)

                # PHASE 2: Identify orders no longer resting and snapshot their data.
                # We release the lock BEFORE making API calls to avoid blocking
                # other threads (get_position, get_total_exposure, etc.) for up to 75s.
                to_remove = [oid for oid in self.active_orders if oid not in open_ids]

                MAX_STATUS_CHECKS_PER_SYNC = 5
                if len(to_remove) > MAX_STATUS_CHECKS_PER_SYNC:
                    print(f"[SYNC] {len(to_remove) - MAX_STATUS_CHECKS_PER_SYNC} removed orders deferred to next cycle")
                    to_remove = to_remove[:MAX_STATUS_CHECKS_PER_SYNC]

                # Snapshot order data while we hold the lock
                orders_to_check = []
                for oid in to_remove:
                    order = self.active_orders[oid]
                    orders_to_check.append((oid, order.ticker, order.side, order.price,
                                            order.size, order.filled_count,
                                            order.fair_value_at_placement, order.is_exit))

            # Lock released — now make API calls without blocking other threads
            for oid, ticker, side, price, size, prev_filled, fv_at_placement, was_exit in orders_to_check:
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                order_status = self.client.get_order_status(oid)
                actual_status = order_status.get("status") if order_status else "unknown"
                final_filled = order_status.get("filled_count", 0) if order_status else 0

                market = self._markets_cache.get(ticker)
                new_fills = final_filled - prev_filled

                if actual_status in ("filled", "executed"):
                    if new_fills > 0:
                        self.tracker.record_partial_fill(oid, new_fills, size, market)
                    # Exit fills tracked separately — don't trigger entry fill circuit breaker
                    if was_exit:
                        self._exit_fill_timestamps.append(time.time())
                    else:
                        self._fill_timestamps.append(time.time())

                    cost = size * price / 100
                    max_profit = size * (100 - price) / 100 if side == "yes" else size * price / 100

                    print(f"[{timestamp}] ORDER FULLY FILLED")
                    print(f"    ID: {oid}")
                    print(f"    Market: {ticker}")
                    print(f"    Side: {side.upper()}, Size: {size}, Price: {price}c")
                    print(f"    Total Cost: ${cost:.2f}, Max Profit: ${max_profit:.2f}")
                    if fv_at_placement:
                        print(f"    FV at placement: {fv_at_placement:.1%}")

                elif actual_status in ("canceled", "cancelled", "expired"):
                    if final_filled > 0 and new_fills > 0:
                        self.tracker.record_partial_fill(oid, new_fills, size, market)
                        print(f"[{timestamp}] ORDER CANCELLED (with {final_filled} partial fills)")
                    else:
                        self.tracker.record_cancel(oid, f"External {actual_status}")
                        print(f"[{timestamp}] ORDER {actual_status.upper()}")
                    print(f"    ID: {oid}, Market: {ticker}")
                    if final_filled > 0:
                        print(f"    Partial fills: {final_filled}/{size} contracts")

                else:
                    if final_filled > 0 and new_fills > 0:
                        self.tracker.record_partial_fill(oid, new_fills, size, market)
                        print(f"[{timestamp}] ORDER REMOVED (status: {actual_status}, {final_filled} filled)")
                    else:
                        self.tracker.record_cancel(oid, f"Unknown status: {actual_status}")
                        print(f"[{timestamp}] ORDER REMOVED (status: {actual_status})")
                    print(f"    ID: {oid}, Market: {ticker}")

                # Re-acquire lock briefly to update state
                with self._lock:
                    if new_fills > 0:
                        self._update_position_on_fill(ticker, side, new_fills, price)
                    if ticker in self.market_orders:
                        del self.market_orders[ticker]
                    # Clean up MM tracking
                    if ticker in self.market_making_orders:
                        self.market_making_orders[ticker].pop(side, None)
                        if not self.market_making_orders[ticker]:
                            del self.market_making_orders[ticker]
                    if oid in self.active_orders:
                        del self.active_orders[oid]

        except Exception as e:
            self.record_error(e, message="Sync orders")

    def get_available_capital(self) -> float:
        """Get available capital for new orders"""
        balance = self._loop_balance or self._last_known_balance or 0
        max_capital = balance * self.config.max_capital_pct

        # Subtract capital already committed (exclude exit orders — they free capital, not consume it)
        committed = sum(
            o.price * o.size / 100
            for o in self.active_orders.values()
            if not o.is_exit
        )

        return max(0, max_capital - committed)

    def manage_exits(self, markets: list[Market]) -> int:
        """
        Check existing positions and execute exits when recommended.

        This runs periodically (every 30s) to:
        1. Analyze all positions against current fair values
        2. Execute exits for positions that should be closed
        3. Return number of exit orders placed

        Skipped entirely if trading is halted.

        Returns:
            Number of exit operations performed
        """
        if self._halted:
            return 0

        now = datetime.now(timezone.utc)

        # Only check exits every 30 seconds to avoid over-trading
        if (now - self._last_exit_check).total_seconds() < 30:
            return 0

        self._last_exit_check = now

        # Build Position objects from local tracking (synced by sync_positions every 10s)
        # Avoids a duplicate API call — self.positions is already up-to-date
        market_map = {m.ticker: m for m in markets}
        raw_positions = []
        with self._lock:
            for pos in self.positions.values():
                if pos.quantity <= 0:
                    continue
                market = self._markets_cache.get(pos.ticker)
                p = Position(
                    ticker=pos.ticker,
                    market_title=market.title if market else pos.ticker,
                    side=Side.YES if pos.side == "yes" else Side.NO,
                    quantity=pos.quantity,
                    avg_price=pos.avg_price,
                    realized_pnl=pos.pnl,
                    fv_at_entry=pos.fv_at_entry,
                )
                # Attach current market data for P&L calculation
                if market:
                    p.current_bid = (market.yes_bid or 0) / 100
                    p.current_ask = (market.yes_ask or 100) / 100
                    p.fair_value = market.fair_value
                raw_positions.append(p)
        if not raw_positions:
            return 0

        # Analyze positions
        recommendations = self.position_manager.analyze_all_positions(raw_positions, market_map)

        # Execute high-urgency exits with aggressive pricing
        exits_executed = 0
        for rec in recommendations:
            if rec.action == "EXIT" and rec.urgency == "HIGH":
                # Skip if we already have a pending exit order for this ticker
                if rec.ticker in self._pending_exits:
                    continue

                # Place exit order
                try:
                    position = rec.position
                    side = position.side.value.lower()  # "yes" or "no"

                    # Cap exit size at actual remaining position quantity
                    exit_size = min(rec.suggested_size, position.quantity)
                    if exit_size <= 0:
                        continue

                    # For HIGH urgency exits, use aggressive pricing to ensure fill.
                    # Subtract 2c from bid for slippage tolerance (configurable).
                    # This dramatically improves fill rate on loss-cut exits.
                    aggressive_slippage = 2  # cents below bid
                    price = max(1, rec.suggested_price - aggressive_slippage)

                    result = self.client.place_order(
                        ticker=rec.ticker,
                        side=side,
                        action="sell",
                        count=exit_size,
                        price=price,
                    )

                    if result and not result.get("error"):
                        timestamp = now.strftime("%Y-%m-%d %H:%M:%S UTC")
                        exit_oid = result.get("order", {}).get("order_id")
                        print(f"[{timestamp}] EXIT ORDER PLACED (aggressive)")
                        print(f"    {rec.ticker[:40]}")
                        print(f"    Sell {side.upper()} {exit_size}x @ {price}c (bid was {rec.suggested_price}c)")
                        print(f"    Reason: {rec.reason.value} - {rec.reasoning}")
                        exits_executed += 1

                        # Register in active_orders so circuit breaker knows this is an exit
                        if exit_oid:
                            with self._lock:
                                self.active_orders[exit_oid] = ActiveOrder(
                                    order_id=exit_oid,
                                    ticker=rec.ticker,
                                    side=side,
                                    price=price,
                                    size=exit_size,
                                    fair_value_at_placement=rec.current_fair_value or 0.0,
                                    placed_at=now,
                                    is_exit=True,
                                )

                        # Track exit for escalation if it doesn't fill
                        self._pending_exits[rec.ticker] = {
                            "placed_at": now,
                            "side": side,
                            "size": exit_size,
                            "initial_price": price,
                            "order_id": exit_oid,
                        }
                    else:
                        print(f"[EXIT] Failed to place exit order for {rec.ticker}: {result}")

                except Exception as e:
                    print(f"[EXIT] Error placing exit for {rec.ticker}: {e}")

        # Escalate stale exits: if an exit order has been sitting >60s, reprice more aggressively
        for ticker, exit_info in list(self._pending_exits.items()):
            age = (now - exit_info["placed_at"]).total_seconds()
            if age > 60 and exit_info.get("order_id"):
                try:
                    # Cancel the old exit order first
                    cancel_result = self.client.cancel_order(exit_info["order_id"])

                    # Verify cancel succeeded before placing replacement
                    cancel_ok = (
                        cancel_result and
                        not cancel_result.get("error")
                    )
                    if not cancel_ok:
                        # Cancel failed — the order may have filled or already been cancelled.
                        # Remove from pending and let the next cycle re-evaluate.
                        print(f"[EXIT] Cancel failed for {ticker} (may have filled), removing from pending")
                        del self._pending_exits[ticker]
                        continue

                    escalated_price = max(1, exit_info["initial_price"] - 2)
                    result = self.client.place_order(
                        ticker=ticker,
                        side=exit_info["side"],
                        action="sell",
                        count=exit_info["size"],
                        price=escalated_price,
                    )
                    if result and not result.get("error"):
                        escalated_oid = result.get("order", {}).get("order_id")
                        print(f"[EXIT] Escalated exit for {ticker}: {exit_info['initial_price']}c -> {escalated_price}c")
                        exit_info["initial_price"] = escalated_price
                        exit_info["placed_at"] = now
                        exit_info["order_id"] = escalated_oid
                        # Register escalated exit order so circuit breaker knows it's an exit
                        if escalated_oid:
                            with self._lock:
                                self.active_orders[escalated_oid] = ActiveOrder(
                                    order_id=escalated_oid,
                                    ticker=ticker,
                                    side=exit_info["side"],
                                    price=escalated_price,
                                    size=exit_info["size"],
                                    fair_value_at_placement=0.0,
                                    placed_at=now,
                                    is_exit=True,
                                )
                    else:
                        del self._pending_exits[ticker]
                except Exception as e:
                    print(f"[EXIT] Error escalating exit for {ticker}: {e}")
                    del self._pending_exits[ticker]
            elif age > 180:
                # Give up after 3 minutes — remove from tracking
                del self._pending_exits[ticker]

        return exits_executed

    def trading_loop(self, markets: list[Market], interval: float = 5.0):
        """
        Main trading loop. Call this periodically with updated markets.

        Strategy:
        0. Run safety checks first
        1. Sync positions and orders with Kalshi
        2. For each market WITH an order: check if needs cancel/adjust
        3. For each market WITHOUT an order: check if should place
        4. Rate limit: max 8 order operations per loop (to stay under 10/sec limit)
        """
        self.running = True  # Mark as running

        # FILL RATE CIRCUIT BREAKER: >5 fills in 60s = pause 120s
        now_ts = time.time()
        if self._fill_pause_until and now_ts < self._fill_pause_until:
            remaining = self._fill_pause_until - now_ts
            if int(remaining) % 30 == 0:  # Log every 30s
                print(f"[SAFETY] Fill rate pause: {remaining:.0f}s remaining")
            return
        self._fill_pause_until = None  # Clear expired pause
        # Count fills in last 60 seconds
        cutoff = now_ts - 60
        while self._fill_timestamps and self._fill_timestamps[0] < cutoff:
            self._fill_timestamps.popleft()
        if len(self._fill_timestamps) > 5:
            self._fill_pause_until = now_ts + 120
            print(f"[SAFETY] Fill rate breaker: {len(self._fill_timestamps)} fills in 60s — pausing 120s")
            return

        # Cache balance once per loop to avoid multiple API calls
        self._loop_balance = self.client.get_balance()
        if self._loop_balance is not None:
            self._last_known_balance = self._loop_balance
            self._last_balance_time = datetime.now(timezone.utc)
            self._consecutive_balance_failures = 0
        else:
            self._consecutive_balance_failures += 1
            self._loop_balance = self._last_known_balance

        # SAFETY CHECK FIRST
        is_safe, reason = self.check_safety()
        if not is_safe:
            # Only log halt message once per minute to avoid log spam
            now_ts = datetime.now(timezone.utc)
            if not hasattr(self, '_last_halt_log') or (now_ts - self._last_halt_log).total_seconds() > 60:
                print(f"[SAFETY] Trading halted: {reason}")
                self._last_halt_log = now_ts
            self._loop_balance = None
            return  # Don't trade if safety check fails

        try:
            # Sync positions every 10 seconds (to track fills and manage exposure)
            now = datetime.now(timezone.utc)
            if (now - self._last_position_sync).total_seconds() > 10:
                self.sync_positions()

            # Sync with actual orders (pass markets for fill detection)
            self.sync_orders(markets)

            # Periodically clear closed-markets cache (every 10 min) so reopened markets get retried
            if time.time() - self._closed_markets_cleared_at > 600:
                if self._closed_markets:
                    print(f"[TRADER] Clearing {len(self._closed_markets)} closed-market entries for retry")
                self._closed_markets.clear()
                self._closed_markets_cleared_at = time.time()

            # Filter markets with valid fair values and not too close to expiry
            if self.config.sanity_check_prices:
                markets = [m for m in markets if self.validate_fair_value(m)]

            # Filter out markets that are too close to expiry
            markets = [m for m in markets if not self.is_near_expiry(m, min_minutes=self.config.near_expiry_min_minutes)]

            # Filter out markets known to be closed (avoids burning order-failure budget)
            if self._closed_markets:
                markets = [m for m in markets if m.ticker not in self._closed_markets]

            # Track order operations this loop (rate limiting)
            ops_this_loop = 0
            max_ops_per_loop = 12  # Budget for cancels + new orders per loop

            # VIX-based dynamic order scaling
            effective_max_orders = self.config.max_total_orders
            if self.config.vix_scale_orders and self._vix_price:
                if self._vix_price > self.config.vix_extreme_threshold:
                    effective_max_orders = int(self.config.max_total_orders * 2.0)
                elif self._vix_price > self.config.vix_high_threshold:
                    effective_max_orders = int(self.config.max_total_orders * 1.5)

            # Build market lookup for speed
            market_map = {m.ticker: m for m in markets}

            # POSITION EXIT MANAGEMENT - Check if any positions should be closed
            # This runs every 30s and places exit orders for loss-cutting/profit-taking
            exits_placed = self.manage_exits(markets)
            ops_this_loop += exits_placed

            # PHASE 0: Cancel stale orders (older than max_order_age_seconds)
            for order_id, order in list(self.active_orders.items()):
                if ops_this_loop >= max_ops_per_loop:
                    break

                age_seconds = (now - order.placed_at).total_seconds()
                if age_seconds > self.config.max_order_age_seconds:
                    self.cancel_order(order_id, f"Stale ({age_seconds:.0f}s old)")
                    ops_this_loop += 1

            # PHASE 1: Check existing orders for cancellation/adjustment
            orders_to_replace = []  # (ticker, market) tuples for orders we cancelled that need replacement

            for order_id, order in list(self.active_orders.items()):
                if ops_this_loop >= max_ops_per_loop:
                    break

                market = market_map.get(order.ticker)
                if not market:
                    self.cancel_order(order_id, "Market not found")
                    ops_this_loop += 1
                    continue

                should_cancel, reason = self.should_cancel_order(order, market)
                if should_cancel:
                    self.cancel_order(order_id, reason)
                    ops_this_loop += 1
                    # Mark for potential replacement if it was a price adjustment
                    if "Price adjust" in reason:
                        orders_to_replace.append((order.ticker, market))

            # PHASE 2: Replace cancelled orders that need adjustment
            available_capital = self.get_available_capital()
            # Conservative per-order cost estimate for pre-check gating.
            # Use min_price_cents as lower bound (cheapest possible order);
            # actual cost is computed from the real price after should_place_order.
            min_order_cost = self.config.position_size * self.config.min_price_cents / 100

            for ticker, market in orders_to_replace:
                if ops_this_loop >= max_ops_per_loop:
                    break
                if available_capital < min_order_cost:
                    break
                if len(self.active_orders) >= effective_max_orders:
                    break
                if ticker in self.market_orders:  # Already have new order
                    continue

                result = self.should_place_order(market)
                if result:
                    side, price, edge, fill_prob = result
                    actual_cost = self.config.position_size * price / 100
                    if available_capital < actual_cost:
                        continue
                    self.place_order(market, side, price)
                    ops_this_loop += 1
                    available_capital -= actual_cost

            # PHASE 3: Place new orders on markets without orders
            if ops_this_loop >= max_ops_per_loop:
                return
            if len(self.active_orders) >= effective_max_orders:
                return
            if available_capital < min_order_cost:
                return

            # Find all opportunities (markets without orders)
            opportunities = []
            skipped_markets = {"no_fv": 0, "stale_fv": 0, "has_order": 0, "no_edge": 0}
            for market in markets:
                if market.ticker in self.market_orders:
                    skipped_markets["has_order"] += 1
                    continue  # Already have order
                result = self.should_place_order(market)
                if result:
                    side, price, edge, fill_prob = result
                    ev = edge * fill_prob
                    opportunities.append((market, side, price, edge, fill_prob, ev))
                elif market.fair_value is None:
                    skipped_markets["no_fv"] += 1
                elif market.fair_value_time and (now - market.fair_value_time).total_seconds() > 30:
                    skipped_markets["stale_fv"] += 1
                else:
                    skipped_markets["no_edge"] += 1

            # Sort by edge descending (not EV - fill probability is hard-coded and
            # penalizes high-edge orders unfairly. With no downside to limit orders,
            # we should prioritize the highest edge opportunities)
            opportunities.sort(key=lambda x: x[3], reverse=True)

            # Periodic diagnostic log (every ~30 seconds)
            if (now - self._last_diag_log).total_seconds() > 30:
                self._last_diag_log = now
                asset_classes = {}
                for m, s, p, e, fp, ev in opportunities:
                    ac = self.risk_manager.get_asset_class(m.ticker) if hasattr(self, 'risk_manager') else "?"
                    asset_classes[ac] = asset_classes.get(ac, 0) + 1
                print(f"[TRADER] Phase 3: {len(opportunities)} opps ({asset_classes}), "
                      f"skipped: {skipped_markets}, "
                      f"orders: {len(self.active_orders)}/{effective_max_orders}, "
                      f"capital: ${available_capital:.0f}")
                # Log rejected markets that have significant displayed edge (helps debug)
                for market in markets:
                    if market.fair_value and market.ticker not in self.market_orders:
                        fv = market.fair_value
                        ask = market.yes_ask or 100
                        displayed_edge = fv - (ask / 100)
                        if displayed_edge >= 0.03 and not any(m.ticker == market.ticker for m, *_ in opportunities):
                            # This market has 3%+ displayed edge but was rejected
                            yes_result = self.calculate_optimal_price(market, "yes")
                            no_result = self.calculate_optimal_price(market, "no")
                            bid = market.yes_bid or 0
                            min_edge = self.calculate_min_required_edge(market, "yes", ask - 1) if ask > 1 else 999
                            print(f"[TRADER] REJECTED {market.ticker[-25:]}: "
                                  f"FV={fv:.1%} bid={bid}c ask={ask}c edge={displayed_edge:.1%} "
                                  f"min_req={min_edge:.1%} "
                                  f"yes={yes_result} no={no_result}")

            # Place orders up to limits
            for market, side, price, edge, fill_prob, ev in opportunities:
                if ops_this_loop >= max_ops_per_loop:
                    break
                if len(self.active_orders) >= effective_max_orders:
                    break
                actual_cost = self.config.position_size * price / 100
                if available_capital < actual_cost:
                    break

                self.place_order(market, side, price)
                ops_this_loop += 1
                available_capital -= actual_cost

            # PHASE 4: Dual-sided market making on wide-spread markets
            if self.config.market_making_enabled and ops_this_loop < max_ops_per_loop:
                # Cancel stale MM orders first
                for ticker, sides in list(self.market_making_orders.items()):
                    for side, order_id in list(sides.items()):
                        if ops_this_loop >= max_ops_per_loop:
                            break
                        order = self.active_orders.get(order_id)
                        if not order:
                            # Order already filled/cancelled, clean up tracking
                            with self._lock:
                                if ticker in self.market_making_orders:
                                    self.market_making_orders[ticker].pop(side, None)
                                    if not self.market_making_orders[ticker]:
                                        del self.market_making_orders[ticker]
                            continue
                        market = market_map.get(ticker)
                        if market:
                            should_cancel, reason = self.should_cancel_order(order, market)
                            if should_cancel:
                                self.cancel_mm_order(order_id, ticker, side, f"MM: {reason}")
                                ops_this_loop += 1

                # Place new MM orders on both sides
                for market in markets:
                    if ops_this_loop >= max_ops_per_loop:
                        break
                    if len(self.active_orders) >= effective_max_orders:
                        break
                    if available_capital < min_order_cost:
                        break

                    mm_opps = self.get_mm_opportunities(market)
                    for side, price, edge, fill_prob in mm_opps:
                        if ops_this_loop >= max_ops_per_loop:
                            break
                        if len(self.active_orders) >= effective_max_orders:
                            break
                        actual_cost = self.config.position_size * price / 100
                        if available_capital < actual_cost:
                            continue
                        self.place_mm_order(market, side, price)
                        ops_this_loop += 1
                        available_capital -= actual_cost

        except Exception as e:
            self.record_error(e, message="Trading loop")
            import traceback
            traceback.print_exc()
        finally:
            self._loop_balance = None  # Clear cached balance at end of loop

    def get_status(self) -> dict:
        """Get current trader status including positions"""
        tracker_stats = self.tracker.get_stats()
        total_exposure = self.get_total_exposure()
        balance = self.client.get_balance() or self._last_known_balance or 0
        equity = self.get_portfolio_value()  # Cash + position values
        max_exposure = balance * self.config.max_capital_pct

        # Calculate daily P&L based on EQUITY (not just cash)
        # This way, buying positions doesn't show as a loss
        daily_pnl = 0
        daily_pnl_pct = 0
        if self._starting_balance and self._starting_balance > 0:
            daily_pnl = equity - self._starting_balance
            daily_pnl_pct = daily_pnl / self._starting_balance * 100

        return {
            "running": self.running,
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "active_orders": len(self.active_orders),
            "total_positions": len(self.positions),
            "total_exposure": total_exposure,
            "max_exposure": max_exposure,
            "balance": balance,
            "equity": equity,  # Cash + position values
            "capital_pct_used": (total_exposure / balance * 100) if balance > 0 else 0,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
            "starting_balance": self._starting_balance,  # This is actually starting equity now
            "errors": self.get_error_summary(),
            "orders": [
                {
                    "ticker": o.ticker,
                    "title": self._markets_cache[o.ticker].title if o.ticker in self._markets_cache else o.ticker,
                    "side": o.side,
                    "price": o.price,
                    "size": o.size,
                    "fair_value": o.fair_value_at_placement,
                    "age_seconds": (datetime.now(timezone.utc) - o.placed_at).total_seconds(),
                }
                for o in self.active_orders.values()
            ],
            "positions": [
                {
                    "ticker": p.ticker,
                    "side": p.side,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "pnl": p.pnl,
                }
                for p in self.positions.values()
            ],
            "fill_stats": {
                "total_orders": tracker_stats.total_orders,
                "filled": tracker_stats.filled,
                "cancelled": tracker_stats.cancelled,
                "fill_rate": tracker_stats.fill_rate,
                "avg_time_to_fill": tracker_stats.avg_time_to_fill_seconds,
                "avg_adverse_selection": tracker_stats.avg_adverse_selection,
                "fill_rate_by_spread": tracker_stats.fill_rate_by_spread,
            }
        }

    def get_fill_stats_formatted(self) -> str:
        """Get formatted fill statistics"""
        return self.tracker.format_stats()


def create_auto_trader(client: KalshiClient,
                       min_edge: float = 0.03,
                       position_size: int = 10,
                       max_orders: int = 20) -> AutoTrader:
    """Factory function to create auto-trader with custom config"""
    config = TraderConfig(
        min_edge=min_edge,
        position_size=position_size,
        max_total_orders=max_orders,
    )
    return AutoTrader(client, config)
