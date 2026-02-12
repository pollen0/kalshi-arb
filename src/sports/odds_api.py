"""
The Odds API Client for Sports Betting Data

Fetches odds from multiple bookmakers to compare with Kalshi sports markets.
Supports: moneylines, spreads, totals, and player props.

API Documentation: https://the-odds-api.com/liveapi/guides/v4/
"""

import time
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict
from enum import Enum


class Sport(Enum):
    """Supported sports mapped to Odds API sport keys"""
    # American Football
    NFL = "americanfootball_nfl"
    NCAAF = "americanfootball_ncaaf"

    # Basketball
    NBA = "basketball_nba"
    NCAAB = "basketball_ncaab"
    WNBA = "basketball_wnba"
    EUROLEAGUE = "basketball_euroleague"

    # Hockey
    NHL = "icehockey_nhl"

    # Baseball
    MLB = "baseball_mlb"

    # Soccer
    EPL = "soccer_epl"
    MLS = "soccer_usa_mls"
    BUNDESLIGA = "soccer_germany_bundesliga"
    LALIGA = "soccer_spain_la_liga"
    SERIEA = "soccer_italy_serie_a"
    LIGUE1 = "soccer_france_ligue_one"
    UCL = "soccer_uefa_champs_league"
    UEL = "soccer_uefa_europa_league"

    # Tennis (use current active tournament — must update seasonally or use get_sports())
    ATP = "tennis_atp_us_open"
    WTA = "tennis_wta_us_open"

    # Golf (use current active tournament — must update seasonally or use get_sports())
    PGA = "golf_pga_championship"

    # MMA / Boxing
    UFC = "mma_mixed_martial_arts"
    BOXING = "boxing_boxing"

    # Motorsport
    F1 = "motorsport_formula_one"
    NASCAR = "motorsport_nascar_cup"


class Market(Enum):
    """Betting market types"""
    # Main markets
    MONEYLINE = "h2h"
    SPREAD = "spreads"
    TOTAL = "totals"

    # Player props - NFL
    PLAYER_PASS_TDS = "player_pass_tds"
    PLAYER_PASS_YDS = "player_pass_yds"
    PLAYER_RUSH_YDS = "player_rush_yds"
    PLAYER_RECEPTIONS = "player_receptions"
    PLAYER_ANYTIME_TD = "player_anytime_td"

    # Player props - NBA
    PLAYER_POINTS = "player_points"
    PLAYER_REBOUNDS = "player_rebounds"
    PLAYER_ASSISTS = "player_assists"
    PLAYER_THREES = "player_threes"

    # Player props - MLB
    BATTER_HOME_RUNS = "batter_home_runs"
    BATTER_HITS = "batter_hits"
    BATTER_RBIS = "batter_rbis"

    # Player props - NHL
    PLAYER_GOALS = "player_goals"
    PLAYER_SHOTS = "player_shots_on_goal"


@dataclass
class Outcome:
    """Single betting outcome"""
    name: str  # Team name or player name
    price: float  # American odds (e.g., -110, +150)
    point: Optional[float] = None  # Spread/total line
    description: Optional[str] = None  # Additional info


@dataclass
class MarketData:
    """Market data from a bookmaker"""
    key: str  # Market type (h2h, spreads, etc.)
    outcomes: List[Outcome]
    last_update: Optional[datetime] = None


@dataclass
class BookmakerOdds:
    """Odds from a single bookmaker"""
    key: str  # Bookmaker ID (draftkings, fanduel, etc.)
    title: str  # Display name
    markets: List[MarketData]
    last_update: Optional[datetime] = None


@dataclass
class Event:
    """Sports event with odds"""
    id: str
    sport_key: str
    sport_title: str
    commence_time: datetime
    home_team: str
    away_team: str
    bookmakers: List[BookmakerOdds] = field(default_factory=list)


@dataclass
class ConsensusOdds:
    """Aggregated consensus odds across bookmakers"""
    market: str
    outcome_name: str
    point: Optional[float]
    avg_price: float
    best_price: float
    best_book: str
    worst_price: float
    num_books: int


