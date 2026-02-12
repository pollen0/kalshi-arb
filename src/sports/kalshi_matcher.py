"""
Kalshi Sports Market Matcher

Matches Kalshi sports markets to The Odds API data for arbitrage detection.

Kalshi Ticker Format Examples:
    - KXNFLGAME-26FEB09SFKC       (NFL Moneyline: SF @ KC on Feb 9)
    - KXNFLSPREAD-26FEB09SFKC-3.5 (NFL Spread: KC -3.5)
    - KXNFLTOTAL-26FEB09SFKC-O47.5 (NFL Total: Over 47.5)
    - KXNBAPLAYERPTS-26FEB04-LEBRON-O25.5 (NBA Player Points: LeBron Over 25.5)
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple, TYPE_CHECKING
from difflib import SequenceMatcher

from .odds_api import (
    OddsAPIClient,
    Sport,
    Market,
    Event,
    ConsensusOdds,
    american_to_probability,
    calculate_no_vig_probability,
)


@dataclass
class KalshiSportsMarket:
    """Parsed Kalshi sports market"""
    ticker: str
    sport: str  # NFL, NBA, NHL, etc.
    market_type: str  # GAME, SPREAD, TOTAL, PLAYER*
    date: Optional[datetime]
    teams: Optional[Tuple[str, str]]  # (away, home) or None
    player: Optional[str]
    line: Optional[float]  # Spread or total line
    side: Optional[str]  # O/U for totals, team for spread

    # Market data
    yes_bid: int = 0
    yes_ask: int = 0
    title: str = ""  # Human-readable market title

    # Fair value from Odds API
    fair_value: Optional[float] = None
    consensus_odds: Optional[float] = None  # American odds


@dataclass
class ArbitrageOpportunity:
    """Detected arbitrage between Kalshi and sportsbooks"""
    kalshi_market: KalshiSportsMarket
    kalshi_price: float  # Probability (0-1)
    fair_value: float  # From sportsbooks
    edge: float  # fair_value - kalshi_price (positive = buy)
    best_book: str
    best_odds: float  # American odds


# Sport-specific team name mappings to avoid abbreviation collisions
# (e.g., "CHI" = Bears in NFL, Bulls in NBA, Blackhawks in NHL)
_NFL_TEAMS = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders", "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SEA": "Seattle Seahawks", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}

_NBA_TEAMS = {
    "BOS": "Boston Celtics", "BKN": "Brooklyn Nets", "NYK": "New York Knicks",
    "PHI": "Philadelphia 76ers", "TOR": "Toronto Raptors", "CHI": "Chicago Bulls",
    "CLE": "Cleveland Cavaliers", "DET": "Detroit Pistons", "IND": "Indiana Pacers",
    "MIL": "Milwaukee Bucks", "ATL": "Atlanta Hawks", "CHA": "Charlotte Hornets",
    "MIA": "Miami Heat", "ORL": "Orlando Magic", "WAS": "Washington Wizards",
    "DEN": "Denver Nuggets", "MIN": "Minnesota Timberwolves", "OKC": "Oklahoma City Thunder",
    "POR": "Portland Trail Blazers", "UTA": "Utah Jazz", "GSW": "Golden State Warriors",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "PHX": "Phoenix Suns",
    "SAC": "Sacramento Kings", "DAL": "Dallas Mavericks", "HOU": "Houston Rockets",
    "MEM": "Memphis Grizzlies", "NOP": "New Orleans Pelicans", "SAS": "San Antonio Spurs",
}

_NHL_TEAMS = {
    "ANA": "Anaheim Ducks", "UTA": "Utah Hockey Club", "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres", "CGY": "Calgary Flames", "CAR": "Carolina Hurricanes",
    "CHI": "Chicago Blackhawks", "COL": "Colorado Avalanche", "CBJ": "Columbus Blue Jackets",
    "DAL": "Dallas Stars", "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers", "LA": "Los Angeles Kings", "LAK": "Los Angeles Kings",
    "MIN": "Minnesota Wild", "MTL": "Montreal Canadiens", "NSH": "Nashville Predators",
    "NJ": "New Jersey Devils", "NJD": "New Jersey Devils",
    "NY": "New York Rangers", "NYI": "New York Islanders", "NYR": "New York Rangers",
    "OTT": "Ottawa Senators", "PHI": "Philadelphia Flyers", "PIT": "Pittsburgh Penguins",
    "SJ": "San Jose Sharks", "SJS": "San Jose Sharks", "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues", "TB": "Tampa Bay Lightning", "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs", "VAN": "Vancouver Canucks", "VGK": "Vegas Golden Knights",
    "WPG": "Winnipeg Jets", "WSH": "Washington Capitals",
}

_MLB_TEAMS = {
    "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves", "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox", "CHC": "Chicago Cubs", "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians", "COL": "Colorado Rockies",
    "DET": "Detroit Tigers", "HOU": "Houston Astros", "KC": "Kansas City Royals",
    "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers", "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers", "MIN": "Minnesota Twins", "NYM": "New York Mets",
    "NYY": "New York Yankees", "OAK": "Oakland Athletics", "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates", "SD": "San Diego Padres", "SF": "San Francisco Giants",
    "SEA": "Seattle Mariners", "STL": "St. Louis Cardinals", "TB": "Tampa Bay Rays",
    "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays", "WAS": "Washington Nationals",
}

# Lookup by sport prefix (Kalshi ticker prefix -> team dict)
_SPORT_TEAM_MAP = {
    "NFL": _NFL_TEAMS, "NBA": _NBA_TEAMS, "NHL": _NHL_TEAMS, "MLB": _MLB_TEAMS,
    "EPL": {},  # Soccer uses full names, not abbreviations
}


def get_team_name(abbrev: str, sport: str = None) -> str:
    """Look up team full name by abbreviation, optionally scoped by sport."""
    if sport and sport in _SPORT_TEAM_MAP:
        name = _SPORT_TEAM_MAP[sport].get(abbrev)
        if name:
            return name
    # Fallback: search all sports (ambiguous but better than nothing)
    for teams in [_NFL_TEAMS, _NBA_TEAMS, _NHL_TEAMS, _MLB_TEAMS]:
        if abbrev in teams:
            return teams[abbrev]
    return abbrev  # Return abbreviation as-is if not found


# Legacy flat dict (for backward compatibility with _split_teams)
# Contains all abbreviations — last-write-wins for duplicates
TEAM_ABBREV = {**_NFL_TEAMS, **_NBA_TEAMS, **_NHL_TEAMS, **_MLB_TEAMS}


def _split_teams(teams_str: str, sport: str = None) -> Tuple[str, str]:
    """
    Split concatenated team codes (e.g., 'SFKC' -> ('SF', 'KC'), 'OKCSAS' -> ('OKC', 'SAS'))

    Uses known team abbreviations to find the split point.
    If sport is provided, uses the sport-specific team dict first to avoid
    cross-sport collisions (e.g., 'CHI' = Bears in NFL, Bulls in NBA).
    """
    # Determine which abbreviation set(s) to check
    sport_teams = _SPORT_TEAM_MAP.get(sport, {}) if sport else {}

    # Phase 1: Try sport-specific abbreviations first (most accurate)
    if sport_teams:
        for i in range(2, len(teams_str) - 1):
            away = teams_str[:i]
            home = teams_str[i:]
            if away in sport_teams and home in sport_teams:
                return (away, home)

    # Phase 2: Fall back to flat dict (covers cases where sport dict misses)
    for i in range(2, len(teams_str) - 1):
        away = teams_str[:i]
        home = teams_str[i:]
        if away in TEAM_ABBREV and home in TEAM_ABBREV:
            return (away, home)

    # Fallback: assume 2-char away, rest is home (common for NFL)
    if len(teams_str) >= 4:
        return (teams_str[:2], teams_str[2:])

    # Last resort: split in half
    mid = len(teams_str) // 2
    return (teams_str[:mid], teams_str[mid:])


def parse_kalshi_ticker(ticker: str) -> Optional[KalshiSportsMarket]:
    """
    Parse a Kalshi sports ticker into structured data

    Examples:
        KXNFLGAME-26FEB09SFKC -> NFL game, SF @ KC, Feb 9 2026
        KXNBASPREAD-26FEB04OKCSAS-6.5 -> NBA spread, OKC @ SAS, OKC -6.5
        KXNFLTOTAL-26FEB09SFKC-O47.5 -> NFL total, Over 47.5
    """
    # Extract base pattern: KXSPORTTYPE-DATE[TEAMS][-LINE]
    patterns = [
        # Moneyline: KXNFLGAME-26FEB09SFKC
        (r"^KX([A-Z]+)(GAME)-(\d{2}[A-Z]{3}\d{2})([A-Z]{4,6})$", "game"),

        # Spread: KXNFLSPREAD-26FEB09SFKC-3.5 or KXNBASPREAD-26FEB04OKCSAS
        (r"^KX([A-Z]+)(SPREAD)-(\d{2}[A-Z]{3}\d{2})([A-Z]{4,6})(?:-([\d.]+))?$", "spread"),

        # Total: KXNFLTOTAL-26FEB09SFKC-O47.5
        (r"^KX([A-Z]+)(TOTAL)-(\d{2}[A-Z]{3}\d{2})([A-Z]{4,6})-([OU])([\d.]+)$", "total"),

        # Player prop: KXNBAPLAYERPTS-26FEB04-LEBRON-O25.5
        (r"^KX([A-Z]+)(PLAYER[A-Z]+)-(\d{2}[A-Z]{3}\d{2})-([A-Z]+)-([OU])([\d.]+)$", "player"),
    ]

    ticker_upper = ticker.upper()

    for pattern, ptype in patterns:
        match = re.match(pattern, ticker_upper)
        if match:
            groups = match.groups()
            sport = groups[0]
            market_type = groups[1]
            date_str = groups[2]

            # Parse date (e.g., 26FEB09 -> Feb 9, 2026)
            try:
                date = datetime.strptime(date_str, "%y%b%d")
                date = date.replace(tzinfo=timezone.utc)
            except ValueError:
                date = None

            # Determine teams, player, line based on market type
            teams = None
            player = None
            line = None
            side = None

            if ptype == "game":
                teams = _split_teams(groups[3], sport)
            elif ptype == "spread":
                teams = _split_teams(groups[3], sport)
                line = float(groups[4]) if groups[4] else None
            elif ptype == "total":
                teams = _split_teams(groups[3], sport)
                side = groups[4]  # O or U
                line = float(groups[5])
            elif ptype == "player":
                player = groups[3]
                side = groups[4]  # O or U
                line = float(groups[5])

            return KalshiSportsMarket(
                ticker=ticker,
                sport=sport,
                market_type=market_type,
                date=date,
                teams=teams,
                player=player,
                line=line,
                side=side,
            )

    return None


def match_team_name(name: str, full_name: str, sport: str = None) -> float:
    """
    Calculate similarity between a team name/abbreviation and a full team name.

    Handles:
    - Abbreviations: "KC" matches "Kansas City Chiefs"
    - City names: "Los Angeles" matches "Los Angeles Kings"
    - Nicknames: "Panthers" matches "Florida Panthers"
    - Full names: "Florida Panthers" matches "Florida Panthers"

    Args:
        name: Team abbreviation or name to look up
        full_name: Full team name to match against
        sport: Optional sport code (NFL, NBA, etc.) for sport-aware lookup

    Returns: similarity score 0-1
    """
    if not name or not full_name:
        return 0.0

    name_lower = name.lower().strip()
    full_lower = full_name.lower().strip()

    # Exact match
    if name_lower == full_lower:
        return 1.0

    # Direct abbreviation lookup — sport-aware first, then fallback
    resolved = get_team_name(name, sport)
    if resolved != name:
        # We found a mapping
        expected = resolved.lower()
        if expected == full_lower:
            return 1.0
        if any(word in full_lower for word in expected.split()):
            return 0.9

    # City name match: "Los Angeles" matches "Los Angeles Kings"
    if full_lower.startswith(name_lower):
        return 0.9

    # Nickname match: "Panthers" matches "Florida Panthers"
    if name_lower in full_lower.split():
        return 0.85

    # Partial word overlap: "Tampa Bay" matches "Tampa Bay Lightning"
    name_words = set(name_lower.split())
    full_words = set(full_lower.split())
    overlap = name_words & full_words
    if overlap and len(overlap) >= len(name_words) * 0.5:
        return 0.8

    # Fuzzy match on full strings
    ratio = SequenceMatcher(None, name_lower, full_lower).ratio()
    return ratio


def match_event(kalshi_market: KalshiSportsMarket, events: List[Event]) -> Optional[Event]:
    """
    Find the best matching Odds API event for a Kalshi market

    Args:
        kalshi_market: Parsed Kalshi market
        events: List of events from Odds API

    Returns:
        Best matching event or None
    """
    if not kalshi_market.teams:
        return None

    away_abbrev, home_abbrev = kalshi_market.teams
    best_match = None
    best_score = 0

    for event in events:
        # Check date match (if available)
        if kalshi_market.date:
            event_date = event.commence_time.date()
            kalshi_date = kalshi_market.date.date()
            if event_date != kalshi_date:
                continue

        # Score team name matches (sport-aware)
        away_score = match_team_name(away_abbrev, event.away_team, kalshi_market.sport)
        home_score = match_team_name(home_abbrev, event.home_team, kalshi_market.sport)
        total_score = (away_score + home_score) / 2

        if total_score > best_score:
            best_score = total_score
            best_match = event

    # Require at least 70% confidence
    return best_match if best_score >= 0.7 else None


class KalshiSportsMatcher:
    """
    Matches Kalshi sports markets to sportsbook odds

    Usage:
        matcher = KalshiSportsMatcher(odds_api_key)
        opportunities = matcher.find_arbitrage(kalshi_markets)
    """

    SPORT_MAP = {
        "NFL": Sport.NFL,
        "NBA": Sport.NBA,
        "NHL": Sport.NHL,
        "MLB": Sport.MLB,
        "EPL": Sport.EPL,
    }

    MARKET_MAP = {
        "GAME": Market.MONEYLINE,
        "SPREAD": Market.SPREAD,
        "TOTAL": Market.TOTAL,
    }

    def __init__(self, odds_api_key: str):
        self.odds_client = OddsAPIClient(odds_api_key)
        self._events_cache: Dict[Sport, List[Event]] = {}
        self._odds_cache: Dict[str, Event] = {}  # event_id -> Event with odds

    def get_fair_value(
        self,
        kalshi_market: KalshiSportsMarket,
    ) -> Optional[Tuple[float, float, str]]:
        """
        Get fair value for a Kalshi market from sportsbook consensus

        Args:
            kalshi_market: Parsed Kalshi market

        Returns:
            Tuple of (fair_probability, american_odds, best_book) or None
        """
        # Map to Odds API sport
        sport = self.SPORT_MAP.get(kalshi_market.sport)
        if not sport:
            return None

        # Get events for the sport
        if sport not in self._events_cache:
            self._events_cache[sport] = self.odds_client.get_events(sport)

        # Match to an event
        event = match_event(kalshi_market, self._events_cache[sport])
        if not event:
            return None

        # Get odds for the event
        if event.id not in self._odds_cache:
            markets = [Market.MONEYLINE, Market.SPREAD, Market.TOTAL]
            event_with_odds = self.odds_client.get_odds(
                sport,
                markets=markets,
                bookmakers=["draftkings", "fanduel", "betmgm"],
            )
            for e in event_with_odds:
                self._odds_cache[e.id] = e

        event_odds = self._odds_cache.get(event.id)
        if not event_odds:
            return None

        # Get consensus for the specific market
        market_type = self.MARKET_MAP.get(kalshi_market.market_type)
        if not market_type:
            return None

        consensus = self.odds_client.get_consensus_odds(event_odds, market_type.value)

        # Find the matching outcome
        for c in consensus:
            # Match by team/side
            if kalshi_market.market_type == "GAME":
                # Moneyline - match team name
                team = kalshi_market.teams[1] if kalshi_market.side == "home" else kalshi_market.teams[0]
                if match_team_name(team, c.outcome_name) > 0.7:
                    fair_prob = c.avg_price  # avg_price is already a probability (averaged from implied probs)
                    return (fair_prob, c.best_price, c.best_book)

            elif kalshi_market.market_type == "SPREAD":
                # Spread - match team and line
                if c.point and abs(c.point - (kalshi_market.line or 0)) < 0.5:
                    fair_prob = c.avg_price  # avg_price is already a probability (averaged from implied probs)
                    return (fair_prob, c.best_price, c.best_book)

            elif kalshi_market.market_type == "TOTAL":
                # Total - match O/U and line
                if c.outcome_name.lower().startswith(kalshi_market.side.lower()):
                    if c.point and abs(c.point - (kalshi_market.line or 0)) < 0.5:
                        fair_prob = c.avg_price  # avg_price is already a probability (averaged from implied probs)
                        return (fair_prob, c.best_price, c.best_book)

        return None

    def find_arbitrage(
        self,
        kalshi_markets: List[KalshiSportsMarket],
        min_edge: float = 0.02,
    ) -> List[ArbitrageOpportunity]:
        """
        Find arbitrage opportunities between Kalshi and sportsbooks

        Args:
            kalshi_markets: List of Kalshi markets to check
            min_edge: Minimum edge (as decimal) to report

        Returns:
            List of arbitrage opportunities
        """
        opportunities = []

        for market in kalshi_markets:
            result = self.get_fair_value(market)
            if not result:
                continue

            fair_prob, best_odds, best_book = result

            # Calculate edge for YES side
            kalshi_yes_prob = market.yes_ask / 100 if market.yes_ask else None
            if kalshi_yes_prob:
                yes_edge = fair_prob - kalshi_yes_prob
                if yes_edge >= min_edge:
                    opportunities.append(ArbitrageOpportunity(
                        kalshi_market=market,
                        kalshi_price=kalshi_yes_prob,
                        fair_value=fair_prob,
                        edge=yes_edge,
                        best_book=best_book,
                        best_odds=best_odds,
                    ))

            # Calculate edge for NO side
            kalshi_no_prob = (100 - market.yes_bid) / 100 if market.yes_bid else None
            if kalshi_no_prob:
                fair_no = 1 - fair_prob
                no_edge = fair_no - kalshi_no_prob
                if no_edge >= min_edge:
                    opportunities.append(ArbitrageOpportunity(
                        kalshi_market=market,
                        kalshi_price=kalshi_no_prob,
                        fair_value=fair_no,
                        edge=no_edge,
                        best_book=best_book,
                        best_odds=-best_odds if best_odds > 0 else abs(best_odds),  # Flip for NO
                    ))

        # Sort by edge descending
        opportunities.sort(key=lambda x: x.edge, reverse=True)
        return opportunities


# === Testing ===

if __name__ == "__main__":
    # Test ticker parsing
    test_tickers = [
        "KXNFLGAME-26FEB09SFKC",
        "KXNFLSPREAD-26FEB09SFKC-3.5",
        "KXNFLTOTAL-26FEB09SFKC-O47.5",
        "KXNBASPREAD-26FEB04OKCSAS-6.5",
        "KXNBAPLAYERPTS-26FEB04-LEBRON-O25.5",
    ]

    print("=== Ticker Parsing ===")
    for ticker in test_tickers:
        parsed = parse_kalshi_ticker(ticker)
        if parsed:
            print(f"\n{ticker}")
            print(f"  Sport: {parsed.sport}")
            print(f"  Type: {parsed.market_type}")
            print(f"  Date: {parsed.date}")
            print(f"  Teams: {parsed.teams}")
            print(f"  Player: {parsed.player}")
            print(f"  Line: {parsed.line}")
            print(f"  Side: {parsed.side}")
        else:
            print(f"\n{ticker} - FAILED TO PARSE")
