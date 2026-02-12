"""
Position Manager - Decides when to hold vs exit positions

Exit Strategy Framework:
1. HOLD to expiration when high confidence (FV > 75% or < 25%)
2. EXIT early when:
   - Loss cutting: FV moved against us significantly
   - Profit taking: FV is uncertain (40-60%) but we have profit
   - Risk management: Overexposed to one asset class
   - Liquidity: Can exit at good price before spread widens
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Tuple
from enum import Enum
from ..core.models import Market, Position, Side


class ExitReason(Enum):
    """Reasons for recommending position exit"""
    HOLD = "hold"                      # Keep position to expiration
    LOSS_CUT = "loss_cut"              # Fair value moved against us
    PROFIT_TAKE = "profit_take"        # Lock in profit on uncertain outcome
    EXPOSURE_LIMIT = "exposure_limit"  # Reduce concentrated exposure
    NEAR_EXPIRY_UNCERTAIN = "near_expiry_uncertain"  # Too close to expiry with uncertain FV
    LIQUIDITY = "liquidity"            # Exit while liquidity is good


@dataclass
class ExitRecommendation:
    """Recommendation for a position"""
    ticker: str
    position: Position
    action: str  # "HOLD", "EXIT", "REDUCE"
    reason: ExitReason
    urgency: str  # "HIGH", "MEDIUM", "LOW"

    # For EXIT/REDUCE actions
    suggested_size: int = 0  # How many contracts to exit
    suggested_price: int = 0  # Limit price for exit order (cents)

    # Analysis
    current_fair_value: Optional[float] = None
    entry_price: float = 0
    current_exit_price: float = 0  # What we'd get if we sell now
    unrealized_pnl: float = 0
    expected_settlement_pnl: float = 0  # P&L if held to expiry at FV
    hours_to_expiry: Optional[float] = None

    reasoning: str = ""


@dataclass
class PositionManagerConfig:
    """Configuration for position manager"""
    # Fair value thresholds for hold vs exit
    high_confidence_threshold: float = 0.75  # FV > 75% = hold YES
    low_confidence_threshold: float = 0.25   # FV < 25% = hold NO
    uncertain_zone_low: float = 0.40         # 40-60% = uncertain
    uncertain_zone_high: float = 0.60

    # Loss cutting
    max_adverse_move: float = 0.15           # Cut if FV moved 15%+ against us
    loss_cut_threshold_pct: float = -0.30    # Cut if down 30%+ on position

    # Profit taking
    min_profit_to_take: float = 0.10         # Take profit if up 10%+ in uncertain zone
    min_profit_dollars: float = 5.0          # Minimum absolute profit floor (for small positions)
    profit_take_pct_of_value: float = 0.08   # Take profit at 8% of position value (scales with size)

    # Time-based
    uncertain_expiry_hours: float = 2.0      # If < 2 hours and uncertain, consider exit

    # Exposure limits (per asset class as % of portfolio)
    max_exposure_per_class: float = 0.30     # Max 30% in any one asset class

    # Exit execution
    exit_spread_tolerance: int = 3           # Accept up to 3c worse than mid


class PositionManager:
    """
    Manages existing positions and recommends exits.

    Philosophy:
    - Binary options have asymmetric payoffs (0 or 100)
    - High confidence positions (FV > 75%) should usually be held
    - Uncertain positions (FV 40-60%) are risky - consider locking profit
    - Adverse selection means our fills often precede bad moves
    - Better to exit early with profit than hold and lose
    """

    def __init__(self, config: PositionManagerConfig = None):
        self.config = config or PositionManagerConfig()

    def analyze_position(self, position: Position, market: Market) -> ExitRecommendation:
        """
        Analyze a single position and recommend action.

        Args:
            position: Current position
            market: Current market data with fair value

        Returns:
            ExitRecommendation with suggested action
        """
        fv = market.fair_value
        if fv is None:
            # Can't analyze without fair value - default to hold
            return ExitRecommendation(
                ticker=position.ticker,
                position=position,
                action="HOLD",
                reason=ExitReason.HOLD,
                urgency="LOW",
                reasoning="No fair value available"
            )

        # Calculate key metrics
        is_yes = position.side == Side.YES
        entry_price = position.avg_price

        # Current exit price (what we'd get selling now)
        if is_yes:
            exit_price = (market.yes_bid or 0) / 100  # Sell YES at bid
        else:
            exit_price = (100 - (market.yes_ask or 100)) / 100  # Sell NO = 100 - yes_ask

        # Calculate P&L
        unrealized_pnl = (exit_price - entry_price) * position.quantity
        unrealized_pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0

        # Expected P&L if held to expiry (based on fair value)
        if is_yes:
            expected_exit = fv  # YES pays $1 if market settles YES
            expected_pnl = (expected_exit - entry_price) * position.quantity
        else:
            expected_exit = 1 - fv  # NO pays $1 if market settles NO
            expected_pnl = (expected_exit - entry_price) * position.quantity

        # Time to expiry
        hours_to_expiry = None
        if market.close_time:
            delta = market.close_time - datetime.now(timezone.utc)
            hours_to_expiry = max(0, delta.total_seconds() / 3600)

        # Fair value move since entry
        # Use fv_at_entry (actual FV when position was opened) for accurate loss-cut
        # Falls back to entry_price if fv_at_entry not available (legacy positions)
        fv_reference = position.fv_at_entry if position.fv_at_entry is not None else entry_price
        # Positive = moved in our favor
        if is_yes:
            fv_move = fv - fv_reference  # Higher FV = good for YES
        else:
            fv_move = fv_reference - fv  # Lower FV = good for NO

        # Build base recommendation
        rec = ExitRecommendation(
            ticker=position.ticker,
            position=position,
            action="HOLD",
            reason=ExitReason.HOLD,
            urgency="LOW",
            current_fair_value=fv,
            entry_price=entry_price,
            current_exit_price=exit_price,
            unrealized_pnl=unrealized_pnl,
            expected_settlement_pnl=expected_pnl,
            hours_to_expiry=hours_to_expiry,
        )

        # Decision logic

        # 1. LOSS CUTTING - FV moved significantly against us
        if fv_move < -self.config.max_adverse_move:
            rec.action = "EXIT"
            rec.reason = ExitReason.LOSS_CUT
            rec.urgency = "HIGH"
            rec.suggested_size = position.quantity
            rec.suggested_price = int(exit_price * 100)
            rec.reasoning = f"FV moved {fv_move*100:+.1f}% against position. Cut loss."
            return rec

        # Also cut if unrealized loss exceeds threshold
        if unrealized_pnl_pct < self.config.loss_cut_threshold_pct:
            rec.action = "EXIT"
            rec.reason = ExitReason.LOSS_CUT
            rec.urgency = "HIGH"
            rec.suggested_size = position.quantity
            rec.suggested_price = int(exit_price * 100)
            rec.reasoning = f"Position down {unrealized_pnl_pct*100:.1f}%. Cut loss."
            return rec

        # 2. HIGH CONFIDENCE - Hold to expiration
        if is_yes and fv >= self.config.high_confidence_threshold:
            rec.action = "HOLD"
            rec.reason = ExitReason.HOLD
            rec.urgency = "LOW"
            rec.reasoning = f"High confidence (FV={fv:.0%}). Hold for max profit."
            return rec

        if not is_yes and fv <= self.config.low_confidence_threshold:
            rec.action = "HOLD"
            rec.reason = ExitReason.HOLD
            rec.urgency = "LOW"
            rec.reasoning = f"High confidence (FV={fv:.0%}, NO favored). Hold for max profit."
            return rec

        # 3. UNCERTAIN ZONE - Consider profit taking
        in_uncertain_zone = self.config.uncertain_zone_low <= fv <= self.config.uncertain_zone_high

        # Scale profit-take threshold by position value: larger positions need larger
        # absolute profit to justify the exit cost, but always enforce a minimum floor.
        position_value = entry_price * position.quantity
        scaled_profit_threshold = max(
            self.config.min_profit_dollars,
            position_value * self.config.profit_take_pct_of_value,
        )
        has_profit = unrealized_pnl > scaled_profit_threshold
        profit_pct_ok = unrealized_pnl_pct >= self.config.min_profit_to_take

        if in_uncertain_zone and has_profit and profit_pct_ok:
            rec.action = "EXIT"
            rec.reason = ExitReason.PROFIT_TAKE
            rec.urgency = "MEDIUM"
            rec.suggested_size = position.quantity
            rec.suggested_price = int(exit_price * 100)
            rec.reasoning = (f"Uncertain FV ({fv:.0%}) with ${unrealized_pnl:.2f} profit "
                           f"({unrealized_pnl_pct*100:+.1f}%). Threshold: "
                           f"${scaled_profit_threshold:.2f}. Lock in gains.")
            return rec

        # 4. NEAR EXPIRY + UNCERTAIN - Higher risk
        if hours_to_expiry is not None and hours_to_expiry < self.config.uncertain_expiry_hours:
            if in_uncertain_zone:
                # Near expiry with uncertain outcome - risky
                if has_profit:
                    rec.action = "EXIT"
                    rec.reason = ExitReason.NEAR_EXPIRY_UNCERTAIN
                    rec.urgency = "HIGH"
                    rec.suggested_size = position.quantity
                    rec.suggested_price = int(exit_price * 100)
                    rec.reasoning = (f"Only {hours_to_expiry:.1f}h to expiry with uncertain FV "
                                   f"({fv:.0%}). Take ${unrealized_pnl:.2f} profit.")
                    return rec
                else:
                    # No profit but uncertain near expiry - hold but flag risk
                    rec.action = "HOLD"
                    rec.reason = ExitReason.HOLD
                    rec.urgency = "MEDIUM"
                    rec.reasoning = (f"Near expiry ({hours_to_expiry:.1f}h) with uncertain FV. "
                                   f"No profit to take - holding.")
                    return rec

        # 5. DEFAULT - Hold position
        rec.action = "HOLD"
        rec.reason = ExitReason.HOLD
        rec.urgency = "LOW"

        if has_profit:
            rec.reasoning = f"FV={fv:.0%}, up ${unrealized_pnl:.2f}. Holding for higher expected value."
        else:
            rec.reasoning = f"FV={fv:.0%}, position at ${unrealized_pnl:.2f}. Holding."

        return rec

    def analyze_all_positions(self, positions: List[Position],
                               markets: dict) -> List[ExitRecommendation]:
        """
        Analyze all positions and return recommendations.

        Args:
            positions: List of current positions
            markets: Dict of ticker -> Market

        Returns:
            List of ExitRecommendations, sorted by urgency
        """
        recommendations = []

        for pos in positions:
            market = markets.get(pos.ticker)
            if market:
                rec = self.analyze_position(pos, market)
                recommendations.append(rec)
            else:
                # No market data - can't analyze
                recommendations.append(ExitRecommendation(
                    ticker=pos.ticker,
                    position=pos,
                    action="HOLD",
                    reason=ExitReason.HOLD,
                    urgency="LOW",
                    reasoning="No market data available"
                ))

        # Sort by urgency (HIGH first) and then by unrealized P&L (losses first)
        urgency_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        recommendations.sort(key=lambda r: (
            urgency_order.get(r.urgency, 2),
            r.unrealized_pnl  # More negative = higher priority
        ))

        return recommendations

    def get_exit_orders(self, positions: List[Position],
                        markets: dict) -> List[dict]:
        """
        Get list of exit orders to place.

        Returns:
            List of order dicts ready for placement
        """
        recommendations = self.analyze_all_positions(positions, markets)

        exit_orders = []
        for rec in recommendations:
            if rec.action in ["EXIT", "REDUCE"] and rec.suggested_size > 0:
                # Determine order side (opposite of position)
                if rec.position.side == Side.YES:
                    # On Kalshi, to close a YES position you sell YES
                    order_side = "yes"
                    order_action = "sell"
                else:
                    order_side = "no"
                    order_action = "sell"

                exit_orders.append({
                    "ticker": rec.ticker,
                    "side": order_side,
                    "action": order_action,
                    "size": rec.suggested_size,
                    "price": rec.suggested_price,
                    "reason": rec.reason.value,
                    "reasoning": rec.reasoning,
                    "urgency": rec.urgency,
                })

        return exit_orders

    def format_recommendations(self, recommendations: List[ExitRecommendation]) -> str:
        """Format recommendations for display"""
        if not recommendations:
            return "No positions to analyze"

        lines = [
            "=" * 70,
            "POSITION ANALYSIS",
            "=" * 70,
            ""
        ]

        # Group by action
        exits = [r for r in recommendations if r.action in ["EXIT", "REDUCE"]]
        holds = [r for r in recommendations if r.action == "HOLD"]

        if exits:
            lines.append("EXIT RECOMMENDATIONS:")
            lines.append("-" * 40)
            for rec in exits:
                side = rec.position.side.value.upper()
                lines.append(f"  [{rec.urgency}] {rec.ticker[:40]}")
                lines.append(f"      {side} {rec.position.quantity}x @ {rec.entry_price*100:.0f}c")
                lines.append(f"      FV: {rec.current_fair_value:.0%}, Exit: {rec.current_exit_price*100:.0f}c")
                lines.append(f"      P&L: ${rec.unrealized_pnl:+.2f}")
                lines.append(f"      >> {rec.reasoning}")
                lines.append("")

        if holds:
            lines.append("HOLD POSITIONS:")
            lines.append("-" * 40)
            for rec in holds:
                side = rec.position.side.value.upper()
                fv_str = f"{rec.current_fair_value:.0%}" if rec.current_fair_value else "N/A"
                lines.append(f"  {rec.ticker[:40]}")
                lines.append(f"      {side} {rec.position.quantity}x, FV: {fv_str}, P&L: ${rec.unrealized_pnl:+.2f}")
                lines.append(f"      >> {rec.reasoning}")
                lines.append("")

        lines.append("=" * 70)
        return "\n".join(lines)


# Global instance
_position_manager: Optional[PositionManager] = None


def get_position_manager() -> PositionManager:
    """Get or create global position manager"""
    global _position_manager
    if _position_manager is None:
        _position_manager = PositionManager()
    return _position_manager