class OddsAPIClient:
    """
    Client for The Odds API

    API Docs: https://the-odds-api.com/liveapi/guides/v4/
    """

    BASE_URL = "https://api.the-odds-api.com/v4"

    # Bookmakers to query (US-focused)
    DEFAULT_BOOKMAKERS = [
        "draftkings",
        "fanduel",
        "betmgm",
        "caesars",
        "pointsbetus",
        "bovada",
        "betonlineag",
    ]

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self._requests_remaining = None
        self._requests_used = None

    @property
    def requests_remaining(self) -> Optional[int]:
        """Get remaining API quota"""
        return self._requests_remaining

    MAX_RETRIES = 3
    BACKOFF_BASE = 1  # seconds: retry delays will be 1s, 2s, 4s

    def _request(self, endpoint: str, params: dict = None) -> dict:
        """Make API request with retry + exponential backoff for transient errors.

        Retries on:
          - HTTP 5xx (server errors)
          - Connection errors (requests.ConnectionError)
          - Timeouts (requests.Timeout)

        Does NOT retry on 4xx (client errors like 401, 404, 422).
        """
        params = params or {}
        params["apiKey"] = self.api_key
        url = f"{self.BASE_URL}{endpoint}"

        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = self.session.get(url, params=params, timeout=30)

                # Track quota usage (headers are strings — convert to int)
                remaining = resp.headers.get("x-requests-remaining")
                used = resp.headers.get("x-requests-used")
                self._requests_remaining = int(remaining) if remaining else None
                self._requests_used = int(used) if used else None

                # 4xx — client error, don't retry
                if 400 <= resp.status_code < 500:
                    return {"error": True, "status": resp.status_code, "message": resp.text}

                # 5xx — server error, retry with backoff
                if resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    if attempt < self.MAX_RETRIES - 1:
                        delay = self.BACKOFF_BASE * (2 ** attempt)  # 1s, 2s, 4s
                        print(f"[ODDS API] Server error {resp.status_code}, retrying in {delay}s (attempt {attempt + 1}/{self.MAX_RETRIES})")
                        time.sleep(delay)
                        continue
                    # Final attempt failed
                    return {"error": True, "status": resp.status_code, "message": resp.text}

                # Success
                return resp.json()

            except (requests.ConnectionError, requests.Timeout) as e:
                last_error = str(e)
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.BACKOFF_BASE * (2 ** attempt)  # 1s, 2s, 4s
                    print(f"[ODDS API] {type(e).__name__}, retrying in {delay}s (attempt {attempt + 1}/{self.MAX_RETRIES}): {e}")
                    time.sleep(delay)
                    continue
                # Final attempt failed
                return {"error": True, "status": 0, "message": f"Network error after {self.MAX_RETRIES} retries: {last_error}"}

        # Should not reach here, but just in case
        return {"error": True, "status": 0, "message": f"Failed after {self.MAX_RETRIES} retries: {last_error}"}

    def get_sports(self) -> List[dict]:
        """
        Get list of in-season sports (FREE - no quota cost)

        Returns:
            List of sport objects with keys: key, group, title, description, active
        """
        result = self._request("/sports")
        if isinstance(result, dict) and result.get("error"):
            return []
        return result

    def get_events(self, sport: Sport) -> List[Event]:
        """
        Get upcoming events for a sport (FREE - no quota cost)

        Args:
            sport: Sport enum value

        Returns:
            List of Event objects (without odds)
        """
        result = self._request(f"/sports/{sport.value}/events")
        if isinstance(result, dict) and result.get("error"):
            return []

        events = []
        for e in result:
            events.append(Event(
                id=e["id"],
                sport_key=e["sport_key"],
                sport_title=e["sport_title"],
                commence_time=datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")),
                home_team=e["home_team"],
                away_team=e["away_team"],
            ))
        return events

    def get_odds(
        self,
        sport: Sport,
        markets: List[Market] = None,
        regions: str = "us",
        bookmakers: List[str] = None,
        odds_format: str = "american",
    ) -> List[Event]:
        """
        Get odds for upcoming events

        Quota cost: 1 × number_of_markets × number_of_regions

        Args:
            sport: Sport to query
            markets: List of market types (default: moneyline only)
            regions: Bookmaker regions (us, uk, au, eu)
            bookmakers: Specific bookmakers to query
            odds_format: 'american' or 'decimal'

        Returns:
            List of Events with bookmaker odds
        """
        markets = markets or [Market.MONEYLINE]
        market_keys = ",".join(m.value for m in markets)

        params = {
            "regions": regions,
            "markets": market_keys,
            "oddsFormat": odds_format,
        }

        if bookmakers:
            params["bookmakers"] = ",".join(bookmakers)

        result = self._request(f"/sports/{sport.value}/odds", params)
        if isinstance(result, dict) and result.get("error"):
            print(f"[ODDS API] Error: {result}")
            return []

        return self._parse_events(result)

    def get_event_odds(
        self,
        sport: Sport,
        event_id: str,
        markets: List[Market] = None,
        regions: str = "us",
        odds_format: str = "american",
    ) -> Optional[Event]:
        """
        Get odds for a single event (supports all markets including props)

        Use this for player props as they require per-event queries.

        Quota cost: 1 × unique_markets_returned × number_of_regions

        Args:
            sport: Sport
            event_id: Event ID from get_events()
            markets: Market types to fetch
            regions: Bookmaker regions
            odds_format: 'american' or 'decimal'

        Returns:
            Event with full odds data
        """
        markets = markets or [Market.MONEYLINE]
        market_keys = ",".join(m.value for m in markets)

        params = {
            "regions": regions,
            "markets": market_keys,
            "oddsFormat": odds_format,
        }

        result = self._request(f"/sports/{sport.value}/events/{event_id}/odds", params)
        if isinstance(result, dict) and result.get("error"):
            print(f"[ODDS API] Error: {result}")
            return None

        events = self._parse_events([result])
        return events[0] if events else None

    def _parse_events(self, data: list) -> List[Event]:
        """Parse API response into Event objects"""
        events = []
        for e in data:
            bookmakers = []
            for b in e.get("bookmakers", []):
                markets = []
                for m in b.get("markets", []):
                    outcomes = []
                    for o in m.get("outcomes", []):
                        outcomes.append(Outcome(
                            name=o["name"],
                            price=o["price"],
                            point=o.get("point"),
                            description=o.get("description"),
                        ))
                    markets.append(MarketData(
                        key=m["key"],
                        outcomes=outcomes,
                        last_update=datetime.fromisoformat(m["last_update"].replace("Z", "+00:00")) if m.get("last_update") else None,
                    ))
                bookmakers.append(BookmakerOdds(
                    key=b["key"],
                    title=b["title"],
                    markets=markets,
                    last_update=datetime.fromisoformat(b["last_update"].replace("Z", "+00:00")) if b.get("last_update") else None,
                ))

            events.append(Event(
                id=e["id"],
                sport_key=e["sport_key"],
                sport_title=e["sport_title"],
                commence_time=datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")),
                home_team=e["home_team"],
                away_team=e["away_team"],
                bookmakers=bookmakers,
            ))
        return events

    def get_consensus_odds(self, event: Event, market: str = "h2h") -> List[ConsensusOdds]:
        """
        Calculate consensus odds across all bookmakers for a market

        Args:
            event: Event with bookmaker odds
            market: Market key (h2h, spreads, totals)

        Returns:
            List of ConsensusOdds for each outcome
        """
        # Collect all outcomes by name+point
        outcome_prices: Dict[tuple, List[tuple]] = {}  # (name, point) -> [(price, book), ...]

        for book in event.bookmakers:
            for mkt in book.markets:
                if mkt.key != market:
                    continue
                for outcome in mkt.outcomes:
                    key = (outcome.name, outcome.point)
                    if key not in outcome_prices:
                        outcome_prices[key] = []
                    outcome_prices[key].append((outcome.price, book.key))

        consensus = []
        for (name, point), prices in outcome_prices.items():
            if not prices:
                continue

            price_values = [p[0] for p in prices]
            best = max(prices, key=lambda x: x[0])
            worst = min(prices, key=lambda x: x[0])

            # Average implied probabilities, not American odds directly.
            # Averaging American odds is mathematically incorrect because
            # the odds-to-probability mapping is non-linear.
            # Filter out invalid odds (american_to_probability returns None for bad values)
            valid_probs = [p for p in (american_to_probability(v) for v in price_values) if p is not None]
            if not valid_probs:
                continue
            avg_prob = sum(valid_probs) / len(valid_probs)
            # Store avg_price as probability (0-1) since that's what callers need
            # This changes the semantics: avg_price is now a probability, not American odds
            consensus.append(ConsensusOdds(
                market=market,
                outcome_name=name,
                point=point,
                avg_price=avg_prob,
                best_price=best[0],
                best_book=best[1],
                worst_price=worst[0],
                num_books=len(valid_probs),
            ))

        # Remove vig: normalize probabilities so they sum to 1.0
        # Works for N-way markets (2-way moneyline, 3-way soccer/hockey with draw).
        # For a 2-way market with vig, implied probs sum to ~1.04-1.10;
        # for a 3-way market, they sum to ~1.05-1.15.
        # Note: 3-way GAME markets are filtered out in live_arb.calculate_fair_value
        # since Kalshi's binary YES/NO doesn't cleanly map to 3-way outcomes.
        total_prob = sum(c.avg_price for c in consensus)
        if total_prob > 0 and len(consensus) >= 2:
            for c in consensus:
                c.avg_price = c.avg_price / total_prob

        return consensus


