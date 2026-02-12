"""
Trading Strategy Module

Smart limit order placement that:
1. Places +EV orders with realistic execution probability
2. Can make markets on both sides when profitable
3. Spreads orders across multiple markets
4. Never suggests impossible prices (like NO @ 100c)
"""

from dataclasses import dataclass, field
from typing import Optional
from ..core.models import Market, Orderbook, Side


@dataclass
class LimitOrder:
    """A limit order to place"""
    ticker: str
    side: str           # "yes" or "no"
    action: str         # "buy"
    price: int          # cents (1-99)
    size: int           # contracts
    edge: float         # expected edge if filled
    fill_prob: float    # estimated fill probability
    ev: float           # expected value = edge * size * fill_prob
    reason: str         # human readable


@dataclass
class MarketMakingOpportunity:
    """Opportunity to make a market (bid + ask)"""
    ticker: str
    yes_bid: Optional[LimitOrder] = None   # Buy YES at this price
    yes_ask: Optional[LimitOrder] = None   # Sell YES at this price
    spread_capture: float = 0              # Expected spread capture


@dataclass
class TradingPlan:
    """Complete trading plan across all markets"""
    orders: list[LimitOrder] = field(default_factory=list)
    total_ev: float = 0
    total_capital_at_risk: float = 0


