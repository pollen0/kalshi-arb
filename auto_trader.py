#!/usr/bin/env python3
"""
Kalshi Auto-Trading System

Live trading only - no paper mode.
Supports pre-game and in-game betting with smart exit logic.
"""

from flask import Flask, render_template_string, jsonify, request
import requests
import os
import json
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import re

app = Flask(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

DATA_DIR = Path(__file__).parent

# Trading parameters
CONFIG = {
    # Entry criteria
    "min_edge_entry": 0.05,       # 5% min edge to enter
    "min_sources": 2,              # At least 2 sources must agree
    "max_source_spread": 0.08,     # Max 8% disagreement between sources
    "min_volume": 500,             # Min $500 market volume

    # Position sizing
    "max_position_pct": 0.15,      # Max 15% of bankroll per position
    "kelly_fraction": 0.25,        # Use 1/4 Kelly

    # Exit criteria
    "take_profit_edge": 0.01,      # Exit if edge drops below 1%
    "stop_loss_pct": 0.40,         # Exit if down 40% on position
    "min_exit_edge": -0.03,        # Exit if consensus flips against us by 3%

    # Live game adjustments
    "live_edge_boost": 0.02,       # Require 2% more edge for live games
    "max_live_position_pct": 0.10, # Smaller positions during live games

    # Risk limits
    "max_positions": 8,            # Max concurrent positions
    "max_daily_loss": 0.20,        # Stop if down 20% of starting balance
    "scan_interval": 30,           # Seconds between scans
}

# =============================================================================
# KALSHI CLIENT
# =============================================================================

class KalshiClient:
    """Live Kalshi trading client"""

    def __init__(self):
        self.client = None
        self.connected = False
        self._connect()

    def _connect(self):
        try:
            from credentials import KALSHI_API_KEY, KALSHI_PRIVATE_KEY
            from kalshi_client import KalshiLiveClient
            self.client = KalshiLiveClient(KALSHI_API_KEY, KALSHI_PRIVATE_KEY)
            bal = self.client.get_balance()
            if not bal.get("error"):
                self.connected = True
                print(f"[KALSHI] Connected - Balance: ${bal.get('balance', 0)/100:.2f}")
        except Exception as e:
            print(f"[KALSHI] Connection failed: {e}")

    def get_balance(self) -> float:
        """Get balance in dollars"""
        if not self.connected:
            return 0
        bal = self.client.get_balance()
        return bal.get("balance", 0) / 100 if not bal.get("error") else 0

    def get_positions(self) -> list:
        """Get open positions"""
        if not self.connected:
            return []
        positions = self.client.get_positions()
        return [p for p in positions if p.get("position", 0) != 0]

    def place_order(self, ticker: str, side: str, count: int, price: int) -> dict:
        """Place a buy order. Price in cents."""
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return self.client.place_order(ticker, side, "buy", count, price)

    def sell_position(self, ticker: str, count: int, price: int) -> dict:
        """Sell/exit a position. Price in cents."""
        if not self.connected:
            return {"error": True, "message": "Not connected"}
        return self.client.place_order(ticker, "yes", "sell", count, price)

    def get_market(self, ticker: str) -> dict:
        """Get market details"""
        if not self.connected:
            return {}
        return self.client.get_market(ticker)


kalshi = KalshiClient()

# =============================================================================
# DATA FETCHING
# =============================================================================

def american_to_prob(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)

def fetch_kalshi_markets() -> list:
    """Fetch all sports markets from Kalshi"""
    markets = []
    for series in ["KXNFLGAME", "KXNBAGAME", "KXNCAAFGAME", "KXNCAABGAME"]:
        try:
            resp = requests.get(f"{KALSHI_BASE}/markets", params={
                "status": "open", "series_ticker": series, "limit": 200
            }, timeout=15)
            if resp.status_code == 200:
                for m in resp.json().get("markets", []):
                    if "Winner" not in m.get("title", ""):
                        continue
                    match = re.search(r"(.+?)\s+(?:vs|at)\s+(.+?)\s+Winner", m.get("title", ""))
                    if not match:
                        continue
                    team1, team2 = match.groups()
                    yes_team = m.get("yes_sub_title", "")
                    markets.append({
                        "ticker": m.get("ticker", ""),
                        "title": m.get("title", ""),
                        "team": yes_team,
                        "opponent": team2.strip() if yes_team.lower() in team1.lower() else team1.strip(),
                        "yes_bid": m.get("yes_bid", 0) or 0,
                        "yes_ask": m.get("yes_ask", 100) or 100,
                        "volume": m.get("volume", 0) or 0,
                        "close_time": m.get("close_time", ""),
                        "series": series,
                    })
        except Exception as e:
            print(f"Kalshi fetch error: {e}")
    return markets

def fetch_espn_data() -> dict:
    """Fetch ESPN games with predictions"""
    games = {}
    leagues = [
        ("football", "nfl"), ("basketball", "nba"),
        ("football", "college-football"), ("basketball", "mens-college-basketball")
    ]

    for sport, league in leagues:
        try:
            # Get scoreboard
            resp = requests.get(f"{ESPN_BASE}/{sport}/{league}/scoreboard", timeout=10)
            if resp.status_code != 200:
                continue

            for event in resp.json().get("events", []):
                game_id = event.get("id")
                comps = event.get("competitions", [])
                if not comps:
                    continue

                comp = comps[0]
                status = event.get("status", {}).get("type", {}).get("name", "")
                is_live = status == "STATUS_IN_PROGRESS"

                home = away = None
                home_score = away_score = 0
                for c in comp.get("competitors", []):
                    name = c.get("team", {}).get("displayName", "")
                    score = int(c.get("score", 0) or 0)
                    if c.get("homeAway") == "home":
                        home, home_score = name, score
                    else:
                        away, away_score = name, score

                games[game_id] = {
                    "id": game_id,
                    "home": home,
                    "away": away,
                    "home_score": home_score,
                    "away_score": away_score,
                    "status": status,
                    "is_live": is_live,
                    "sport": sport,
                    "league": league,
                    "game_time": event.get("date", ""),
                    "fpi_home": None,
                    "fpi_away": None,
                    "dk_home": None,
                    "dk_away": None,
                }

                # Get predictions for scheduled/live games
                if status in ["STATUS_SCHEDULED", "STATUS_IN_PROGRESS"]:
                    try:
                        pred_resp = requests.get(
                            f"https://site.web.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary",
                            params={"event": game_id}, timeout=10
                        )
                        if pred_resp.status_code == 200:
                            data = pred_resp.json()
                            pred = data.get("predictor", {})
                            if pred:
                                h = pred.get("homeTeam", {})
                                a = pred.get("awayTeam", {})
                                if h.get("gameProjection"):
                                    games[game_id]["fpi_home"] = float(h["gameProjection"]) / 100
                                if a.get("gameProjection"):
                                    games[game_id]["fpi_away"] = float(a["gameProjection"]) / 100

                            for pc in data.get("pickcenter", []):
                                if "draft" in pc.get("provider", {}).get("name", "").lower():
                                    hml = pc.get("homeTeamOdds", {}).get("moneyLine")
                                    aml = pc.get("awayTeamOdds", {}).get("moneyLine")
                                    if hml:
                                        games[game_id]["dk_home"] = american_to_prob(hml)
                                    if aml:
                                        games[game_id]["dk_away"] = american_to_prob(aml)
                                    break
                    except:
                        pass
        except Exception as e:
            print(f"ESPN fetch error: {e}")

    return games

# Odds API cache
_odds_cache = {}
_odds_cache_time = {}

def fetch_odds_api() -> dict:
    """Fetch odds from The Odds API with caching"""
    global _odds_cache, _odds_cache_time

    if not ODDS_API_KEY:
        return {}

    result = {}
    sports = {
        "americanfootball_nfl": "nfl",
        "basketball_nba": "nba",
        "americanfootball_ncaaf": "cfb",
        "basketball_ncaab": "ncaab",
    }

    for sport_key, label in sports.items():
        # Check cache (5 min)
        if sport_key in _odds_cache:
            age = (datetime.now() - _odds_cache_time.get(sport_key, datetime.min)).seconds
            if age < 300:
                result[label] = _odds_cache[sport_key]
                continue

        try:
            resp = requests.get(f"{ODDS_API_BASE}/sports/{sport_key}/odds", params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h",
                "oddsFormat": "american",
            }, timeout=15)

            if resp.status_code == 200:
                data = resp.json()
                _odds_cache[sport_key] = data
                _odds_cache_time[sport_key] = datetime.now()
                result[label] = data
        except:
            if sport_key in _odds_cache:
                result[label] = _odds_cache[sport_key]

    return result

