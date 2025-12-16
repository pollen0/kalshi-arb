#!/usr/bin/env python3
"""
SPX/SPY Options Arbitrage vs Kalshi

Compares Kalshi SPX range markets against SPY 0DTE options
to find arbitrage opportunities.

Requirements:
- TWS or IB Gateway running with API enabled
- ib_insync installed
- Kalshi API access
"""

import asyncio
import math
from datetime import datetime, date
from typing import Optional
import json
from pathlib import Path

# IBKR
from ib_insync import IB, Stock, Option, util

# Local
from credentials import KALSHI_API_KEY, KALSHI_PRIVATE_KEY
from kalshi_client import KalshiLiveClient

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG = {
    "ibkr_host": "127.0.0.1",
    "ibkr_port": 7497,  # TWS paper=7497, live=7496. Gateway paper=4002, live=4001
    "ibkr_client_id": 1,

    # Arbitrage thresholds
    "min_edge": 0.03,           # 3% min edge to signal
    "min_liquidity": 100,       # Min option volume

    # SPY to SPX conversion (SPY ≈ SPX/10)
    "spy_to_spx_ratio": 10,
}


# =============================================================================
# IBKR CLIENT
# =============================================================================

class IBKRClient:
    """Interactive Brokers client for options data"""

    def __init__(self):
        self.ib = IB()
        self.connected = False

    def connect(self) -> bool:
        """Connect to TWS/Gateway"""
        try:
            self.ib.connect(
                CONFIG["ibkr_host"],
                CONFIG["ibkr_port"],
                clientId=CONFIG["ibkr_client_id"]
            )
            self.connected = True
            print(f"[IBKR] Connected to TWS/Gateway")
            return True
        except Exception as e:
            print(f"[IBKR] Connection failed: {e}")
            print("[IBKR] Make sure TWS/Gateway is running with API enabled")
            return False

    def disconnect(self):
        """Disconnect from IBKR"""
        if self.connected:
            self.ib.disconnect()
            self.connected = False

    def get_spy_price(self) -> Optional[float]:
        """Get current SPY price"""
        if not self.connected:
            return None

        spy = Stock('SPY', 'SMART', 'USD')
        self.ib.qualifyContracts(spy)

        ticker = self.ib.reqMktData(spy, '', False, False)
        self.ib.sleep(1)  # Wait for data

        price = ticker.marketPrice()
        self.ib.cancelMktData(spy)

        return price if not math.isnan(price) else None

    def get_spy_options_chain(self, expiration: str = None) -> list:
        """
        Get SPY options chain for given expiration.
        If no expiration, uses today (0DTE).

        Returns list of option data with bid/ask/greeks.
        """
        if not self.connected:
            return []

        spy = Stock('SPY', 'SMART', 'USD')
        self.ib.qualifyContracts(spy)

        # Get chain parameters
        chains = self.ib.reqSecDefOptParams(spy.symbol, '', spy.secType, spy.conId)

        if not chains:
            print("[IBKR] No option chains found")
            return []

        # Find SMART exchange chain
        chain = next((c for c in chains if c.exchange == 'SMART'), chains[0])

        # Use today's expiration if not specified (0DTE)
        if expiration is None:
            today = date.today().strftime('%Y%m%d')
            # Find closest expiration
            expirations = sorted(chain.expirations)
            expiration = next((e for e in expirations if e >= today), expirations[0])

        print(f"[IBKR] Fetching options for expiration: {expiration}")

        # Get current price to focus on ATM options
        spy_price = self.get_spy_price()
        if not spy_price:
            spy_price = 600  # fallback

        # Get strikes around current price (+/- 5%)
        min_strike = spy_price * 0.95
        max_strike = spy_price * 1.05

        strikes = [s for s in chain.strikes if min_strike <= s <= max_strike]

        options_data = []

        # Create option contracts
        contracts = []
        for strike in strikes:
            for right in ['C', 'P']:  # Calls and Puts
                opt = Option('SPY', expiration, strike, right, 'SMART')
                contracts.append(opt)

        # Qualify contracts
        self.ib.qualifyContracts(*contracts)

        # Request market data for all
        tickers = []
        for contract in contracts:
            ticker = self.ib.reqMktData(contract, '106', False, False)  # 106 = greeks
            tickers.append((contract, ticker))

        # Wait for data
        self.ib.sleep(2)

        # Collect data
        for contract, ticker in tickers:
            if ticker.bid and ticker.ask:
                options_data.append({
                    "symbol": "SPY",
                    "expiration": expiration,
                    "strike": contract.strike,
                    "right": contract.right,
                    "bid": ticker.bid,
                    "ask": ticker.ask,
                    "mid": (ticker.bid + ticker.ask) / 2,
                    "last": ticker.last if not math.isnan(ticker.last) else None,
                    "volume": ticker.volume if ticker.volume else 0,
                    "iv": ticker.modelGreeks.impliedVol if ticker.modelGreeks else None,
                    "delta": ticker.modelGreeks.delta if ticker.modelGreeks else None,
                    "gamma": ticker.modelGreeks.gamma if ticker.modelGreeks else None,
                    "theta": ticker.modelGreeks.theta if ticker.modelGreeks else None,
                })

            # Cancel data subscription
            self.ib.cancelMktData(contract)

        print(f"[IBKR] Got {len(options_data)} options")
        return options_data

    def calculate_range_probability(self, options: list, lower: float, upper: float) -> dict:
        """
        Calculate probability of SPY being in a price range at expiration.

        Uses vertical call spread pricing:
        P(S > K1) - P(S > K2) ≈ P(K1 < S < K2)

        The cost of a call spread / max payout ≈ probability of being in range
        """
        if not options:
            return {"probability": None, "method": "no_data"}

        # Find calls near our strikes
        calls = [o for o in options if o["right"] == "C"]

        # Find closest strikes to our range
        lower_calls = sorted(calls, key=lambda x: abs(x["strike"] - lower))
        upper_calls = sorted(calls, key=lambda x: abs(x["strike"] - upper))

        if not lower_calls or not upper_calls:
            return {"probability": None, "method": "no_strikes"}

        lower_call = lower_calls[0]
        upper_call = upper_calls[0]

        # Bull call spread: Buy lower strike call, sell upper strike call
        # Cost = premium paid - premium received
        # If SPY ends in range, payout = upper - lower (approximately)

        spread_cost = lower_call["ask"] - upper_call["bid"]  # Cost to enter
        max_payout = upper_call["strike"] - lower_call["strike"]

        if max_payout <= 0:
            return {"probability": None, "method": "invalid_spread"}

        # Implied probability
        prob = spread_cost / max_payout
        prob = max(0, min(1, prob))  # Clamp to [0, 1]

        # Also use delta as a cross-check
        # Delta of a call ≈ probability of expiring ITM
        delta_lower = abs(lower_call.get("delta", 0.5) or 0.5)
        delta_upper = abs(upper_call.get("delta", 0.5) or 0.5)
        delta_prob = delta_lower - delta_upper

        return {
            "probability": prob,
            "delta_probability": delta_prob,
            "method": "call_spread",
            "lower_strike": lower_call["strike"],
            "upper_strike": upper_call["strike"],
            "spread_cost": spread_cost,
            "max_payout": max_payout,
            "lower_call_ask": lower_call["ask"],
            "upper_call_bid": upper_call["bid"],
        }