# === Kalshi Market Mapping ===

# Map Kalshi series prefixes to Odds API sports
KALSHI_TO_ODDS_API_SPORT = {
    # NFL
    "KXNFLGAME": Sport.NFL,
    "KXNFLSPREAD": Sport.NFL,
    "KXNFLTOTAL": Sport.NFL,
    "KXNFLPLAYERPASS": Sport.NFL,
    "KXNFLPLAYERRUSH": Sport.NFL,
    "KXNFLPLAYERTD": Sport.NFL,

    # NBA
    "KXNBAGAME": Sport.NBA,
    "KXNBASPREAD": Sport.NBA,
    "KXNBATOTAL": Sport.NBA,
    "KXNBAPLAYERPTS": Sport.NBA,
    "KXNBAPLAYERREB": Sport.NBA,
    "KXNBAPLAYERAST": Sport.NBA,

    # NHL
    "KXNHLGAME": Sport.NHL,
    "KXNHLSPREAD": Sport.NHL,
    "KXNHLTOTAL": Sport.NHL,

    # Soccer
    "KXEPLGAME": Sport.EPL,
    "KXEPLSPREAD": Sport.EPL,
    "KXEPLTOTAL": Sport.EPL,
    "KXEPLBTTS": Sport.EPL,
}

# Map Kalshi market types to Odds API markets
KALSHI_TO_ODDS_API_MARKET = {
    # Main markets
    "GAME": Market.MONEYLINE,
    "SPREAD": Market.SPREAD,
    "TOTAL": Market.TOTAL,

    # NFL player props
    "PLAYERPASS": Market.PLAYER_PASS_YDS,
    "PLAYERTD": Market.PLAYER_ANYTIME_TD,
    "PLAYERRUSH": Market.PLAYER_RUSH_YDS,

    # NBA player props
    "PLAYERPTS": Market.PLAYER_POINTS,
    "PLAYERREB": Market.PLAYER_REBOUNDS,
    "PLAYERAST": Market.PLAYER_ASSISTS,
}