def teams_match(n1: str, n2: str) -> bool:
    """Check if team names match"""
    if not n1 or not n2:
        return False
    n1, n2 = n1.lower().strip(), n2.lower().strip()
    if n1 == n2 or n1 in n2 or n2 in n1:
        return True
    w1 = set(n1.split()) - {"state", "university", "college", "the", "at"}
    w2 = set(n2.split()) - {"state", "university", "college", "the", "at"}
    return bool(w1 & w2)

# =============================================================================
# SIGNAL GENERATION
# =============================================================================

def calculate_consensus(sources: list) -> tuple:
    """
    Calculate weighted consensus from sources.
    Returns: (consensus, num_sources, max_spread)
    """
    # Weights: Pinnacle > DK/FD > BetMGM > FPI
    weights = {
        "pinnacle": 2.5,
        "draftkings": 1.5,
        "fanduel": 1.5,
        "betmgm": 1.2,
        "fpi": 1.0,
    }

    valid = [(s, p, weights.get(s, 1.0)) for s, p in sources if p is not None]
    if not valid:
        return None, 0, 0

    weighted_sum = sum(p * w for _, p, w in valid)
    total_weight = sum(w for _, _, w in valid)
    consensus = weighted_sum / total_weight

    probs = [p for _, p, _ in valid]
    spread = max(probs) - min(probs) if len(probs) > 1 else 0

    return consensus, len(valid), spread

