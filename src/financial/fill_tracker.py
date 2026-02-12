"""
Fill Probability Tracker

Tracks order outcomes to build a data-driven fill probability model.
Logs every order with full market context, tracks fills vs. cancellations,
and learns actual fill rates from historical data.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path


@dataclass
class OrderRecord:
    """Complete record of an order with context at placement time"""
    # Order identification
    order_id: str
    ticker: str
    side: str  # "yes" or "no"
    price: int  # cents
    size: int

    # Market context at placement
    fair_value: float
    market_yes_bid: int
    market_yes_ask: int
    market_no_bid: int
    market_no_ask: int
    spread: int  # ask - our_bid
    volume: int  # market volume
    hours_to_expiry: Optional[float] = None

    # Orderbook context (if available)
    orderbook_depth_bid: int = 0  # contracts ahead of us
    orderbook_depth_ask: int = 0  # contracts on other side
    queue_position: int = 0  # our position in the queue (0 = best)

    # Timing
    placed_at: str = ""  # ISO timestamp
    placed_at_unix: float = 0

    # Outcome (filled in later)
    outcome: str = "pending"  # "pending", "filled", "cancelled", "expired"
    outcome_at: Optional[str] = None
    outcome_at_unix: Optional[float] = None
    time_to_outcome_seconds: Optional[float] = None

    # Post-fill analysis (for adverse selection)
    fair_value_at_outcome: Optional[float] = None
    fair_value_change: Optional[float] = None  # positive = moved in our favor

    # Settlement outcome tracking (for true P&L analysis)
    settlement_outcome: Optional[str] = None  # "won", "lost", None (not yet settled)
    settlement_pnl: Optional[float] = None  # Actual P&L from this position

    def __post_init__(self):
        if not self.placed_at:
            now = datetime.now(timezone.utc)
            self.placed_at = now.isoformat()
            self.placed_at_unix = now.timestamp()


@dataclass
class FillStats:
    """Aggregated fill statistics"""
    total_orders: int = 0
    filled: int = 0
    cancelled: int = 0
    expired: int = 0
    pending: int = 0

    fill_rate: float = 0.0
    avg_time_to_fill_seconds: float = 0.0
    avg_adverse_selection: float = 0.0  # avg fair value move against us

    # By spread bucket
    fill_rate_by_spread: dict = field(default_factory=dict)
    # By time to expiry bucket
    fill_rate_by_expiry: dict = field(default_factory=dict)
    # By volume bucket
    fill_rate_by_volume: dict = field(default_factory=dict)


class FillTracker:
    """
    Tracks order outcomes and builds fill probability model.

    Usage:
        tracker = FillTracker()

        # When placing order
        tracker.record_order(order_id, ticker, side, price, size, market)

        # When order fills
        tracker.record_fill(order_id, market)

        # When cancelling
        tracker.record_cancel(order_id, reason)

        # Get predicted fill probability
        prob = tracker.predict_fill_probability(market, price, side)
    """

    def __init__(self, data_dir: str = None):
        if data_dir is None:
            # Store in project directory
            data_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data"
            )

        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)

        self.orders_file = self.data_dir / "order_history.json"
        self.stats_file = self.data_dir / "fill_stats.json"

        # In-memory state
        self.pending_orders: dict[str, OrderRecord] = {}
        self.completed_orders: list[OrderRecord] = []

        # Load existing data
        self._load_data()

        # Cached model parameters (updated on each recalculation)
        self._model_params = self._calculate_model_params()

        # Clean up stale pending orders on startup
        self._cleanup_stale_pending()

    def _cleanup_stale_pending(self):
        """Remove pending orders older than 24 hours (likely orphaned)."""
        now = time.time()
        max_age_seconds = 24 * 3600  # 24 hours
        stale_ids = [
            oid for oid, record in self.pending_orders.items()
            if record.placed_at_unix and (now - record.placed_at_unix) > max_age_seconds
        ]
        for oid in stale_ids:
            record = self.pending_orders.pop(oid)
            record.outcome = "expired"
            record.outcome_at = datetime.now(timezone.utc).isoformat()
            record.outcome_at_unix = now
            record.time_to_outcome_seconds = now - record.placed_at_unix
            self.completed_orders.append(record)
        if stale_ids:
            print(f"[TRACKER] Cleaned up {len(stale_ids)} stale pending orders (>24h old)")
            self._save_data()

    def _load_data(self):
        """Load historical order data from disk"""
        if self.orders_file.exists():
            try:
                with open(self.orders_file, 'r') as f:
                    data = json.load(f)

                for record_dict in data.get("completed", []):
                    self.completed_orders.append(OrderRecord(**record_dict))

                for order_id, record_dict in data.get("pending", {}).items():
                    self.pending_orders[order_id] = OrderRecord(**record_dict)

                print(f"[TRACKER] Loaded {len(self.completed_orders)} historical orders, {len(self.pending_orders)} pending")
            except Exception as e:
                print(f"[TRACKER] Error loading data: {e}")

    def _save_data(self):
        """Persist order data to disk"""
        try:
            data = {
                "completed": [asdict(r) for r in self.completed_orders[-10000:]],  # Keep last 10k
                "pending": {oid: asdict(r) for oid, r in self.pending_orders.items()},
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }

            tmp = str(self.orders_file) + ".tmp"
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, str(self.orders_file))
        except Exception as e:
            print(f"[TRACKER] Error saving data: {e}")

    def record_order(self, order_id: str, ticker: str, side: str, price: int,
                     size: int, market, orderbook=None) -> OrderRecord:
        """Record a new order with full market context"""

        # Periodically clean up stale pending orders
        self._cleanup_stale_pending()

        # Calculate spread from our price to the ask
        if side == "yes":
            spread = (market.yes_ask or 100) - price
        else:
            spread = (market.no_ask or 100) - price

        # Calculate hours to expiry
        hours_to_expiry = None
        if market.close_time:
            delta = market.close_time - datetime.now(timezone.utc)
            hours_to_expiry = max(0, delta.total_seconds() / 3600)

        # Get orderbook depth and queue position if available
        depth_bid = 0
        depth_ask = 0
        queue_position = 0
        if orderbook:
            # Count contracts ahead of us at same or better price
            for level in orderbook.yes_bids:
                if level.price > price:
                    depth_bid += level.quantity
                    queue_position += level.quantity
                elif level.price == price:
                    depth_bid += level.quantity
                    # We'd be at the back of this price level
                    queue_position += level.quantity
            for level in orderbook.yes_asks:
                depth_ask += level.quantity

        record = OrderRecord(
            order_id=order_id,
            ticker=ticker,
            side=side,
            price=price,
            size=size,
            fair_value=market.fair_value or 0.5,
            market_yes_bid=market.yes_bid or 0,
            market_yes_ask=market.yes_ask or 100,
            market_no_bid=market.no_bid or 0,
            market_no_ask=market.no_ask or 100,
            spread=spread,
            volume=market.volume or 0,
            hours_to_expiry=hours_to_expiry,
            orderbook_depth_bid=depth_bid,
            orderbook_depth_ask=depth_ask,
            queue_position=queue_position,
        )

        self.pending_orders[order_id] = record
        self._save_data()

        return record

    def record_fill(self, order_id: str, market=None):
        """Record that an order was filled"""
        if order_id not in self.pending_orders:
            return

        record = self.pending_orders.pop(order_id)
        now = datetime.now(timezone.utc)

        record.outcome = "filled"
        record.outcome_at = now.isoformat()
        record.outcome_at_unix = now.timestamp()
        record.time_to_outcome_seconds = now.timestamp() - record.placed_at_unix

        # Record fair value at fill for adverse selection analysis
        if market and market.fair_value:
            record.fair_value_at_outcome = market.fair_value
            # Positive = price moved in our favor (we bought cheap and it went up)
            if record.side == "yes":
                record.fair_value_change = market.fair_value - record.fair_value
            else:
                record.fair_value_change = record.fair_value - market.fair_value

        self.completed_orders.append(record)
        self._save_data()
        self._model_params = self._calculate_model_params()

        print(f"[TRACKER] Filled: {record.side.upper()} @ {record.price}c "
              f"(took {record.time_to_outcome_seconds:.0f}s, "
              f"adverse selection: {(record.fair_value_change or 0)*100:+.1f}%)")

    def record_partial_fill(self, order_id: str, filled_count: int, total_size: int, market=None):
        """
        Record a partial fill (some contracts filled, order still resting).

        Unlike record_fill(), this doesn't move the order to completed - it just
        logs the partial fill for tracking purposes. The order remains pending
        until it's fully filled or cancelled.

        Args:
            order_id: The order ID
            filled_count: Number of contracts filled in this batch
            total_size: Original total order size
            market: Current market data (for adverse selection tracking)
        """
        now = datetime.now(timezone.utc)

        # Get the order record if we have it
        record = self.pending_orders.get(order_id)

        # Calculate adverse selection if we have market data
        adverse_selection = None
        if record and market and market.fair_value:
            if record.side == "yes":
                adverse_selection = market.fair_value - record.fair_value
            else:
                adverse_selection = record.fair_value - market.fair_value

        # Log the partial fill
        if record:
            fill_pct = filled_count / total_size * 100
            print(f"[TRACKER] Partial fill: {record.side.upper()} {filled_count}/{total_size} @ {record.price}c "
                  f"({fill_pct:.0f}% of order, "
                  f"adverse selection: {(adverse_selection or 0)*100:+.1f}%)")
        else:
            # Order not in our tracking - log anyway
            print(f"[TRACKER] Partial fill: {filled_count}/{total_size} contracts (order not tracked)")

    def record_cancel(self, order_id: str, reason: str = ""):
        """Record that an order was cancelled"""
        if order_id not in self.pending_orders:
            return

        record = self.pending_orders.pop(order_id)
        now = datetime.now(timezone.utc)

        record.outcome = "cancelled"
        record.outcome_at = now.isoformat()
        record.outcome_at_unix = now.timestamp()
        record.time_to_outcome_seconds = now.timestamp() - record.placed_at_unix

        self.completed_orders.append(record)
        self._save_data()
        self._model_params = self._calculate_model_params()

    def record_expired(self, order_id: str):
        """Record that an order expired without filling"""
        if order_id not in self.pending_orders:
            return

        record = self.pending_orders.pop(order_id)
        now = datetime.now(timezone.utc)

        record.outcome = "expired"
        record.outcome_at = now.isoformat()
        record.outcome_at_unix = now.timestamp()
        record.time_to_outcome_seconds = now.timestamp() - record.placed_at_unix

        self.completed_orders.append(record)
        self._save_data()
        self._model_params = self._calculate_model_params()

    def record_settlement(self, ticker: str, won: bool, pnl: float = None):
        """
        Record settlement outcome for filled orders on a market.

        This tracks whether our positions actually won (settled in our favor)
        or lost, which is critical for measuring true adverse selection.

        Args:
            ticker: Market ticker that settled
            won: True if position settled favorably (YES=1 for YES positions, YES=0 for NO)
            pnl: Actual P&L in dollars (optional)
        """
        # Update all completed orders for this ticker
        updated = 0
        for record in self.completed_orders:
            if record.ticker == ticker and record.outcome == "filled":
                record.settlement_outcome = "won" if won else "lost"
                if pnl is not None:
                    record.settlement_pnl = pnl
                updated += 1

        if updated > 0:
            self._save_data()
            print(f"[TRACKER] Settlement recorded for {ticker}: {'WON' if won else 'LOST'} ({updated} orders)")

    def get_settlement_stats(self) -> dict:
        """Get statistics on settlement outcomes"""
        settled = [r for r in self.completed_orders
                   if r.outcome == "filled" and r.settlement_outcome is not None]

        if not settled:
            return {"total_settled": 0, "win_rate": None, "total_pnl": None}

        wins = sum(1 for r in settled if r.settlement_outcome == "won")
        total_pnl = sum(r.settlement_pnl or 0 for r in settled)

        return {
            "total_settled": len(settled),
            "wins": wins,
            "losses": len(settled) - wins,
            "win_rate": wins / len(settled) if settled else None,
            "total_pnl": total_pnl,
        }

    def _calculate_model_params(self) -> dict:
        """Calculate fill probability model parameters from historical data"""
        if len(self.completed_orders) < 10:
            # Not enough data, return conservative defaults for Kalshi's thin markets
            # Old (optimistic): {5: 0.70, 15: 0.40, 30: 0.20, 100: 0.05}
            # New (conservative): lower fill rates to account for thin liquidity
            return {
                "spread_buckets": {5: 0.50, 15: 0.25, 30: 0.10, 100: 0.03},
                "expiry_multiplier": {},
                "volume_multiplier": {},
                "base_fill_rate": 0.20,  # Conservative default
                "sample_size": len(self.completed_orders),
            }

        # Calculate fill rates by spread bucket
        spread_buckets = {5: [], 15: [], 30: [], 100: []}
        expiry_buckets = {2: [], 6: [], 12: [], 24: [], 100: []}  # hours
        volume_buckets = {100: [], 1000: [], 10000: [], 100000: []}

        fill_times = []
        adverse_selections = []

        for record in self.completed_orders:
            is_fill = 1 if record.outcome == "filled" else 0

            # Spread bucket
            for threshold in sorted(spread_buckets.keys()):
                if record.spread <= threshold:
                    spread_buckets[threshold].append(is_fill)
                    break

            # Expiry bucket
            if record.hours_to_expiry is not None:
                for threshold in sorted(expiry_buckets.keys()):
                    if record.hours_to_expiry <= threshold:
                        expiry_buckets[threshold].append(is_fill)
                        break

            # Volume bucket
            for threshold in sorted(volume_buckets.keys()):
                if record.volume <= threshold:
                    volume_buckets[threshold].append(is_fill)
                    break

            # Fill time and adverse selection
            if record.outcome == "filled":
                if record.time_to_outcome_seconds:
                    fill_times.append(record.time_to_outcome_seconds)
                if record.fair_value_change is not None:
                    adverse_selections.append(record.fair_value_change)

        # Calculate rates
        def safe_rate(lst):
            return sum(lst) / len(lst) if lst else 0.2

        params = {
            "spread_buckets": {k: safe_rate(v) for k, v in spread_buckets.items()},
            "expiry_buckets": {k: safe_rate(v) for k, v in expiry_buckets.items()},
            "volume_buckets": {k: safe_rate(v) for k, v in volume_buckets.items()},
            "base_fill_rate": safe_rate([1 if r.outcome == "filled" else 0 for r in self.completed_orders]),
            "avg_fill_time_seconds": sum(fill_times) / len(fill_times) if fill_times else 0,
            "avg_adverse_selection": sum(adverse_selections) / len(adverse_selections) if adverse_selections else 0,
            "sample_size": len(self.completed_orders),
        }

        return params

    def predict_fill_probability(self, market, price: int, side: str) -> float:
        """
        Predict fill probability using learned parameters.

        Returns probability between 0 and 1.
        """
        params = self._model_params

        # Calculate spread (distance from our bid to the ask)
        if side == "yes":
            spread = (market.yes_ask or 100) - price
        else:
            # Derive NO ask from YES bid when no_ask is missing/zero
            no_ask = market.no_ask
            if not no_ask and market.yes_bid:
                no_ask = 100 - market.yes_bid
            spread = (no_ask or 100) - price

        # Base probability from spread (conservative defaults for Kalshi's thin markets)
        spread_probs = params.get("spread_buckets", {5: 0.50, 15: 0.25, 30: 0.10, 100: 0.03})
        base_prob = 0.05
        for threshold in sorted(spread_probs.keys()):
            if spread <= threshold:
                base_prob = spread_probs[threshold]
                break

        # Adjust for time to expiry
        if market.close_time:
            delta = market.close_time - datetime.now(timezone.utc)
            hours = max(0, delta.total_seconds() / 3600)

            expiry_probs = params.get("expiry_buckets", {})
            if expiry_probs:
                for threshold in sorted(expiry_probs.keys()):
                    if hours <= threshold:
                        # Blend with expiry-specific rate
                        expiry_rate = expiry_probs[threshold]
                        base_prob = (base_prob + expiry_rate) / 2
                        break

        # Adjust for volume
        volume = market.volume or 0
        volume_probs = params.get("volume_buckets", {})
        if volume_probs:
            for threshold in sorted(volume_probs.keys()):
                if volume <= threshold:
                    volume_rate = volume_probs[threshold]
                    # Higher volume = higher fill prob (slight adjustment)
                    base_prob = base_prob * (0.8 + 0.4 * volume_rate)
                    break

        # Clamp to valid range
        return max(0.01, min(0.95, base_prob))

    def get_stats(self) -> FillStats:
        """Get aggregated fill statistics"""
        stats = FillStats()

        stats.total_orders = len(self.completed_orders)
        stats.filled = sum(1 for r in self.completed_orders if r.outcome == "filled")
        stats.cancelled = sum(1 for r in self.completed_orders if r.outcome == "cancelled")
        stats.expired = sum(1 for r in self.completed_orders if r.outcome == "expired")
        stats.pending = len(self.pending_orders)

        if stats.total_orders > 0:
            stats.fill_rate = stats.filled / stats.total_orders

        # Average time to fill
        fill_times = [r.time_to_outcome_seconds for r in self.completed_orders
                      if r.outcome == "filled" and r.time_to_outcome_seconds]
        if fill_times:
            stats.avg_time_to_fill_seconds = sum(fill_times) / len(fill_times)

        # Adverse selection
        adverse = [r.fair_value_change for r in self.completed_orders
                   if r.outcome == "filled" and r.fair_value_change is not None]
        if adverse:
            stats.avg_adverse_selection = sum(adverse) / len(adverse)

        # Fill rates by bucket
        stats.fill_rate_by_spread = self._model_params.get("spread_buckets", {})
        stats.fill_rate_by_expiry = self._model_params.get("expiry_buckets", {})
        stats.fill_rate_by_volume = self._model_params.get("volume_buckets", {})

        return stats

    def format_stats(self) -> str:
        """Format statistics for display"""
        stats = self.get_stats()
        params = self._model_params

        lines = [
            "=" * 60,
            "FILL PROBABILITY TRACKER - STATISTICS",
            "=" * 60,
            f"",
            f"SAMPLE SIZE: {stats.total_orders} orders ({stats.pending} pending)",
            f"",
            f"OUTCOMES:",
            f"  Filled:    {stats.filled:4d} ({stats.fill_rate*100:5.1f}%)",
            f"  Cancelled: {stats.cancelled:4d}",
            f"  Expired:   {stats.expired:4d}",
            f"",
            f"TIMING:",
            f"  Avg time to fill: {stats.avg_time_to_fill_seconds:.0f} seconds",
            f"",
            f"ADVERSE SELECTION:",
            f"  Avg fair value change after fill: {stats.avg_adverse_selection*100:+.2f}%",
            f"  (negative = filled when price moved against us)",
            f"",
        ]

        if params.get("sample_size", 0) >= 10:
            lines.extend([
                f"LEARNED FILL RATES BY SPREAD:",
            ])
            for spread, rate in sorted(params.get("spread_buckets", {}).items()):
                lines.append(f"  Spread <= {spread:2d}c: {rate*100:5.1f}%")

            lines.append(f"")
            lines.append(f"LEARNED FILL RATES BY TIME TO EXPIRY:")
            for hours, rate in sorted(params.get("expiry_buckets", {}).items()):
                lines.append(f"  <= {hours:2d} hours: {rate*100:5.1f}%")

            lines.append(f"")
            lines.append(f"LEARNED FILL RATES BY VOLUME:")
            for vol, rate in sorted(params.get("volume_buckets", {}).items()):
                lines.append(f"  <= {vol:6d} contracts: {rate*100:5.1f}%")
        else:
            lines.append(f"(Need 10+ orders to calculate learned rates)")

        lines.append("=" * 60)

        return "\n".join(lines)


# Global tracker instance
_tracker: Optional[FillTracker] = None


def get_tracker() -> FillTracker:
    """Get or create the global fill tracker"""
    global _tracker
    if _tracker is None:
        _tracker = FillTracker()
    return _tracker