# =============================================================================
# KALSHI SPX MARKETS
# =============================================================================

def fetch_kalshi_spx_markets() -> list:
    """Fetch Kalshi SPX range markets"""
    try:
        client = KalshiLiveClient(KALSHI_API_KEY, KALSHI_PRIVATE_KEY)

        # Get SPX markets
        events = client.get_events(series_ticker="INXD")  # SPX daily

        markets = []
        for event in events:
            event_markets = client.get_markets(event_ticker=event.get("event_ticker"))
            for m in event_markets:
                if m.get("status") == "active":
                    # Parse range from title
                    title = m.get("title", "")
                    subtitle = m.get("subtitle", "")

                    # Extract range bounds
                    # Titles like "6,800 to 6,824.99" or "6800 to 6824.999"
                    import re
                    range_match = re.search(r'([\d,]+(?:\.\d+)?)\s*to\s*([\d,]+(?:\.\d+)?)', title)

                    if range_match:
                        lower = float(range_match.group(1).replace(',', ''))
                        upper = float(range_match.group(2).replace(',', ''))

                        markets.append({
                            "ticker": m.get("ticker"),
                            "title": title,
                            "subtitle": subtitle,
                            "lower_spx": lower,
                            "upper_spx": upper,
                            "lower_spy": lower / CONFIG["spy_to_spx_ratio"],
                            "upper_spy": upper / CONFIG["spy_to_spx_ratio"],
                            "yes_bid": m.get("yes_bid", 0) / 100,
                            "yes_ask": m.get("yes_ask", 0) / 100,
                            "no_bid": m.get("no_bid", 0) / 100,
                            "no_ask": m.get("no_ask", 0) / 100,
                            "volume": m.get("volume", 0),
                            "close_time": m.get("close_time"),
                        })

        return markets

    except Exception as e:
        print(f"[KALSHI] Error fetching SPX markets: {e}")
        return []