def analyze_opportunity(kalshi_market: dict, espn_game: dict, odds_data: dict) -> dict:
    """
    Analyze a trading opportunity.
    Returns analysis with entry/exit recommendations.
    """
    ticker = kalshi_market["ticker"]
    team = kalshi_market["team"]
    opponent = kalshi_market["opponent"]

    kalshi_bid = kalshi_market["yes_bid"] / 100
    kalshi_ask = kalshi_market["yes_ask"] / 100
    kalshi_mid = (kalshi_bid + kalshi_ask) / 2
    volume = kalshi_market["volume"]

    is_live = espn_game.get("is_live", False)

    # Determine if this is home or away team
    is_home = teams_match(team, espn_game.get("home", ""))

    # Gather source probabilities
    sources = []

    if is_home:
        if espn_game.get("fpi_home"):
            sources.append(("fpi", espn_game["fpi_home"]))
        if espn_game.get("dk_home"):
            sources.append(("draftkings", espn_game["dk_home"]))
    else:
        if espn_game.get("fpi_away"):
            sources.append(("fpi", espn_game["fpi_away"]))
        if espn_game.get("dk_away"):
            sources.append(("draftkings", espn_game["dk_away"]))

    # Add Odds API data
    for label, games in odds_data.items():
        for og in games:
            if teams_match(og.get("home_team", ""), espn_game.get("home", "")) and \
               teams_match(og.get("away_team", ""), espn_game.get("away", "")):
                for bm in og.get("bookmakers", []):
                    bk = bm.get("key", "")
                    if bk not in ["pinnacle", "fanduel", "betmgm"]:
                        continue
                    for mkt in bm.get("markets", []):
                        if mkt.get("key") != "h2h":
                            continue
                        for out in mkt.get("outcomes", []):
                            out_team = out.get("name", "")
                            prob = american_to_prob(out.get("price", 0))
                            if teams_match(out_team, team):
                                sources.append((bk, prob))
                break

    consensus, num_sources, spread = calculate_consensus(sources)

    if consensus is None:
        return None

    # Calculate edge (buying at ask)
    edge = consensus - kalshi_ask

    # Adjust for live games
    min_edge = CONFIG["min_edge_entry"]
    if is_live:
        min_edge += CONFIG["live_edge_boost"]

    # Build analysis
    analysis = {
        "ticker": ticker,
        "team": team,
        "opponent": opponent,
        "game": f"{team} vs {opponent}",
        "is_live": is_live,
        "kalshi_bid": kalshi_bid,
        "kalshi_ask": kalshi_ask,
        "kalshi_mid": kalshi_mid,
        "volume": volume,
        "consensus": consensus,
        "edge": edge,
        "num_sources": num_sources,
        "source_spread": spread,
        "sources": sources,

        # Entry decision
        "should_enter": False,
        "entry_reason": "",

        # Exit decision (for existing positions)
        "should_exit": False,
        "exit_reason": "",
    }

    # Entry criteria
    if edge >= min_edge:
        if num_sources >= CONFIG["min_sources"]:
            if spread <= CONFIG["max_source_spread"]:
                if volume >= CONFIG["min_volume"]:
                    analysis["should_enter"] = True
                    analysis["entry_reason"] = f"Edge {edge*100:.1f}% > {min_edge*100:.0f}% threshold"
                else:
                    analysis["entry_reason"] = f"Low volume: ${volume}"
            else:
                analysis["entry_reason"] = f"Sources disagree: {spread*100:.1f}%"
        else:
            analysis["entry_reason"] = f"Only {num_sources} sources"
    else:
        analysis["entry_reason"] = f"Edge {edge*100:.1f}% < {min_edge*100:.0f}%"

    return analysis