def american_to_probability(american_odds: float) -> Optional[float]:
    """
    Convert American odds to implied probability.

    Returns None for invalid inputs:
      - Odds between -100 and +100 (exclusive) are invalid in American format
      - Results outside [0, 1] indicate bad input

    Examples:
        -110 -> 0.524 (52.4%)
        +150 -> 0.400 (40.0%)
        +50  -> None  (invalid: between -100 and +100)
        -99  -> None  (invalid: between -100 and +100)
    """
    # Reject odds in the invalid range (-100, +100) exclusive
    # American odds must be <= -100 or >= +100
    if -100 < american_odds < 100:
        return None

    if american_odds >= 100:
        prob = 100 / (american_odds + 100)
    else:
        prob = abs(american_odds) / (abs(american_odds) + 100)

    # Sanity check: probability must be in [0, 1]
    if prob < 0.0 or prob > 1.0:
        return None

    return prob


def american_to_decimal(american_odds: float) -> float:
    """
    Convert American odds to decimal odds

    Examples:
        -110 -> 1.909
        +150 -> 2.500
    """
    if american_odds >= 100:
        return (american_odds / 100) + 1
    else:
        return (100 / abs(american_odds)) + 1


def calculate_no_vig_probability(odds1: float, odds2: float) -> Optional[tuple[float, float]]:
    """
    Calculate no-vig (fair) probabilities from two-way market

    Args:
        odds1: American odds for outcome 1
        odds2: American odds for outcome 2

    Returns:
        Tuple of (prob1, prob2) that sum to 1.0, or None if either odds value is invalid
    """
    p1 = american_to_probability(odds1)
    p2 = american_to_probability(odds2)

    if p1 is None or p2 is None:
        return None

    # Remove vig by normalizing
    total = p1 + p2
    if total <= 0:
        return None
    return (p1 / total, p2 / total)


# === Example Usage ===

if __name__ == "__main__":
    import os

    # Get API key from environment or prompt
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("Set ODDS_API_KEY environment variable")
        exit(1)

    client = OddsAPIClient(api_key)

    # Get NFL events
    print("=== NFL Events ===")
    events = client.get_events(Sport.NFL)
    for e in events[:5]:
        print(f"{e.away_team} @ {e.home_team} - {e.commence_time}")

    # Get NFL odds with spreads
    print("\n=== NFL Odds ===")
    events = client.get_odds(
        Sport.NFL,
        markets=[Market.MONEYLINE, Market.SPREAD, Market.TOTAL],
        bookmakers=["draftkings", "fanduel"],
    )

    for e in events[:2]:
        print(f"\n{e.away_team} @ {e.home_team}")
        consensus = client.get_consensus_odds(e, "h2h")
        for c in consensus:
            fair_prob = c.avg_price  # avg_price is already a probability
            print(f"  {c.outcome_name}: avg={c.avg_price:+.0f} ({fair_prob:.1%}), best={c.best_price:+.0f} @ {c.best_book}")

    print(f"\nAPI quota remaining: {client.requests_remaining}")
