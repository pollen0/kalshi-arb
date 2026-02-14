"""
Core data models for Kalshi arbitrage system
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from enum import Enum


class MarketType(Enum):
    FINANCIAL_RANGE = "range"
    FINANCIAL_ABOVE = "above"
    FINANCIAL_BELOW = "below"


class Side(Enum):
    YES = "yes"
    NO = "no"


@dataclass
class OrderbookLevel:
    """Single price level in orderbook"""
    price: int      # cents (1-99)
    quantity: int   # contracts

    @property
    def price_pct(self) -> float:
        return self.price / 100


@dataclass
class Orderbook:
    """Full orderbook for a market"""
    ticker: str
    yes_bids: list[OrderbookLevel] = field(default_factory=list)
    yes_asks: list[OrderbookLevel] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def best_bid(self) -> Optional[int]:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_ask(self) -> Optional[int]:
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def spread(self) -> Optional[int]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.best_ask if self.best_ask is not None else self.best_bid

    @property
    def is_empty(self) -> bool:
        return not self.yes_bids and not self.yes_asks


@dataclass
class Market:
    """Kalshi market with pricing data"""
    ticker: str
    event_ticker: str
    title: str
    subtitle: str
    market_type: MarketType

    # Bounds (for financial range markets)
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None

    # Current prices (cents)
    yes_bid: int = 0
    yes_ask: int = 0
    no_bid: int = 0
    no_ask: int = 0

    # Volume and timing
    volume: int = 0
    close_time: Optional[datetime] = None

    # Calculated values
    fair_value: Optional[float] = None
    fair_value_time: Optional[datetime] = None  # When fair value was last computed
    model_source: Optional[str] = None

    # Full orderbook (optional)
    orderbook: Optional[Orderbook] = None

    @property
    def yes_mid(self) -> float:
        if self.yes_bid is not None and self.yes_bid > 0 and self.yes_ask is not None and self.yes_ask > 0:
            return (self.yes_bid + self.yes_ask) / 200  # Convert to probability
        return ((self.yes_ask if self.yes_ask else None) or (self.yes_bid if self.yes_bid else None) or 50) / 100

    @property
    def market_price(self) -> float:
        """Current market price for YES (matches Kalshi display - uses yes_ask)"""
        # Kalshi displays yes_ask as the "market price" for YES
        return (self.yes_ask or self.yes_bid or 50) / 100

    @property
    def yes_buy_price(self) -> float:
        """Price to BUY YES (yes_ask)"""
        return (self.yes_ask or 100) / 100

    @property
    def yes_sell_price(self) -> float:
        """Price to SELL YES (yes_bid)"""
        return (self.yes_bid or 0) / 100

    @property
    def no_buy_price(self) -> float:
        """
        Price to BUY NO.

        On Kalshi, buying NO costs (100 - yes_bid) cents.
        If yes_bid=40, buying NO costs 60 cents.
        """
        if self.yes_bid:
            return (100 - self.yes_bid) / 100
        return 1.0  # No bid means NO costs 100c

    @property
    def no_sell_price(self) -> float:
        """
        Price to SELL NO (what you receive).

        On Kalshi, selling NO gives you (100 - yes_ask) cents.
        If yes_ask=45, selling NO gives you 55 cents.
        """
        if self.yes_ask:
            return (100 - self.yes_ask) / 100
        return 0.0

    @property
    def no_market_price(self) -> float:
        """
        Current market price for NO (what it costs to BUY NO).

        IMPORTANT: This is the actual cost to buy NO, not just 1 - yes_ask.
        Buying NO = selling YES, so it costs (100 - yes_bid).
        """
        return self.no_buy_price

    @property
    def no_fair_value(self) -> Optional[float]:
        """Fair value for NO side"""
        if self.fair_value is None:
            return None
        return 1 - self.fair_value

    @property
    def yes_edge(self) -> Optional[float]:
        """
        Edge on YES side (positive = YES underpriced, buy YES).

        Edge = Fair Value - Buy Price
        If FV=50% and yes_ask=45c, edge = 0.50 - 0.45 = +5%
        """
        if self.fair_value is None:
            return None
        return self.fair_value - self.yes_buy_price

    @property
    def no_edge(self) -> Optional[float]:
        """
        Edge on NO side (positive = NO underpriced, buy NO).

        Edge = NO Fair Value - NO Buy Price
        If YES FV=50%, NO FV=50%, and yes_bid=40 (NO costs 60c):
        Edge = 0.50 - 0.60 = -10% (NO is overpriced)

        If YES FV=30%, NO FV=70%, and yes_bid=40 (NO costs 60c):
        Edge = 0.70 - 0.60 = +10% (NO is underpriced)
        """
        if self.fair_value is None:
            return None
        return self.no_fair_value - self.no_buy_price

    @property
    def best_edge(self) -> Optional[float]:
        """Best edge between YES and NO (always positive if opportunity exists)"""
        if self.fair_value is None:
            return None
        yes_e = self.yes_edge or 0
        no_e = self.no_edge or 0
        return max(yes_e, no_e)

    @property
    def best_side(self) -> Optional[str]:
        """Which side has the best edge: 'YES' or 'NO'"""
        if self.fair_value is None:
            return None
        yes_e = self.yes_edge or 0
        no_e = self.no_edge or 0
        return "YES" if yes_e >= no_e else "NO"

    @property
    def edge(self) -> Optional[float]:
        """Best edge (for backward compatibility)"""
        return self.best_edge

    @property
    def edge_pct(self) -> Optional[float]:
        """Edge as percentage"""
        return self.edge * 100 if self.edge else None


@dataclass
class Position:
    """Current position in a market"""
    ticker: str
    market_title: str
    side: Side
    quantity: int
    avg_price: float  # Average entry price (0-1)

    # Current market data
    current_bid: Optional[float] = None
    current_ask: Optional[float] = None
    fair_value: Optional[float] = None

    # P&L
    realized_pnl: float = 0

    # Fair value at time of entry (for loss-cut calculations)
    fv_at_entry: Optional[float] = None

    @property
    def current_mid(self) -> Optional[float]:
        if self.current_bid is not None and self.current_ask is not None:
            return (self.current_bid + self.current_ask) / 2
        return self.current_ask if self.current_ask is not None else self.current_bid

    @property
    def unrealized_pnl(self) -> Optional[float]:
        """
        Unrealized P&L in dollars.

        Matches Kalshi's calculation:
        - Cost = quantity * avg_price
        - Market Value = quantity * current_exit_price
        - Unrealized P&L = Market Value - Cost

        For YES positions: exit at current YES bid
        For NO positions: exit at current NO bid = (1 - YES ask)

        We use mid price as approximation since we don't always have bid/ask.
        """
        if self.current_mid is None:
            return None

        if self.side == Side.YES:
            # YES position: current value based on YES price
            # P&L = (current_yes_mid - avg_yes_price) * quantity
            return (self.current_mid - self.avg_price) * self.quantity
        else:
            # NO position: current value based on NO price
            # current_mid is YES mid, so NO mid = 1 - YES mid
            # P&L = (current_no_mid - avg_no_price) * quantity
            current_no_mid = 1 - self.current_mid
            return (current_no_mid - self.avg_price) * self.quantity

    @property
    def model_edge(self) -> Optional[float]:
        """Current edge vs model fair value"""
        if self.fair_value is None:
            return None

        if self.side == Side.YES:
            return self.fair_value - self.avg_price
        else:
            return self.avg_price - self.fair_value

    @property
    def exit_price(self) -> Optional[float]:
        """Price to exit position"""
        if self.side == Side.YES:
            return self.current_bid  # Sell YES at bid
        else:
            return self.current_ask  # Buy back NO at ask (or sell YES)



@dataclass
class FuturesQuote:
    """Futures price data"""
    symbol: str
    price: float
    change: float
    change_pct: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