class TradingStrategy:
    """
    Generates optimal limit orders based on fair value and orderbook.

    Key principles:
    1. Place orders at prices with BOTH edge AND execution probability
    2. Don't place orders at extreme prices (1c or 99c) that won't fill
    3. Improve best bid/ask when we have edge to increase fill probability
    4. Spread orders across many markets (diversification)
    """

    def __init__(self,
                 min_edge: float = 0.02,          # 2% minimum edge
                 max_position_per_market: int = 50,
                 max_total_capital: float = 500,   # Max $ at risk
                 min_fill_prob: float = 0.10):     # 10% min fill probability
        self.min_edge = min_edge
        self.max_position_per_market = max_position_per_market
        self.max_total_capital = max_total_capital
        self.min_fill_prob = min_fill_prob

    def generate_orders(self, markets: list[Market]) -> TradingPlan:
        """Generate limit orders for all markets with edge"""
        plan = TradingPlan()

        for market in markets:
            if market.fair_value is None:
                continue

            # Generate orders for this market
            orders = self._generate_market_orders(market)
            plan.orders.extend(orders)

        # Sort by EV and limit total capital
        plan.orders.sort(key=lambda o: o.ev, reverse=True)

        # Calculate totals
        capital_used = 0
        filtered_orders = []
        for order in plan.orders:
            order_capital = order.price * order.size / 100  # Convert cents to dollars
            if capital_used + order_capital <= self.max_total_capital:
                filtered_orders.append(order)
                capital_used += order_capital
                plan.total_ev += order.ev

        plan.orders = filtered_orders
        plan.total_capital_at_risk = capital_used

        return plan

    def _generate_market_orders(self, market: Market) -> list[LimitOrder]:
        """Generate all profitable orders for a single market"""
        orders = []

        fair_yes = market.fair_value
        fair_no = 1 - fair_yes

        # Current market prices
        yes_bid = market.yes_bid or 0
        yes_ask = market.yes_ask or 100
        no_bid = market.no_bid or 0
        no_ask = market.no_ask or 100

        # === YES SIDE ===
        # Buy YES: profitable if we pay less than fair value
        yes_buy_orders = self._generate_buy_orders(
            ticker=market.ticker,
            side="yes",
            fair_value=fair_yes,
            current_bid=yes_bid,
            current_ask=yes_ask,
            market_title=market.title
        )
        orders.extend(yes_buy_orders)

        # === NO SIDE ===
        # Buy NO: profitable if we pay less than fair NO value
        # NO bid/ask are separate from YES
        no_buy_orders = self._generate_buy_orders(
            ticker=market.ticker,
            side="no",
            fair_value=fair_no,
            current_bid=no_bid,
            current_ask=no_ask,
            market_title=market.title
        )
        orders.extend(no_buy_orders)

        return orders

    def _generate_buy_orders(self, ticker: str, side: str, fair_value: float,
                             current_bid: int, current_ask: int,
                             market_title: str) -> list[LimitOrder]:
        """Generate buy orders for one side"""
        orders = []
        fair_cents = int(fair_value * 100)

        # Strategy 1: Join/improve the bid (passive, higher fill prob)
        # Place bid slightly above current best bid but below fair value

        if current_bid > 0 and current_ask < 100:
            # There's a market - try to improve the bid
            spread = current_ask - current_bid

            # Place order at various price levels
            for price_offset in range(0, min(spread, 10)):
                bid_price = current_bid + price_offset + 1

                # Don't bid at or above the ask (that would cross)
                if bid_price >= current_ask:
                    break

                # Don't bid above fair value (negative edge)
                if bid_price > fair_cents:
                    break

                # Don't place extreme orders
                if bid_price < 2 or bid_price > 98:
                    continue

                edge = fair_value - (bid_price / 100)
                if edge < self.min_edge:
                    continue

                # Estimate fill probability
                # Improving bid = higher fill prob
                if bid_price > current_bid:
                    fill_prob = min(0.5, 0.2 + (price_offset * 0.1))
                else:
                    fill_prob = 0.1

                if fill_prob < self.min_fill_prob:
                    continue

                size = min(self.max_position_per_market, 20)
                ev = edge * size * fill_prob

                orders.append(LimitOrder(
                    ticker=ticker,
                    side=side,
                    action="buy",
                    price=bid_price,
                    size=size,
                    edge=edge,
                    fill_prob=fill_prob,
                    ev=ev,
                    reason=f"Buy {side.upper()} @ {bid_price}c (fair: {fair_cents}c, edge: {edge*100:.1f}%)"
                ))

        elif current_ask < 100 and current_ask <= fair_cents - 2:
            # No bid but there's an ask below our fair value
            # We can cross the spread (aggressive, guaranteed fill)
            edge = fair_value - (current_ask / 100)
            if edge >= self.min_edge:
                size = min(self.max_position_per_market, 10)
                orders.append(LimitOrder(
                    ticker=ticker,
                    side=side,
                    action="buy",
                    price=current_ask,
                    size=size,
                    edge=edge,
                    fill_prob=0.95,
                    ev=edge * size * 0.95,
                    reason=f"Cross spread: Buy {side.upper()} @ {current_ask}c (fair: {fair_cents}c)"
                ))

        elif current_bid == 0 and fair_cents > 5:
            # No bids at all - place a lowball bid
            # Start the market at a price with edge
            bid_price = max(2, min(fair_cents - 3, 50))  # At least 3c edge
            edge = fair_value - (bid_price / 100)

            if edge >= self.min_edge:
                size = min(self.max_position_per_market, 10)
                orders.append(LimitOrder(
                    ticker=ticker,
                    side=side,
                    action="buy",
                    price=bid_price,
                    size=size,
                    edge=edge,
                    fill_prob=0.15,  # Low fill prob for lowball
                    ev=edge * size * 0.15,
                    reason=f"Start market: Buy {side.upper()} @ {bid_price}c (fair: {fair_cents}c)"
                ))

        return orders

    def get_market_making_opportunity(self, market: Market) -> Optional[MarketMakingOpportunity]:
        """
        Check if we can make a market (bid + offer) with positive edge on both sides.
        This captures spread when both sides fill.
        """
        if market.fair_value is None:
            return None

        fair_yes = int(market.fair_value * 100)
        yes_bid = market.yes_bid or 0
        yes_ask = market.yes_ask or 100

        # Our bid should be below fair value
        # Our ask should be above fair value
        our_bid = max(2, fair_yes - 2)  # 2c below fair
        our_ask = min(98, fair_yes + 2)  # 2c above fair

        # Check if we can improve the market
        can_bid = our_bid > yes_bid and our_bid < fair_yes
        can_ask = our_ask < yes_ask and our_ask > fair_yes

        if not (can_bid or can_ask):
            return None

        opp = MarketMakingOpportunity(ticker=market.ticker)

        if can_bid:
            edge = market.fair_value - (our_bid / 100)
            opp.yes_bid = LimitOrder(
                ticker=market.ticker,
                side="yes",
                action="buy",
                price=our_bid,
                size=10,
                edge=edge,
                fill_prob=0.3,
                ev=edge * 10 * 0.3,
                reason=f"MM bid: {our_bid}c"
            )

        if can_ask:
            edge = (our_ask / 100) - market.fair_value
            opp.yes_ask = LimitOrder(
                ticker=market.ticker,
                side="yes",
                action="sell",
                price=our_ask,
                size=10,
                edge=edge,
                fill_prob=0.3,
                ev=edge * 10 * 0.3,
                reason=f"MM ask: {our_ask}c"
            )

        if opp.yes_bid and opp.yes_ask:
            opp.spread_capture = (our_ask - our_bid) / 100

        return opp


def generate_trading_plan(markets: list[Market],
                         min_edge: float = 0.02,
                         max_capital: float = 500) -> TradingPlan:
    """Convenience function to generate a trading plan"""
    strategy = TradingStrategy(min_edge=min_edge, max_total_capital=max_capital)
    return strategy.generate_orders(markets)


def format_trading_plan(plan: TradingPlan) -> str:
    """Format trading plan for display"""
    if not plan.orders:
        return "No profitable orders found"

    lines = [
        f"=== Trading Plan ===",
        f"Total orders: {len(plan.orders)}",
        f"Capital at risk: ${plan.total_capital_at_risk:.2f}",
        f"Expected value: ${plan.total_ev:.2f}",
        f"",
        "Orders (sorted by EV):",
    ]

    for i, order in enumerate(plan.orders[:20], 1):
        lines.append(
            f"  {i}. {order.side.upper()} {order.size}x @ {order.price}c "
            f"| Edge: {order.edge*100:.1f}% | Fill: {order.fill_prob*100:.0f}% "
            f"| EV: ${order.ev:.2f}"
        )

    if len(plan.orders) > 20:
        lines.append(f"  ... and {len(plan.orders) - 20} more orders")

    return "\n".join(lines)