# =============================================================================
# ARBITRAGE ANALYSIS
# =============================================================================

def analyze_arbitrage(kalshi_markets: list, spy_options: list, spy_price: float) -> list:
    """
    Compare Kalshi SPX markets against SPY options implied probabilities.
    Returns list of arbitrage opportunities.
    """
    ibkr = IBKRClient()
    opportunities = []

    for km in kalshi_markets:
        # Convert SPX range to SPY range
        lower_spy = km["lower_spy"]
        upper_spy = km["upper_spy"]

        # Calculate options-implied probability
        opt_prob = ibkr.calculate_range_probability(spy_options, lower_spy, upper_spy)

        if opt_prob["probability"] is None:
            continue

        kalshi_yes = km["yes_ask"]  # Cost to buy Yes
        kalshi_no = km["no_ask"]    # Cost to buy No
        options_prob = opt_prob["probability"]

        # Edge calculations
        # If options say 90% prob but Kalshi Yes costs 85% → buy Kalshi Yes
        # If options say 90% prob but Kalshi No costs 15% → buy Kalshi No (implied 85% yes)

        yes_edge = options_prob - kalshi_yes  # Positive = Kalshi Yes is cheap
        no_edge = (1 - options_prob) - kalshi_no  # Positive = Kalshi No is cheap

        best_edge = max(yes_edge, no_edge)
        best_side = "YES" if yes_edge > no_edge else "NO"

        opp = {
            "kalshi_ticker": km["ticker"],
            "range": f"{km['lower_spx']:.0f}-{km['upper_spx']:.0f}",
            "spy_range": f"{lower_spy:.1f}-{upper_spy:.1f}",
            "kalshi_yes": kalshi_yes,
            "kalshi_no": kalshi_no,
            "options_prob": options_prob,
            "delta_prob": opt_prob.get("delta_probability"),
            "yes_edge": yes_edge,
            "no_edge": no_edge,
            "best_edge": best_edge,
            "best_side": best_side,
            "signal": best_edge >= CONFIG["min_edge"],
            "opt_details": opt_prob,
        }

        opportunities.append(opp)

    # Sort by edge
    opportunities.sort(key=lambda x: x["best_edge"], reverse=True)

    return opportunities


# =============================================================================
# MAIN
# =============================================================================

def run_analysis():
    """Run full arbitrage analysis"""
    print("\n" + "="*60)
    print("SPX/SPY OPTIONS ARBITRAGE SCANNER")
    print("="*60 + "\n")

    # Connect to IBKR
    ibkr = IBKRClient()
    if not ibkr.connect():
        print("\nCannot proceed without IBKR connection.")
        print("Please ensure TWS or IB Gateway is running with API enabled.")
        return

    try:
        # Get SPY price
        spy_price = ibkr.get_spy_price()
        print(f"\nSPY Price: ${spy_price:.2f}" if spy_price else "\nCould not get SPY price")

        # Get SPY 0DTE options
        print("\nFetching SPY 0DTE options...")
        spy_options = ibkr.get_spy_options_chain()

        # Get Kalshi SPX markets
        print("\nFetching Kalshi SPX markets...")
        kalshi_markets = fetch_kalshi_spx_markets()
        print(f"Found {len(kalshi_markets)} Kalshi SPX markets")

        # Analyze arbitrage
        print("\nAnalyzing arbitrage opportunities...")
        opportunities = analyze_arbitrage(kalshi_markets, spy_options, spy_price)

        # Display results
        print("\n" + "-"*60)
        print("ARBITRAGE OPPORTUNITIES")
        print("-"*60)

        for opp in opportunities[:10]:
            signal = ">>> BUY" if opp["signal"] else ""
            print(f"\nRange: {opp['range']} (SPY: {opp['spy_range']})")
            print(f"  Kalshi Yes: {opp['kalshi_yes']*100:.0f}c | No: {opp['kalshi_no']*100:.0f}c")
            print(f"  Options Prob: {opp['options_prob']*100:.1f}%")
            print(f"  Edge: {opp['best_side']} +{opp['best_edge']*100:.1f}% {signal}")

        # Summary
        signals = [o for o in opportunities if o["signal"]]
        print(f"\n{'='*60}")
        print(f"Found {len(signals)} opportunities with {CONFIG['min_edge']*100:.0f}%+ edge")

        return opportunities

    finally:
        ibkr.disconnect()


if __name__ == "__main__":
    run_analysis()