def check_exit_conditions(position: dict, current_analysis: dict) -> dict:
    """
    Check if we should exit a position.
    Returns exit decision with reason.
    """
    if not current_analysis:
        return {"should_exit": False, "reason": "No current data"}

    # Position details
    contracts = position.get("position", 0)
    entry_cost = position.get("market_exposure", 0)  # in cents
    avg_price = entry_cost / contracts if contracts > 0 else 0

    current_bid = current_analysis["kalshi_bid"] * 100
    current_consensus = current_analysis["consensus"]
    current_edge = current_analysis["edge"]

    # Calculate P&L
    current_value = contracts * current_bid
    pnl_pct = (current_value - entry_cost) / entry_cost if entry_cost > 0 else 0

    result = {
        "should_exit": False,
        "reason": "Hold",
        "action": "hold",
        "pnl_pct": pnl_pct,
        "current_edge": current_edge,
    }

    # Check stop loss
    if pnl_pct <= -CONFIG["stop_loss_pct"]:
        result["should_exit"] = True
        result["reason"] = f"Stop loss: {pnl_pct*100:.1f}%"
        result["action"] = "stop_loss"
        return result

    # Check if edge has disappeared or flipped
    if current_edge < CONFIG["take_profit_edge"]:
        result["should_exit"] = True
        result["reason"] = f"Edge gone: {current_edge*100:.1f}%"
        result["action"] = "take_profit"
        return result

    # Check if consensus flipped against us significantly
    if current_edge < CONFIG["min_exit_edge"]:
        result["should_exit"] = True
        result["reason"] = f"Consensus flipped: {current_edge*100:.1f}%"
        result["action"] = "cut_loss"
        return result

    # Otherwise hold - still have edge
    result["reason"] = f"Hold - edge {current_edge*100:.1f}%"

    return result

# =============================================================================
# TRADING ENGINE
# =============================================================================

