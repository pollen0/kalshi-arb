"""
Risk Manager for Kalshi Arbitrage System

Handles:
1. Correlated exposure limits (group NASDAQ markets together, SPX together)
2. Mark-to-market equity calculation (use current prices, not avg_price)
3. Daily P&L tracking based on true portfolio value
4. Position and order limits
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple
from ..core.models import Market, Position, Side
from ..core.config import get_risk_config, get_asset_class_config


@dataclass
class AssetClassExposure:
    """Exposure summary for an asset class"""
    asset_class: str
    long_exposure: float = 0.0   # Total long position value
    short_exposure: float = 0.0  # Total short position value (for NO positions)
    net_exposure: float = 0.0    # Long - Short
    gross_exposure: float = 0.0  # Long + Short
    position_count: int = 0
    tickers: List[str] = field(default_factory=list)


@dataclass
class PortfolioSnapshot:
    """Complete portfolio state at a point in time"""
    timestamp: datetime
    cash_balance: float
    position_mtm: float          # Mark-to-market value of all positions
    total_equity: float          # Cash + MTM
    gross_exposure: float        # Total absolute exposure
    net_exposure: float          # Net directional exposure

    # By asset class
    exposure_by_class: Dict[str, AssetClassExposure] = field(default_factory=dict)

    # Pending orders
    pending_order_exposure: float = 0.0

    # Daily tracking
    starting_equity: Optional[float] = None
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0


class RiskManager:
    """
    Centralized risk management for the trading system.

    Key responsibilities:
    1. Calculate true mark-to-market equity
    2. Track correlated exposure across asset classes
    3. Enforce position and loss limits
    4. Provide risk checks before order placement
    """

    # Asset class mapping for Kalshi series tickers
    SERIES_TO_ASSET_CLASS = {
        "KXNASDAQ100": "nasdaq",
        "KXNASDAQ100U": "nasdaq",
        "KXINX": "spx",
        "KXINXU": "spx",
        "KXTNOTED": "treasury",
    }

    def __init__(self):
        self.config = get_risk_config()
        self.asset_config = get_asset_class_config()

        # Daily tracking
        self._starting_equity: Optional[float] = None
        self._starting_date: Optional[datetime] = None

        # Cache for market data (for MTM calculations)
        self._markets_cache: Dict[str, Market] = {}

    def update_markets_cache(self, markets: List[Market]):
        """Update cache of current market prices for MTM calculation"""
        for m in markets:
            self._markets_cache[m.ticker] = m

    def get_asset_class(self, ticker: str) -> str:
        """Get asset class for a ticker"""
        # Match against known series prefixes directly
        # Tickers look like KXNASDAQ100U-26FEB04H1200-B21250
        for series, asset_class in self.SERIES_TO_ASSET_CLASS.items():
            if ticker.startswith(series):
                return asset_class

        # Fallback: keyword-based matching as safety net
        ticker_upper = ticker.upper()
        if "NASDAQ" in ticker_upper or "NDX" in ticker_upper or "NQ" in ticker_upper:
            return "nasdaq"
        elif "INX" in ticker_upper or "SPX" in ticker_upper or "SP" in ticker_upper:
            return "spx"
        elif "TNOTE" in ticker_upper or "TREASURY" in ticker_upper:
            return "treasury"

        return "unknown"

    def get_asset_class_from_series(self, series_ticker: str) -> str:
        """Get asset class from series ticker"""
        return self.SERIES_TO_ASSET_CLASS.get(series_ticker, "unknown")

    def calculate_position_mtm(self, position: Position,
                                current_market: Optional[Market] = None) -> float:
        """
        Calculate mark-to-market value of a position.

        Uses current market prices (not avg_price) for true valuation.

        For YES positions: value = quantity * current_yes_bid (exit price)
        For NO positions: value = quantity * current_no_bid (exit price)

        Args:
            position: Position to value
            current_market: Current market data (uses cache if not provided)

        Returns:
            MTM value in dollars
        """
        if current_market is None:
            current_market = self._markets_cache.get(position.ticker)

        if current_market is None:
            # Fall back to using position's own price data or avg_price
            if position.current_bid is not None:
                if position.side == Side.YES:
                    return position.quantity * position.current_bid
                else:
                    # For NO, current_bid is YES bid, so NO value = 1 - YES ask
                    no_bid = 1 - (position.current_ask or position.current_bid)
                    return position.quantity * no_bid
            else:
                # Last resort: use avg_price (not ideal for MTM)
                return position.quantity * position.avg_price

        # Use current market prices for MTM
        if position.side == Side.YES:
            # YES position exits at YES bid
            exit_price = (current_market.yes_bid or 0) / 100
            return position.quantity * exit_price
        else:
            # NO position exits at NO bid = 100 - YES ask
            no_bid = (100 - (current_market.yes_ask or 100)) / 100
            return position.quantity * no_bid

    def calculate_position_unrealized_pnl(self, position: Position,
                                           current_market: Optional[Market] = None) -> float:
        """
        Calculate unrealized P&L for a position.

        Returns:
            P&L in dollars (positive = profit)
        """
        mtm = self.calculate_position_mtm(position, current_market)
        cost_basis = position.quantity * position.avg_price
        return mtm - cost_basis

    def calculate_total_equity(self, cash_balance: float,
                                positions: List[Position]) -> float:
        """
        Calculate total portfolio equity = cash + MTM of all positions.

        This is the true account value, not just cash balance.
        """
        position_mtm = sum(
            self.calculate_position_mtm(pos) for pos in positions
        )
        return cash_balance + position_mtm

    def get_exposure_by_asset_class(self,
                                     positions: List[Position]) -> Dict[str, AssetClassExposure]:
        """
        Calculate exposure grouped by asset class.

        This is critical for managing correlated risk - all NASDAQ markets
        move together, so we need to limit total NASDAQ exposure.
        """
        exposures: Dict[str, AssetClassExposure] = {}

        for pos in positions:
            asset_class = self.get_asset_class(pos.ticker)

            if asset_class not in exposures:
                exposures[asset_class] = AssetClassExposure(asset_class=asset_class)

            exp = exposures[asset_class]
            mtm = self.calculate_position_mtm(pos)

            if pos.side == Side.YES:
                exp.long_exposure += mtm
            else:
                exp.short_exposure += mtm

            exp.position_count += 1
            exp.tickers.append(pos.ticker)

        # Calculate net and gross for each class
        for exp in exposures.values():
            exp.net_exposure = exp.long_exposure - exp.short_exposure
            exp.gross_exposure = exp.long_exposure + exp.short_exposure

        return exposures

    def check_exposure_limits(self, positions: List[Position],
                               total_equity: float) -> Tuple[bool, str]:
        """
        Check if exposure limits are breached.

        Returns:
            (is_ok, message) - is_ok=True if within limits
        """
        if total_equity <= 0:
            return (False, "Zero or negative equity")

        exposures = self.get_exposure_by_asset_class(positions)

        # Check per-asset-class limits
        for asset_class, exp in exposures.items():
            exposure_pct = exp.gross_exposure / total_equity

            if exposure_pct > self.config.max_exposure_per_asset_class:
                return (False, f"{asset_class} exposure {exposure_pct*100:.1f}% > "
                              f"{self.config.max_exposure_per_asset_class*100:.0f}% limit")

        # Check total exposure limit
        total_gross = sum(exp.gross_exposure for exp in exposures.values())
        total_exposure_pct = total_gross / total_equity

        if total_exposure_pct > self.config.max_total_exposure:
            return (False, f"Total exposure {total_exposure_pct*100:.1f}% > "
                          f"{self.config.max_total_exposure*100:.0f}% limit")

        return (True, "")

    def check_daily_loss_limit(self, current_equity: float) -> Tuple[bool, str]:
        """
        Check if daily loss limit is breached.

        Returns:
            (is_ok, message)
        """
        if self._starting_equity is None:
            return (True, "")

        daily_pnl = current_equity - self._starting_equity
        daily_pnl_pct = daily_pnl / self._starting_equity

        if daily_pnl_pct < -self.config.max_daily_loss_pct:
            return (False, f"Daily loss {daily_pnl_pct*100:.1f}% exceeds "
                          f"{self.config.max_daily_loss_pct*100:.0f}% limit")

        if daily_pnl_pct < -self.config.warning_daily_loss_pct:
            # Warning but don't block
            print(f"[RISK] Warning: Daily loss {daily_pnl_pct*100:.1f}%")

        return (True, "")

    def check_new_order(self, ticker: str, side: str, size: int, price: int,
                        positions: List[Position], total_equity: float,
                        pending_orders: List = None) -> Tuple[bool, str]:
        """
        Check if a new order can be placed without breaching limits.

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            size: Number of contracts
            price: Price in cents
            positions: Current positions
            total_equity: Current total equity
            pending_orders: List of pending orders

        Returns:
            (can_place, reason)
        """
        if total_equity <= 0:
            return (False, "Zero or negative equity")

        # Calculate order cost
        order_cost = size * price / 100  # In dollars

        # Check if order exceeds per-market limit
        max_market_value = total_equity * self.config.max_position_value_per_market
        current_position_value = 0

        for pos in positions:
            if pos.ticker == ticker:
                current_position_value = self.calculate_position_mtm(pos)
                break

        if current_position_value + order_cost > max_market_value:
            return (False, f"Would exceed per-market limit: "
                          f"${current_position_value + order_cost:.0f} > ${max_market_value:.0f}")

        # Check asset class exposure
        asset_class = self.get_asset_class(ticker)
        exposures = self.get_exposure_by_asset_class(positions)

        current_class_exposure = 0
        if asset_class in exposures:
            current_class_exposure = exposures[asset_class].gross_exposure

        max_class_exposure = total_equity * self.config.max_exposure_per_asset_class

        if current_class_exposure + order_cost > max_class_exposure:
            return (False, f"Would exceed {asset_class} exposure limit: "
                          f"${current_class_exposure + order_cost:.0f} > ${max_class_exposure:.0f}")

        # Check total exposure
        total_current_exposure = sum(exp.gross_exposure for exp in exposures.values())
        pending_exposure = 0
        if pending_orders:
            # Use remaining_count (unfilled remainder) instead of total size
            # to avoid double-counting already-filled portions as pending exposure
            pending_exposure = sum(
                getattr(o, 'remaining_count', o.size) * o.price / 100
                for o in pending_orders
            )

        max_total = total_equity * self.config.max_total_exposure

        if total_current_exposure + pending_exposure + order_cost > max_total:
            return (False, f"Would exceed total exposure limit")

        return (True, "")

    def get_portfolio_snapshot(self, cash_balance: float,
                                positions: List[Position],
                                pending_orders: List = None) -> PortfolioSnapshot:
        """
        Get complete portfolio snapshot with all risk metrics.
        """
        position_mtm = sum(self.calculate_position_mtm(pos) for pos in positions)
        total_equity = cash_balance + position_mtm

        exposures = self.get_exposure_by_asset_class(positions)
        gross_exposure = sum(exp.gross_exposure for exp in exposures.values())
        net_exposure = sum(exp.net_exposure for exp in exposures.values())

        pending_exposure = 0
        if pending_orders:
            # Use remaining_count (unfilled remainder) instead of total size
            pending_exposure = sum(
                getattr(o, 'remaining_count', o.size) * o.price / 100
                for o in pending_orders
            )

        # Daily P&L
        daily_pnl = 0
        daily_pnl_pct = 0
        if self._starting_equity and self._starting_equity > 0:
            daily_pnl = total_equity - self._starting_equity
            daily_pnl_pct = daily_pnl / self._starting_equity

        return PortfolioSnapshot(
            timestamp=datetime.now(timezone.utc),
            cash_balance=cash_balance,
            position_mtm=position_mtm,
            total_equity=total_equity,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            exposure_by_class=exposures,
            pending_order_exposure=pending_exposure,
            starting_equity=self._starting_equity,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
        )

    def initialize_daily_tracking(self, cash_balance: float, positions: List[Position]):
        """Initialize daily P&L tracking (call at start of day or on first trade)"""
        total_equity = self.calculate_total_equity(cash_balance, positions)
        self._starting_equity = total_equity
        self._starting_date = datetime.now(timezone.utc).date()
        print(f"[RISK] Daily tracking initialized: Starting equity = ${total_equity:.2f}")

    def reset_daily_tracking(self, cash_balance: float, positions: List[Position]):
        """Reset daily tracking (call at start of new trading day)"""
        self.initialize_daily_tracking(cash_balance, positions)

    def should_reset_daily_tracking(self) -> bool:
        """Check if daily tracking should be reset (new day)"""
        if self._starting_date is None:
            return True
        current_date = datetime.now(timezone.utc).date()
        return current_date > self._starting_date

    def run_all_checks(self, cash_balance: float, positions: List[Position],
                       pending_orders: List = None) -> Tuple[bool, List[str]]:
        """
        Run all risk checks.

        Returns:
            (is_ok, list_of_issues)
        """
        issues = []

        total_equity = self.calculate_total_equity(cash_balance, positions)

        # Check exposure limits
        ok, msg = self.check_exposure_limits(positions, total_equity)
        if not ok:
            issues.append(msg)

        # Check daily loss limit
        ok, msg = self.check_daily_loss_limit(total_equity)
        if not ok:
            issues.append(msg)

        return (len(issues) == 0, issues)


# Global instance
_risk_manager: Optional[RiskManager] = None


def get_risk_manager() -> RiskManager:
    """Get or create global risk manager"""
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = RiskManager()
    return _risk_manager
