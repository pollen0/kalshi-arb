"""
Orderbook analyzer for optimal limit order placement
"""

from dataclasses import dataclass
from typing import Optional
from ..core.models import Orderbook, Market, Side


@dataclass
class OrderRecommendation:
    """Recommended order based on orderbook analysis"""
    side: Side
    price: int              # Limit price in cents
    size: int               # Recommended contracts
    edge_at_price: float    # Expected edge if filled
    fill_probability: float # Estimated chance of fill
    expected_value: float   # edge * size * fill_prob
    reasoning: str          # Human readable explanation


class OrderbookAnalyzer:
    """
    Analyzes orderbook to determine optimal limit order placement.

    Strategy:
    - If fair value is significantly different from market, we have edge
    - Place limit orders that maximize expected value (edge * fill probability)
    - Consider queue depth and spread width
    """

    def __init__(self,
                 min_edge: float = 0.03,      # 3% minimum edge to trade
                 max_position: int = 100,      # Max contracts per market
                 aggression: float = 0.5):     # 0=passive, 1=aggressive
        self.min_edge = min_edge
        self.max_position = max_position
        self.aggression = aggression

    def analyze(self, market: Market, orderbook: Orderbook) -> Optional[OrderRecommendation]:
        """
        Analyze orderbook and return best order recommendation.

        Returns None if no good opportunity exists.
        """
        if market.fair_value is None:
            return None

        if orderbook.is_empty:
            return None

        # Check both YES and NO sides
        yes_rec = self._analyze_yes_side(market, orderbook)
        no_rec = self._analyze_no_side(market, orderbook)

        # Return better opportunity
        if yes_rec and no_rec:
            return yes_rec if yes_rec.expected_value > no_rec.expected_value else no_rec
        return yes_rec or no_rec

    def _analyze_yes_side(self, market: Market, orderbook: Orderbook) -> Optional[OrderRecommendation]:
        """Analyze buying YES"""
        fair_value_cents = int(market.fair_value * 100)

        # Best ask is what we'd pay to buy YES immediately
        best_ask = orderbook.best_ask
        if not best_ask:
            return None

        # Edge if we cross the spread (aggressive)
        aggressive_edge = market.fair_value - (best_ask / 100)

        # For passive orders, we want to place below best ask
        # Optimal price balances fill probability vs edge

        # Calculate edge at various price levels
        best_rec = None

        for price_offset in range(0, 10):  # Try prices from best_ask down to best_ask-9
            limit_price = best_ask - price_offset
            if limit_price < 1:
                break

            edge = market.fair_value - (limit_price / 100)

            if edge < self.min_edge:
                continue

            # Estimate fill probability based on:
            # - Distance from best ask (further = less likely)
            # - Queue depth at that level
            fill_prob = self._estimate_fill_probability(
                limit_price, orderbook.yes_bids, orderbook.yes_asks, is_buy=True
            )

            # Size based on liquidity and max position
            size = self._calculate_size(limit_price, orderbook.yes_asks, is_buy=True)

            ev = edge * size * fill_prob

            if best_rec is None or ev > best_rec.expected_value:
                reasoning = self._build_reasoning(
                    "YES", limit_price, edge, fill_prob, size,
                    best_ask, orderbook.best_bid, fair_value_cents
                )
                best_rec = OrderRecommendation(
                    side=Side.YES,
                    price=limit_price,
                    size=size,
                    edge_at_price=edge,
                    fill_probability=fill_prob,
                    expected_value=ev,
                    reasoning=reasoning
                )

        return best_rec

    def _analyze_no_side(self, market: Market, orderbook: Orderbook) -> Optional[OrderRecommendation]:
        """Analyze buying NO (selling YES)"""
        fair_value_cents = int(market.fair_value * 100)
        no_fair_value = 1 - market.fair_value

        # To buy NO, we need to look at the YES bid (which is the NO ask)
        best_bid = orderbook.best_bid
        if not best_bid:
            return None

        # NO price = 100 - YES price
        no_ask = 100 - best_bid  # Price to buy NO by selling YES at bid

        best_rec = None

        for price_offset in range(0, 10):
            # For NO, we want to sell YES at higher prices (better for us)
            yes_sell_price = best_bid + price_offset
            if yes_sell_price > 99:
                break

            no_price = 100 - yes_sell_price
            edge = no_fair_value - (no_price / 100)

            if edge < self.min_edge:
                continue

            fill_prob = self._estimate_fill_probability(
                yes_sell_price, orderbook.yes_bids, orderbook.yes_asks, is_buy=False
            )

            size = self._calculate_size(yes_sell_price, orderbook.yes_bids, is_buy=False)

            ev = edge * size * fill_prob

            if best_rec is None or ev > best_rec.expected_value:
                reasoning = self._build_reasoning(
                    "NO", no_price, edge, fill_prob, size,
                    no_ask, 100 - orderbook.best_ask if orderbook.best_ask else None,
                    100 - fair_value_cents
                )
                best_rec = OrderRecommendation(
                    side=Side.NO,
                    price=no_price,
                    size=size,
                    edge_at_price=edge,
                    fill_probability=fill_prob,
                    expected_value=ev,
                    reasoning=reasoning
                )

        return best_rec

    def _estimate_fill_probability(self, price: int, bids: list, asks: list,
                                   is_buy: bool) -> float:
        """
        Estimate probability of getting filled at a limit price.

        Factors:
        - Crossing spread = 100% fill (but worst price)
        - At best bid/ask = high fill prob
        - Below best bid = lower fill prob based on depth
        """
        if is_buy:
            # Buying: if our price >= best_ask, immediate fill
            if asks and price >= asks[0].price:
                return 0.95  # Near certain

            # Joining the bid side
            if bids:
                best_bid = bids[0].price
                if price == best_bid:
                    # Queue depth matters
                    queue_depth = sum(b.quantity for b in bids if b.price == best_bid)
                    # Assume ~50% fill if reasonable queue
                    return max(0.3, 0.7 - (queue_depth / 200))
                elif price < best_bid:
                    # Below best bid, need price to come to us
                    distance = best_bid - price
                    return max(0.1, 0.5 - (distance * 0.05))
                else:
                    # Improving best bid
                    return 0.7
            return 0.3
        else:
            # Selling: if our price <= best_bid, immediate fill
            if bids and price <= bids[0].price:
                return 0.95

            if asks:
                best_ask = asks[0].price
                if price == best_ask:
                    queue_depth = sum(a.quantity for a in asks if a.price == best_ask)
                    return max(0.3, 0.7 - (queue_depth / 200))
                elif price > best_ask:
                    distance = price - best_ask
                    return max(0.1, 0.5 - (distance * 0.05))
                else:
                    return 0.7
            return 0.3

    def _calculate_size(self, price: int, levels: list, is_buy: bool) -> int:
        """Calculate recommended size based on liquidity"""
        if not levels:
            return min(10, self.max_position)

        # Sum available liquidity at or better than our price
        available = 0
        for level in levels:
            if is_buy and level.price <= price:
                available += level.quantity
            elif not is_buy and level.price >= price:
                available += level.quantity

        # Take a portion of available liquidity
        size = min(
            self.max_position,
            max(5, int(available * 0.3)),  # Don't take more than 30% of liquidity
            50  # Cap per order
        )

        return size

    def _build_reasoning(self, side: str, price: int, edge: float,
                        fill_prob: float, size: int, best_ask: int,
                        best_bid: Optional[int], fair_value_cents: int) -> str:
        """Build human-readable explanation"""
        parts = [f"Buy {size} {side} @ {price}c"]
        parts.append(f"Fair value: {fair_value_cents}c")
        parts.append(f"Edge: {edge*100:.1f}%")
        parts.append(f"Fill prob: {fill_prob*100:.0f}%")

        if best_bid and best_ask:
            parts.append(f"Spread: {best_bid}c/{best_ask}c")

        return " | ".join(parts)


def get_order_recommendation(market: Market, orderbook: Orderbook,
                            min_edge: float = 0.03) -> Optional[OrderRecommendation]:
    """Convenience function to get order recommendation"""
    analyzer = OrderbookAnalyzer(min_edge=min_edge)
    return analyzer.analyze(market, orderbook)