class TradingEngine:
    """Auto-trading engine"""

    def __init__(self):
        self.running = False
        self.thread = None
        self.last_scan = None
        self.opportunities = []
        self.actions = []  # Recent actions log
        self.starting_balance = None
        self.daily_pnl = 0

    def calculate_position_size(self, edge: float, consensus: float,
                                 balance: float, is_live: bool) -> int:
        """Calculate optimal position size using Kelly criterion"""
        if edge <= 0 or consensus <= 0 or consensus >= 1:
            return 0

        # Kelly formula for binary bets
        # f* = (p * b - q) / b where b = odds, p = win prob, q = 1-p
        # Simplified for binary: f* ≈ edge / (1 - consensus)
        kelly = edge / (1 - consensus)

        # Use fractional Kelly
        position_pct = kelly * CONFIG["kelly_fraction"]

        # Cap at max position
        max_pct = CONFIG["max_live_position_pct"] if is_live else CONFIG["max_position_pct"]
        position_pct = min(position_pct, max_pct)

        # Calculate dollar amount
        position_dollars = balance * position_pct

        # Convert to contracts (assuming we buy at consensus price roughly)
        price_cents = int(consensus * 100)
        contracts = int(position_dollars / (price_cents / 100))

        return max(1, contracts)  # At least 1 contract

    def execute_entry(self, analysis: dict) -> dict:
        """Execute an entry trade"""
        balance = kalshi.get_balance()
        if balance < 1:
            return {"success": False, "message": "Insufficient balance"}

        # Check position limits
        positions = kalshi.get_positions()
        if len(positions) >= CONFIG["max_positions"]:
            return {"success": False, "message": "Max positions reached"}

        # Check if already have position in this market
        for p in positions:
            if p.get("ticker") == analysis["ticker"]:
                return {"success": False, "message": "Already have position"}

        # Calculate size
        contracts = self.calculate_position_size(
            analysis["edge"],
            analysis["consensus"],
            balance,
            analysis["is_live"]
        )

        if contracts < 1:
            return {"success": False, "message": "Position too small"}

        # Place order at ask (or slightly above to ensure fill)
        price = int(analysis["kalshi_ask"] * 100) + 1
        price = min(price, 99)  # Cap at 99c

        result = kalshi.place_order(analysis["ticker"], "yes", contracts, price)

        if result.get("error"):
            return {"success": False, "message": result.get("message", "Order failed")}

        action = {
            "time": datetime.now().isoformat(),
            "type": "ENTRY",
            "ticker": analysis["ticker"],
            "team": analysis["team"],
            "contracts": contracts,
            "price": price,
            "edge": analysis["edge"],
            "consensus": analysis["consensus"],
        }
        self.actions.append(action)

        return {"success": True, "message": f"Bought {contracts}x @ {price}c", "action": action}

    def execute_exit(self, position: dict, reason: str) -> dict:
        """Execute an exit trade"""
        ticker = position.get("ticker")
        contracts = position.get("position", 0)

        if contracts <= 0:
            return {"success": False, "message": "No position to exit"}

        # Get current bid
        market = kalshi.get_market(ticker)
        if not market:
            return {"success": False, "message": "Could not get market data"}

        bid = market.get("market", {}).get("yes_bid", 1)

        # Sell at bid (or slightly below to ensure fill)
        price = max(1, bid - 1)

        result = kalshi.sell_position(ticker, contracts, price)

        if result.get("error"):
            return {"success": False, "message": result.get("message", "Order failed")}

        action = {
            "time": datetime.now().isoformat(),
            "type": "EXIT",
            "reason": reason,
            "ticker": ticker,
            "contracts": contracts,
            "price": price,
        }
        self.actions.append(action)

        return {"success": True, "message": f"Sold {contracts}x @ {price}c", "action": action}

    def scan_and_trade(self):
        """Main scan and trade cycle"""
        self.last_scan = datetime.now().isoformat()

        try:
            # Check daily loss limit
            balance = kalshi.get_balance()
            print(f"\n[SCAN] {datetime.now().strftime('%H:%M:%S')} | Balance: ${balance:.2f}", flush=True)

            if self.starting_balance is None:
                self.starting_balance = balance

            daily_return = (balance - self.starting_balance) / self.starting_balance if self.starting_balance > 0 else 0
            if daily_return < -CONFIG["max_daily_loss"]:
                print(f"[ENGINE] Daily loss limit hit: {daily_return*100:.1f}%")
                return

            # Fetch all data
            kalshi_markets = fetch_kalshi_markets()
            espn_data = fetch_espn_data()
            odds_data = fetch_odds_api()

            # Get current positions
            positions = kalshi.get_positions()
            position_tickers = {p.get("ticker"): p for p in positions}

            # Analyze all opportunities
            opportunities = []

            for km in kalshi_markets:
                # Find matching ESPN game
                matched_game = None
                for gid, game in espn_data.items():
                    if (teams_match(km["team"], game.get("home", "")) or
                        teams_match(km["team"], game.get("away", ""))) and \
                       (teams_match(km["opponent"], game.get("home", "")) or
                        teams_match(km["opponent"], game.get("away", ""))):
                        matched_game = game
                        break

                if not matched_game:
                    continue

                analysis = analyze_opportunity(km, matched_game, odds_data)
                if analysis:
                    opportunities.append(analysis)

            # Sort by edge
            opportunities.sort(key=lambda x: x["edge"], reverse=True)
            self.opportunities = opportunities[:20]

            # Check exits for existing positions
            for ticker, pos in position_tickers.items():
                # Find current analysis for this position
                current = next((o for o in opportunities if o["ticker"] == ticker), None)
                if current:
                    exit_decision = check_exit_conditions(pos, current)
                    if exit_decision["should_exit"]:
                        print(f"[ENGINE] Exiting {ticker}: {exit_decision['reason']}")
                        self.execute_exit(pos, exit_decision["reason"])

            # Look for new entries
            buy_signals = [o for o in opportunities if o["should_enter"]]
            print(f"[SCAN] Found {len(opportunities)} opportunities, {len(buy_signals)} with BUY signal", flush=True)

            if balance < 1:
                print(f"[SCAN] Balance too low (${balance:.2f}) - need to deposit funds!", flush=True)

            for opp in opportunities:
                if opp["should_enter"] and opp["ticker"] not in position_tickers:
                    print(f"[ENGINE] Trying {opp['team']}: edge={opp['edge']*100:.1f}%", flush=True)
                    result = self.execute_entry(opp)
                    if result["success"]:
                        print(f"[ENGINE] SUCCESS: {result['message']}", flush=True)
                        break  # One entry per cycle
                    else:
                        print(f"[ENGINE] SKIP: {result['message']}", flush=True)

        except Exception as e:
            print(f"[ENGINE] Error: {e}")
            import traceback
            traceback.print_exc()

    def run_loop(self):
        """Main trading loop"""
        while self.running:
            self.scan_and_trade()
            time.sleep(CONFIG["scan_interval"])

    def start(self):
        """Start the engine"""
        if self.running:
            return {"success": False, "message": "Already running"}
        if not kalshi.connected:
            return {"success": False, "message": "Kalshi not connected"}

        self.running = True
        self.starting_balance = kalshi.get_balance()
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()
        return {"success": True, "message": "Engine started"}

    def stop(self):
        """Stop the engine"""
        self.running = False
        return {"success": True, "message": "Engine stopped"}

    def get_status(self) -> dict:
        """Get engine status"""
        balance = kalshi.get_balance()
        positions = kalshi.get_positions()

        pnl = balance - self.starting_balance if self.starting_balance else 0

        # Load API usage
        api_usage = {"calls_this_month": 0, "monthly_limit": 500}
        try:
            usage_file = DATA_DIR / "api_usage.json"
            if usage_file.exists():
                api_usage = json.loads(usage_file.read_text())
        except:
            pass

        return {
            "running": self.running,
            "connected": kalshi.connected,
            "balance": balance,
            "starting_balance": self.starting_balance,
            "pnl": pnl,
            "pnl_pct": pnl / self.starting_balance * 100 if self.starting_balance else 0,
            "positions": positions,
            "position_count": len(positions),
            "last_scan": self.last_scan,
            "opportunities": self.opportunities[:10],
            "recent_actions": self.actions[-20:][::-1],
            "config": CONFIG,
            "api_calls_used": api_usage.get("calls_this_month", 0),
            "api_calls_limit": api_usage.get("monthly_limit", 500),
        }


engine = TradingEngine()

# =============================================================================
# DASHBOARD HTML
# =============================================================================

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <title>Kalshi Auto-Trader</title>
    <meta charset="utf-8">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0a0a0a; --card: #141414; --border: #222;
            --text: #fff; --muted: #666; --green: #00d26a; --red: #ff4757; --blue: #3b82f6; --yellow: #f59e0b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); padding: 20px; }

        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
        .title { font-size: 24px; font-weight: 700; }
        .status { display: flex; gap: 12px; align-items: center; }
        .badge { padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 600; }
        .badge-green { background: var(--green); color: #000; }
        .badge-red { background: var(--red); color: #fff; }
        .badge-yellow { background: var(--yellow); color: #000; }

        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background: var(--card); border-radius: 12px; padding: 20px; }
        .card-title { font-size: 11px; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; letter-spacing: 0.5px; }

        .big-number { font-size: 36px; font-weight: 700; }
        .sub-text { font-size: 13px; color: var(--muted); margin-top: 4px; }
        .positive { color: var(--green); }
        .negative { color: var(--red); }

        .stats-row { display: flex; gap: 24px; margin-top: 16px; }
        .stat { text-align: center; }
        .stat-value { font-size: 20px; font-weight: 600; }
        .stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; }

        .btn { padding: 12px 24px; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; }
        .btn-green { background: var(--green); color: #000; }
        .btn-red { background: var(--red); color: #fff; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }

        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; padding: 12px; font-size: 10px; text-transform: uppercase; color: var(--muted); border-bottom: 1px solid var(--border); }
        td { padding: 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
        tr:hover td { background: #1a1a1a; }

        .edge-badge { padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
        .edge-high { background: rgba(0,210,106,0.2); color: var(--green); }
        .edge-med { background: rgba(245,158,11,0.2); color: var(--yellow); }
        .edge-low { background: rgba(102,102,102,0.2); color: var(--muted); }

        .live-badge { background: var(--red); color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 10px; margin-left: 8px; }

        .action-log { max-height: 300px; overflow-y: auto; }
        .action-item { padding: 10px; border-bottom: 1px solid var(--border); font-size: 12px; }
        .action-time { color: var(--muted); }
        .action-entry { color: var(--green); }
        .action-exit { color: var(--red); }

        .config-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
        .config-item { display: flex; justify-content: space-between; padding: 6px 0; font-size: 12px; }
        .config-label { color: var(--muted); }
    </style>
</head>
<body>
    <div class="header">
        <h1 class="title">Kalshi Auto-Trader</h1>
        <div class="status">
            <span class="badge" id="connection-badge">OFFLINE</span>
            <span class="badge" id="engine-badge">STOPPED</span>
            <button class="btn btn-green" id="start-btn" onclick="startEngine()">Start Trading</button>
            <button class="btn btn-red" id="stop-btn" onclick="stopEngine()" disabled>Stop</button>
        </div>
    </div>

    <div class="grid" style="grid-template-columns: repeat(4, 1fr);">
        <div class="card">
            <div class="card-title">Account Balance</div>
            <div class="big-number" id="balance">$0.00</div>
            <div class="sub-text" id="pnl">P&L: $0.00</div>
        </div>
        <div class="card">
            <div class="card-title">Positions</div>
            <div class="big-number" id="position-count">0</div>
            <div class="sub-text">of 8 max</div>
        </div>
        <div class="card">
            <div class="card-title">Engine Status</div>
            <div class="big-number" id="status-text">Stopped</div>
            <div class="sub-text" id="last-scan">-</div>
        </div>
        <div class="card">
            <div class="card-title">Odds API</div>
            <div class="big-number" id="api-remaining">-</div>
            <div class="sub-text" id="api-usage">0 / 500 calls</div>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <div class="card-title">Live Positions</div>
            <div id="positions-table">
                <div style="color: var(--muted); text-align: center; padding: 20px;">No positions</div>
            </div>
        </div>
        <div class="card">
            <div class="card-title">Trading Config</div>
            <div class="config-grid" id="config-display"></div>
        </div>
    </div>

    <div class="grid">
        <div class="card" style="grid-column: span 2;">
            <div class="card-title">Top Opportunities</div>
            <table>
                <thead>
                    <tr><th>Game</th><th>Team</th><th>Kalshi</th><th>Consensus</th><th>Edge</th><th>Sources</th><th>Signal</th></tr>
                </thead>
                <tbody id="opportunities-table"></tbody>
            </table>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <div class="card-title">Recent Actions</div>
            <div class="action-log" id="actions-log">
                <div style="color: var(--muted); text-align: center; padding: 20px;">No actions yet</div>
            </div>
        </div>
    </div>

    <script>
        async function fetchStatus() {
            try {
                const resp = await fetch('/api/status');
                const data = await resp.json();

                // Connection
                document.getElementById('connection-badge').textContent = data.connected ? 'CONNECTED' : 'OFFLINE';
                document.getElementById('connection-badge').className = 'badge ' + (data.connected ? 'badge-green' : 'badge-red');

                // Engine
                document.getElementById('engine-badge').textContent = data.running ? 'RUNNING' : 'STOPPED';
                document.getElementById('engine-badge').className = 'badge ' + (data.running ? 'badge-green' : 'badge-yellow');
                document.getElementById('start-btn').disabled = data.running || !data.connected;
                document.getElementById('stop-btn').disabled = !data.running;

                // Balance
                document.getElementById('balance').textContent = '$' + (data.balance || 0).toFixed(2);
                const pnl = data.pnl || 0;
                document.getElementById('pnl').innerHTML = `P&L: <span class="${pnl >= 0 ? 'positive' : 'negative'}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)} (${data.pnl_pct?.toFixed(1) || 0}%)</span>`;

                // Positions
                document.getElementById('position-count').textContent = data.position_count || 0;
                document.getElementById('status-text').textContent = data.running ? 'Active' : 'Stopped';
                document.getElementById('last-scan').textContent = data.last_scan ? 'Last: ' + new Date(data.last_scan).toLocaleTimeString() : '-';

                // API Usage
                const apiUsed = data.api_calls_used || 0;
                const apiLimit = data.api_calls_limit || 500;
                const apiRemaining = apiLimit - apiUsed;
                document.getElementById('api-remaining').textContent = apiRemaining;
                document.getElementById('api-usage').textContent = `${apiUsed} / ${apiLimit} used`;

                // Positions table
                if (data.positions && data.positions.length > 0) {
                    let html = '<table><thead><tr><th>Market</th><th>Contracts</th><th>Value</th></tr></thead><tbody>';
                    for (const p of data.positions) {
                        const ticker = p.ticker?.split('-').slice(-1)[0] || p.ticker;
                        html += `<tr><td>${ticker}</td><td>${p.position}</td><td>$${(p.market_exposure/100).toFixed(2)}</td></tr>`;
                    }
                    html += '</tbody></table>';
                    document.getElementById('positions-table').innerHTML = html;
                } else {
                    document.getElementById('positions-table').innerHTML = '<div style="color: var(--muted); text-align: center; padding: 20px;">No positions</div>';
                }

                // Config
                if (data.config) {
                    let configHtml = '';
                    const labels = {
                        min_edge_entry: 'Min Edge', max_position_pct: 'Max Position',
                        stop_loss_pct: 'Stop Loss', take_profit_edge: 'Take Profit Edge',
                        max_positions: 'Max Positions', scan_interval: 'Scan Interval'
                    };
                    for (const [key, label] of Object.entries(labels)) {
                        let val = data.config[key];
                        if (typeof val === 'number' && val < 1) val = (val * 100).toFixed(0) + '%';
                        else if (key === 'scan_interval') val = val + 's';
                        configHtml += `<div class="config-item"><span class="config-label">${label}</span><span>${val}</span></div>`;
                    }
                    document.getElementById('config-display').innerHTML = configHtml;
                }

                // Opportunities
                if (data.opportunities && data.opportunities.length > 0) {
                    let html = '';
                    for (const o of data.opportunities) {
                        const edgePct = (o.edge * 100).toFixed(1);
                        let edgeClass = 'edge-low';
                        if (o.edge >= 0.06) edgeClass = 'edge-high';
                        else if (o.edge >= 0.04) edgeClass = 'edge-med';

                        const signal = o.should_enter ? '<span class="edge-badge edge-high">BUY</span>' : '<span style="color: var(--muted);">-</span>';
                        const live = o.is_live ? '<span class="live-badge">LIVE</span>' : '';

                        html += `<tr>
                            <td>${o.game}${live}</td>
                            <td>${o.team}</td>
                            <td>${(o.kalshi_ask * 100).toFixed(0)}c</td>
                            <td>${(o.consensus * 100).toFixed(1)}%</td>
                            <td><span class="edge-badge ${edgeClass}">+${edgePct}%</span></td>
                            <td>${o.num_sources}</td>
                            <td>${signal}</td>
                        </tr>`;
                    }
                    document.getElementById('opportunities-table').innerHTML = html;
                } else {
                    document.getElementById('opportunities-table').innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--muted);">No opportunities</td></tr>';
                }

                // Actions
                if (data.recent_actions && data.recent_actions.length > 0) {
                    let html = '';
                    for (const a of data.recent_actions) {
                        const time = new Date(a.time).toLocaleTimeString();
                        const cls = a.type === 'ENTRY' ? 'action-entry' : 'action-exit';
                        html += `<div class="action-item">
                            <span class="action-time">${time}</span>
                            <span class="${cls}"> ${a.type}</span>
                            ${a.team || a.ticker?.split('-').slice(-1)[0]} - ${a.contracts}x @ ${a.price}c
                            ${a.reason ? `(${a.reason})` : ''}
                        </div>`;
                    }
                    document.getElementById('actions-log').innerHTML = html;
                }

            } catch (e) {
                console.error(e);
            }
        }

        async function startEngine() {
            await fetch('/api/start', { method: 'POST' });
            fetchStatus();
        }

        async function stopEngine() {
            await fetch('/api/stop', { method: 'POST' });
            fetchStatus();
        }

        fetchStatus();
        setInterval(fetchStatus, 5000);
    </script>
</body>
</html>
'''

# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route('/api/status')
def api_status():
    return jsonify(engine.get_status())

@app.route('/api/start', methods=['POST'])
def api_start():
    return jsonify(engine.start())

@app.route('/api/stop', methods=['POST'])
def api_stop():
    return jsonify(engine.stop())

# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    print('''
    ╔═══════════════════════════════════════════════════════════╗
    ║           KALSHI AUTO-TRADING SYSTEM                      ║
    ╠═══════════════════════════════════════════════════════════╣
    ║  LIVE TRADING - REAL MONEY                                ║
    ║                                                           ║
    ║  Features:                                                ║
    ║  - Auto entry on 5%+ edge with source agreement           ║
    ║  - Pre-game and live betting                              ║
    ║  - Stop loss at -40%                                      ║
    ║  - Take profit when edge disappears                       ║
    ║  - Kelly criterion position sizing                        ║
    ╚═══════════════════════════════════════════════════════════╝
    ''')

    print(f"Kalshi: {'Connected' if kalshi.connected else 'NOT CONNECTED'}")
    if kalshi.connected:
        print(f"Balance: ${kalshi.get_balance():.2f}")
        print("\n[AUTO-START] Starting trading engine...")
        engine.start()
        print("[AUTO-START] Engine running - will scan for opportunities every 30s")
    else:
        print("\n[WARNING] Not connected to Kalshi - engine not started")

    print(f"\nDashboard: http://127.0.0.1:5050")
    print()

    app.run(host='0.0.0.0', port=5050, debug=False)
