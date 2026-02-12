"""
Live Sports Arbitrage System

Compares Kalshi live sports markets to liquid sportsbooks via The Odds API
to find and execute arbitrage opportunities during live games.

Key Features:
- Discovers live games on Kalshi (games that have started)
- Fetches real-time odds from major sportsbooks (DraftKings, FanDuel, etc.)
- Calculates edges and executes trades when profitable
- Comprehensive guardrails for risk management

Usage:
    arb = LiveSportsArbitrage(kalshi_client, odds_api_key)
    arb.start()  # Start monitoring loop
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple, Set
from enum import Enum
from collections import deque

from ..core.client import KalshiClient
from ..core.config import get_risk_config
from ..core.models import Market as KalshiMarket

from .odds_api import (
    OddsAPIClient,
    Sport,
    Market as OddsMarket,
    Event,
    ConsensusOdds,
    american_to_probability,
    calculate_no_vig_probability,
)
from .kalshi_matcher import (
    KalshiSportsMarket,
    parse_kalshi_ticker,
    match_event,
    match_team_name,
    get_team_name,
    TEAM_ABBREV,
)


class GameState(Enum):
    """Current state of a live game"""
    PRE_GAME = "pre_game"
    LIVE = "live"
    HALFTIME = "halftime"
    FINAL_MINUTES = "final_minutes"  # Last 2 minutes - too risky
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class BetSide(Enum):
    """Which side to bet"""
    YES = "yes"
    NO = "no"


@dataclass
class LiveGame:
    """Represents a live game with matched Kalshi and sportsbook data"""
    # Identifiers
    kalshi_event_ticker: str
    odds_api_event_id: str
    sport: Sport

    # Teams
    home_team: str
    away_team: str

    # Timing
    commence_time: datetime
    game_state: GameState = GameState.UNKNOWN

    # Scores (if available)
    home_score: Optional[int] = None
    away_score: Optional[int] = None

    # Available markets on Kalshi
    kalshi_markets: List[KalshiSportsMarket] = field(default_factory=list)

    # Sportsbook odds (cached)
    sportsbook_event: Optional[Event] = None
    odds_last_updated: Optional[datetime] = None

    # Custom display name (set for non-standard game names like golf tournaments)
    _display_name: str = ""

    @property
    def is_live(self) -> bool:
        """Check if game is currently live"""
        now = datetime.now(timezone.utc)
        return (
            self.commence_time <= now and
            self.game_state not in [GameState.COMPLETED, GameState.PRE_GAME]
        )

    @property
    def odds_age_seconds(self) -> float | None:
        """How old are the cached sportsbook odds"""
        if not self.odds_last_updated:
            return None
        return (datetime.now(timezone.utc) - self.odds_last_updated).total_seconds()

    @property
    def display_name(self) -> str:
        """Human-readable game name"""
        if self._display_name:
            return self._display_name
        if self.home_team and self.away_team:
            score = ""
            if self.home_score is not None and self.away_score is not None:
                score = f" ({self.away_score}-{self.home_score})"
            return f"{self.away_team} @ {self.home_team}{score}"
        return self.kalshi_event_ticker

    @display_name.setter
    def display_name(self, value: str):
        self._display_name = value


@dataclass
class LiveOpportunity:
    """A detected arbitrage opportunity on a live game"""
    # Game context
    game: LiveGame
    market_type: str  # GAME, SPREAD, TOTAL

    # Kalshi side
    kalshi_ticker: str
    kalshi_title: str
    bet_side: BetSide
    kalshi_price: float  # Probability 0-1

    # Sportsbook side
    fair_value: float  # No-vig probability from books
    best_book: str
    best_odds: float  # American odds
    num_books: int

    # Edge calculation
    edge: float  # fair_value - kalshi_price (positive = profitable)
    edge_pct: float  # Edge as percentage

    # Data freshness
    kalshi_updated: datetime
    odds_updated: datetime

    # Risk factors
    confidence: float = 1.0  # 0-1, reduced for various risk factors

    @property
    def is_stale(self) -> bool:
        """Check if opportunity data is too old"""
        max_age = 10  # seconds (H-S3: tightened from 30s for live sports)
        now = datetime.now(timezone.utc)
        kalshi_age = (now - self.kalshi_updated).total_seconds()
        odds_age = (now - self.odds_updated).total_seconds()
        return kalshi_age > max_age or odds_age > max_age

    @property
    def expected_value(self) -> float:
        """Expected value per dollar bet"""
        return self.edge * self.confidence


@dataclass
class BetRecord:
    """Record of a placed bet"""
    opportunity: LiveOpportunity
    order_id: str
    side: BetSide
    size: int  # contracts
    price: int  # cents
    placed_at: datetime
    status: str = "pending"  # pending, filled, cancelled
    filled_size: int = 0
    pnl: float = 0.0


@dataclass
class LiveArbConfig:
    """Configuration for live arbitrage system"""
    # Edge requirements
    min_edge: float = 0.05  # 5% minimum edge for live betting
    min_edge_low_confidence: float = 0.08  # 8% if confidence is reduced

    # Position limits
    max_bet_size: int = 20  # Max contracts per bet
    max_exposure_per_game: float = 100.0  # Max $ exposure per game
    max_total_exposure: float = 500.0  # Max $ total exposure
    max_bets_per_game: int = 3  # Max number of bets per game

    # Timing
    stale_data_seconds: int = 10  # Reject odds older than this (H-S3: tightened from 30s)
    min_game_time_remaining: int = 120  # Don't bet in final 2 minutes
    bet_cooldown_seconds: int = 30  # Cooldown between bets on same market

    # Data quality
    min_books_for_consensus: int = 3  # Need at least 3 books for fair value
    max_book_spread: float = 0.05  # Reject if books disagree by >5%

    # Execution
    price_buffer_cents: int = 1  # Place order 1c better than edge price
    dry_run: bool = True  # Don't actually place orders

    # Monitoring
    poll_interval_seconds: float = 5.0  # How often to check for opportunities
    odds_refresh_seconds: float = 10.0  # How often to refresh sportsbook odds


class LiveSportsArbitrage:
    """
    Live sports arbitrage detection and execution system.

    Workflow:
    1. Discover live games on Kalshi (dynamically finds ALL sports)
    2. Match to sportsbook events
    3. Fetch live odds from multiple books
    4. Calculate fair values and edges
    5. Execute trades when edge exceeds threshold
    """

    # Map Kalshi series ticker keywords to Odds API Sport enum
    SERIES_TO_SPORT = {
        "NBA": Sport.NBA,
        "NHL": Sport.NHL,
        "NFL": Sport.NFL,
        "MLB": Sport.MLB,
        "NCAAF": Sport.NCAAF,
        "NCAAB": Sport.NCAAB,
        "ATP": Sport.ATP,
        "WTA": Sport.WTA,
        "EPL": Sport.EPL,
        "UFC": Sport.UFC,
        "MMA": Sport.UFC,
        "PGA": Sport.PGA,
        "LIV": Sport.PGA,  # Map LIV Golf to PGA for odds
        "GOLF": Sport.PGA,
        "MLS": Sport.MLS,
        "BUNDESLIGA": Sport.BUNDESLIGA,
        "LALIGA": Sport.LALIGA,
        "SERIEA": Sport.SERIEA,
        "LIGUE1": Sport.LIGUE1,
        "CHAMPIONSLEAGUE": Sport.UCL,
        "UCL": Sport.UCL,
        "EUROPALEAGUE": Sport.UEL,
        "BOXING": Sport.BOXING,
        "F1": Sport.F1,
        "NASCAR": Sport.NASCAR,
    }

    # Keywords in series tickers that indicate individual game/match outcomes
    GAME_KEYWORDS = [
        "GAME", "MATCH", "FIGHT", "TOUR", "CHAMP",
        "SPREAD", "TOTAL", "OVER", "WINNER",
        "TOP5", "TOP10", "TOP20", "R1LEAD",
        "ANYSET", "EXACTMATCH", "DOUBLES",
    ]

    # Series to exclude (esports, non-arb-able, parlays, etc.)
    EXCLUDED_SERIES_KEYWORDS = [
        "CSGO", "VALORANT", "LOL", "DOTA", "OVERWATCH",
        "CALLOFDUTY", "STARCRAFT", "HALO", "FORTNITE",
        "ALPHAARENA",  # Kalshi-specific game
        "PICKLEBALLGAMES",  # Too niche for odds API
        "MULTIGAME",  # Multi-game parlays, not individual outcomes
        "MVESINGLEGAME",  # MVE single game wrappers
    ]

    def __init__(
        self,
        kalshi_client: KalshiClient,
        odds_api_key: str,
        config: LiveArbConfig = None,
    ):
        self.kalshi = kalshi_client
        self.odds_client = OddsAPIClient(odds_api_key)
        self.config = config or LiveArbConfig()

        # State
        self._live_games: Dict[str, LiveGame] = {}  # kalshi_event_ticker -> LiveGame
        self._opportunities: List[LiveOpportunity] = []
        self._bet_history: deque[BetRecord] = deque(maxlen=100)  # For recent activity display only
        self._bets_per_game: Dict[str, int] = {}  # game_id -> count of real (non-dry-run) bets
        self._exposure_by_game: Dict[str, float] = {}  # game_id -> $ exposure
        self._last_bet_time: Dict[str, datetime] = {}  # market_ticker -> last bet time

        # Series discovery cache
        self._sports_series_cache: List[str] = []  # Cached list of sports series tickers
        self._series_cache_time: Optional[datetime] = None
        self._series_cache_ttl = timedelta(hours=1)  # Refresh every hour

        # Threading
        self._running = False
        self._lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None

        # Callbacks for UI updates
        self._on_opportunity_callbacks: List[callable] = []
        self._on_bet_callbacks: List[callable] = []
        self._on_game_update_callbacks: List[callable] = []

    # === Event Registration ===

    def on_opportunity(self, callback: callable):
        """Register callback for new opportunities: callback(opportunity: LiveOpportunity)"""
        self._on_opportunity_callbacks.append(callback)

    def on_bet(self, callback: callable):
        """Register callback for placed bets: callback(bet: BetRecord)"""
        self._on_bet_callbacks.append(callback)

    def on_game_update(self, callback: callable):
        """Register callback for game updates: callback(game: LiveGame)"""
        self._on_game_update_callbacks.append(callback)

    def _notify_opportunity(self, opp: LiveOpportunity):
        for cb in self._on_opportunity_callbacks:
            try:
                cb(opp)
            except Exception as e:
                print(f"[LIVE ARB] Callback error: {e}")

    def _notify_bet(self, bet: BetRecord):
        for cb in self._on_bet_callbacks:
            try:
                cb(bet)
            except Exception as e:
                print(f"[LIVE ARB] Callback error: {e}")

    def _notify_game_update(self, game: LiveGame):
        for cb in self._on_game_update_callbacks:
            try:
                cb(game)
            except Exception as e:
                print(f"[LIVE ARB] Callback error: {e}")

    # === Discovery ===

    def _discover_sports_series(self) -> List[str]:
        """
        Dynamically discover all sports series on Kalshi that represent
        individual game/match outcomes.

        Results are cached for 1 hour to avoid excessive API calls.
        """
        now = datetime.now(timezone.utc)

        # Return cached if fresh
        if (self._sports_series_cache and self._series_cache_time and
                now - self._series_cache_time < self._series_cache_ttl):
            return self._sports_series_cache

        print("[LIVE ARB] Discovering sports series from Kalshi...")
        all_series = []
        cursor = None

        # Paginate through all series
        for _ in range(30):  # Max 30 pages = 6000 series
            url = "/series?limit=200"
            if cursor:
                url += "&cursor=" + cursor
            result = self.kalshi._request("GET", url)
            if not isinstance(result, dict):
                break
            series = result.get("series", [])
            cursor = result.get("cursor", "")
            all_series.extend(series)
            if not cursor or len(series) < 200:
                break

        # Filter for sports category and game-like series
        sports_series = []
        for s in all_series:
            if s.get("category") != "Sports":
                continue
            ticker = s.get("ticker", "")
            ticker_upper = ticker.upper()

            # Exclude esports and non-arbitrageable series
            if any(ex in ticker_upper for ex in self.EXCLUDED_SERIES_KEYWORDS):
                continue

            # Check if this series represents individual game outcomes
            has_game_keyword = any(kw in ticker_upper for kw in self.GAME_KEYWORDS)
            if has_game_keyword:
                sports_series.append(ticker)

        self._sports_series_cache = sports_series
        self._series_cache_time = now
        print(f"[LIVE ARB] Found {len(sports_series)} sports game/match series")

        return sports_series

    def _infer_sport(self, series_ticker: str, title: str = "") -> Optional[Sport]:
        """Infer the Sport enum from a series ticker or event title.

        Uses prefix matching on the ticker (after 'KX') to avoid false positives
        from short keywords appearing inside longer words (e.g., 'PGA' in 'CHAMPIONSHIPGAME').
        """
        ticker_upper = series_ticker.upper()

        # Strip KX prefix for prefix matching
        ticker_body = ticker_upper[2:] if ticker_upper.startswith("KX") else ticker_upper

        # Prefix patterns: checked against the START of the ticker body
        # This prevents false matches like "PGA" in "EFLCHAMPIONSHIPGAME"
        prefix_patterns = [
            # American Football
            (["NFL", "NFLG", "SUPERBOWL"], Sport.NFL),
            (["NCAAF", "COLLEGEFOOTBALL", "CFP"], Sport.NCAAF),
            # Basketball
            (["NBA", "NBAG"], Sport.NBA),
            (["NCAAB", "COLLEGEBASKETBALL", "MARCHMADNESS"], Sport.NCAAB),
            (["WNBA"], Sport.WNBA),
            (["EUROLEAGUE", "EUROCUP", "FIBA"], Sport.EUROLEAGUE),
            # Hockey
            (["NHL", "NHLG", "STANLEYCUP"], Sport.NHL),
            (["KHL"], Sport.NHL),  # Russian hockey
            # Baseball
            (["MLB", "MLBG", "WORLDSERIES"], Sport.MLB),
            # Tennis
            (["ATP", "ATPM", "ATPG"], Sport.ATP),
            (["WTA", "WTAM", "WTAG"], Sport.WTA),
            # Golf
            (["PGA", "PGAT", "MASTERS"], Sport.PGA),
            (["LIV", "LIVG", "LIVT"], Sport.PGA),
            # Soccer
            (["EPL", "PREMIERLEAGUE"], Sport.EPL),
            (["EFL", "FACUP"], Sport.EPL),  # English Football League
            (["MLS"], Sport.MLS),
            (["BUNDESLIGA"], Sport.BUNDESLIGA),
            (["LALIGA"], Sport.LALIGA),
            (["SERIEA", "COPPAITALIA"], Sport.SERIEA),
            (["LIGUE1", "COUPEFRANCE"], Sport.LIGUE1),
            (["UCL", "CHAMPIONSLEAGUE"], Sport.UCL),
            (["UEL", "EUROPALEAGUE"], Sport.UEL),
            (["SOCCER", "FOOTBALL", "WPL"], Sport.MLS),
            # MMA/Boxing
            (["UFC", "MMA"], Sport.UFC),
            (["BOXING", "BOX"], Sport.BOXING),
            # Motorsport
            (["F1", "FORMULA"], Sport.F1),
            (["NASCAR"], Sport.NASCAR),
        ]

        # Phase 1: prefix match on ticker body (most reliable)
        for keywords, sport in prefix_patterns:
            for kw in keywords:
                if ticker_body.startswith(kw):
                    return sport

        # Phase 2: substring match on ticker for longer unique keywords only
        # Only use keywords that are 5+ chars to avoid false positives
        for keywords, sport in prefix_patterns:
            for kw in keywords:
                if len(kw) >= 5 and kw in ticker_upper:
                    return sport

        # Phase 3: check event title for clues
        title_upper = title.upper()
        for keywords, sport in prefix_patterns:
            for kw in keywords:
                if kw in title_upper:
                    return sport

        # Unknown sport - return None to skip unrecognized sports
        return None

    def discover_live_kalshi_games(self) -> List[LiveGame]:
        """
        Find all currently live/upcoming sports games on Kalshi.

        Uses dynamic series discovery to find ALL sports, not just
        NFL/NBA/NHL/MLB. Covers tennis, golf, soccer, UFC, college, etc.
        """
        live_games = []
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%y%b%d").upper()  # e.g. "26FEB04"

        # Get all sports series dynamically
        sports_series = self._discover_sports_series()

        # Check each series for open events
        for series_ticker in sports_series:
            try:
                events = self.kalshi.get_events(
                    series_ticker=series_ticker,
                    status="open",
                    limit=20,
                )

                for event in events:
                    event_ticker = event.get("event_ticker", "")
                    title = event.get("title", "")

                    if not event_ticker:
                        continue

                    # Skip if already tracked
                    with self._lock:
                        if event_ticker in self._live_games:
                            game = self._live_games[event_ticker]
                            if game not in live_games:
                                live_games.append(game)
                            continue

                    # Infer sport from series/title
                    sport = self._infer_sport(series_ticker, title)
                    if sport is None:
                        continue  # Skip unrecognized sports

                    # Extract team/player names from event title
                    home_team = ""
                    away_team = ""
                    display_name = title

                    # Parse "X vs Y" or "X at Y" patterns
                    for separator in [" vs ", " at ", " v "]:
                        if separator in title:
                            parts = title.split(separator, 1)
                            away_team = parts[0].strip()
                            home_team = parts[1].strip()
                            # Clean suffixes from team names (e.g., ": Total Points", " Winner?")
                            for suffix in [": Total Points", ": Spreads", ": Totals",
                                           " Winner?", " Winner", "?"]:
                                home_team = home_team.removesuffix(suffix)
                                away_team = away_team.removesuffix(suffix)
                            home_team = home_team.strip()
                            away_team = away_team.strip()
                            display_name = f"{away_team} @ {home_team}"
                            break

                    # If no vs/at pattern, use full title
                    if not home_team and not away_team:
                        # For formats like "LIV Golf Riyadh Champion?"
                        display_name = title.rstrip("?")

                    # Create LiveGame
                    game = LiveGame(
                        kalshi_event_ticker=event_ticker,
                        odds_api_event_id="",
                        sport=sport,
                        home_team=home_team,
                        away_team=away_team,
                        commence_time=now,  # Will be refined from market data
                        game_state=GameState.LIVE,
                    )
                    game.display_name = display_name

                    with self._lock:
                        self._live_games[event_ticker] = game
                    live_games.append(game)

            except Exception as e:
                # Don't spam logs for series with no events
                if "404" not in str(e) and "not found" not in str(e).lower():
                    print(f"[LIVE ARB] Error checking {series_ticker}: {e}")

        # Evict completed/ended games from _live_games to prevent memory leak.
        # Any game we're tracking that wasn't returned by the "open" query is done.
        active_tickers = {g.kalshi_event_ticker for g in live_games}
        with self._lock:
            stale_tickers = [
                ticker for ticker in self._live_games
                if ticker not in active_tickers
            ]
            for ticker in stale_tickers:
                evicted = self._live_games.pop(ticker)
                evicted.game_state = GameState.COMPLETED
                print(f"[LIVE ARB] Evicted completed game: {evicted.display_name}")

        print(f"[LIVE ARB] Found {len(live_games)} live/upcoming events ({len(stale_tickers)} evicted)")
        return live_games

    def refresh_game_markets(self, game: LiveGame):
        """Fetch/refresh market data for a specific game."""
        try:
            self._fetch_game_markets(game)
        except Exception as e:
            print(f"[LIVE ARB] Error fetching markets for {game.kalshi_event_ticker}: {e}")

    def _fetch_game_markets(self, game: LiveGame):
        """Fetch current market data for a game's event."""
        result = self.kalshi._request(
            "GET",
            f"/markets?event_ticker={game.kalshi_event_ticker}&limit=50",
        )
        raw_markets = result.get("markets", []) if isinstance(result, dict) else []

        for m in raw_markets:
            ticker = m.get("ticker", "")
            title = m.get("title", "")
            status = m.get("status", "")

            # Skip non-active markets
            if status not in ("active", "open"):
                continue

            # Try to parse the ticker with the original parser
            parsed = parse_kalshi_ticker(ticker)
            if parsed:
                parsed.yes_bid = m.get("yes_bid", 0)
                parsed.yes_ask = m.get("yes_ask", 0)
            else:
                # Infer market type and side from ticker and event context
                market_type, line, side = self._infer_market_info(
                    ticker, title, game
                )
                parsed = KalshiSportsMarket(
                    ticker=ticker,
                    sport=game.sport.name,
                    market_type=market_type,
                    date=None,
                    teams=(game.away_team, game.home_team) if game.away_team else None,
                    player=None,
                    line=line,
                    side=side,
                    yes_bid=m.get("yes_bid", 0),
                    yes_ask=m.get("yes_ask", 0),
                )

            parsed.title = title

            # Update or add market
            existing = None
            for km in game.kalshi_markets:
                if km.ticker == ticker:
                    existing = km
                    break

            if existing:
                existing.yes_bid = parsed.yes_bid
                existing.yes_ask = parsed.yes_ask
            else:
                game.kalshi_markets.append(parsed)

    def _infer_market_info(
        self, ticker: str, title: str, game: LiveGame
    ) -> tuple:
        """
        Infer market type, line, and side from ticker and title.

        Returns: (market_type, line, side)

        Examples:
            KXNHLGAME-26FEB05LAVGK-VGK -> ("GAME", None, "VGK")
            KXNHLTOTAL-26FEB05LAVGK-5  -> ("TOTAL", 5.0, None)
            KXNHLSPREAD-26FEB05LAVGK   -> ("SPREAD", None, None)
        """
        ticker_upper = ticker.upper()
        event_ticker_upper = game.kalshi_event_ticker.upper()

        # Determine market type from the event ticker
        market_type = "GAME"  # default
        if "TOTAL" in event_ticker_upper:
            market_type = "TOTAL"
        elif "SPREAD" in event_ticker_upper:
            market_type = "SPREAD"
        elif "1HTOTAL" in event_ticker_upper:
            market_type = "TOTAL"
        elif "4QSPREAD" in event_ticker_upper:
            market_type = "SPREAD"

        # Get the suffix (part after the event ticker)
        # e.g., "KXNHLGAME-26FEB05LAVGK-VGK" -> suffix = "VGK"
        suffix = ""
        if ticker_upper.startswith(event_ticker_upper):
            remainder = ticker[len(game.kalshi_event_ticker):]
            if remainder.startswith("-"):
                suffix = remainder[1:]

        line = None
        side = None

        if market_type == "GAME":
            # Suffix is the team abbreviation (which team YES represents)
            side = suffix if suffix else None

        elif market_type == "TOTAL":
            # Suffix is the line number (e.g., "5", "6.5")
            try:
                line = float(suffix) if suffix else None
            except ValueError:
                # Might be O5.5 or U5.5
                if suffix and suffix[0] in ("O", "U"):
                    side = suffix[0]
                    try:
                        line = float(suffix[1:])
                    except ValueError:
                        pass

        elif market_type == "SPREAD":
            # Suffix might be a spread value
            try:
                line = float(suffix) if suffix else None
            except ValueError:
                side = suffix

        return (market_type, line, side)

    def match_to_sportsbooks(self, game: LiveGame) -> bool:
        """
        Match a Kalshi game to The Odds API event and fetch live odds.

        Returns True if successfully matched and odds fetched.
        """
        try:
            # Get events for the sport
            events = self.odds_client.get_events(game.sport)

            # Find best match by team names
            best_match = None
            best_score = 0

            for event in events:
                # Score team name matches
                away_score = max(
                    match_team_name(game.away_team.split()[-1], event.away_team) if game.away_team else 0,
                    match_team_name(game.away_team, event.away_team) if game.away_team else 0,
                )
                home_score = max(
                    match_team_name(game.home_team.split()[-1], event.home_team) if game.home_team else 0,
                    match_team_name(game.home_team, event.home_team) if game.home_team else 0,
                )
                total_score = (away_score + home_score) / 2

                if total_score > best_score and total_score >= 0.6:
                    best_score = total_score
                    best_match = event

            if not best_match:
                return False

            game.odds_api_event_id = best_match.id

            # Check API quota before expensive odds call
            remaining = self.odds_client.requests_remaining
            if remaining is not None and remaining < 50:
                print(f"[LIVE ARB] API quota low ({remaining} remaining) — skipping odds fetch")
                return False

            # Fetch live odds
            event_with_odds = self.odds_client.get_event_odds(
                game.sport,
                best_match.id,
                markets=[OddsMarket.MONEYLINE, OddsMarket.SPREAD, OddsMarket.TOTAL],
                regions="us",
            )

            if event_with_odds:
                game.sportsbook_event = event_with_odds
                game.odds_last_updated = datetime.now(timezone.utc)
                return True

            return False

        except Exception as e:
            print(f"[LIVE ARB] Error matching {game.display_name}: {e}")
            return False

    # === Edge Calculation ===

    def calculate_fair_value(
        self,
        game: LiveGame,
        kalshi_market: KalshiSportsMarket,
    ) -> Optional[Tuple[float, float, str, int]]:
        """
        Calculate fair value for a Kalshi market from sportsbook consensus.

        Returns: (fair_prob, best_odds, best_book, num_books) or None
        """
        if not game.sportsbook_event:
            return None

        # Determine market type
        market_type_map = {
            "GAME": "h2h",
            "SPREAD": "spreads",
            "TOTAL": "totals",
        }
        market_key = market_type_map.get(kalshi_market.market_type)
        if not market_key:
            return None

        # Get consensus from all bookmakers
        consensus = self.odds_client.get_consensus_odds(game.sportsbook_event, market_key)

        if not consensus:
            return None

        # M7-S: Enforce max_book_spread — skip if sportsbooks disagree too much.
        # The spread is the implied probability gap between the best and worst
        # odds across books for the same outcome. Wide disagreement means the
        # fair value estimate is unreliable.
        for c in consensus:
            best_prob = american_to_probability(c.best_price)
            worst_prob = american_to_probability(c.worst_price)
            if best_prob is not None and worst_prob is not None:
                book_spread = abs(best_prob - worst_prob)
                if book_spread > self.config.max_book_spread:
                    return None  # Books disagree too much for reliable fair value

        # H-S1: Guard against 3-way markets (soccer/hockey moneyline with Draw).
        # Vig removal normalizes all N outcomes to sum to 1.0, which is correct.
        # But for GAME markets, having >2 outcomes means the NO-side fair value
        # (1 - fair_prob) includes the draw probability, while the best_odds
        # field can only represent a single opposing outcome. Skip 3-way GAME
        # markets entirely since we can't map them to Kalshi's binary YES/NO.
        if kalshi_market.market_type == "GAME" and len(consensus) > 2:
            draw_outcomes = [c for c in consensus if "draw" in c.outcome_name.lower()]
            if draw_outcomes:
                return None  # 3-way market with draw -- not supported

        # Find matching outcome
        for c in consensus:
            matched = False

            if kalshi_market.market_type == "GAME":
                # Moneyline: determine which team the Kalshi market's YES represents
                # The 'side' field holds the team abbreviation (e.g., "VGK", "LA")
                team_id = kalshi_market.side or ""
                sport_code = kalshi_market.sport  # e.g., "NFL", "NBA", "NHL"

                if team_id:
                    # Try matching the abbreviation to the consensus outcome name (sport-aware)
                    team_full = get_team_name(team_id, sport_code)
                    if match_team_name(team_full, c.outcome_name, sport_code) > 0.6:
                        matched = True
                    # Also try matching team_id directly (city name like "Vegas")
                    elif match_team_name(team_id, c.outcome_name, sport_code) > 0.6:
                        matched = True
                elif kalshi_market.teams:
                    # Fallback: use teams tuple (home team = teams[1])
                    home_abbrev = kalshi_market.teams[1]
                    home_full = get_team_name(home_abbrev, sport_code)
                    if match_team_name(home_full, c.outcome_name, sport_code) > 0.6:
                        matched = True

            elif kalshi_market.market_type == "SPREAD":
                # Match by point spread
                if c.point is not None and kalshi_market.line is not None:
                    if abs(c.point - kalshi_market.line) < 0.5:
                        matched = True

            elif kalshi_market.market_type == "TOTAL":
                # Kalshi totals: YES = "N+ goals/points", NO = "under N"
                # Kalshi integer line N is equivalent to sportsbook Over (N-0.5)
                # e.g., Kalshi "6+" = sportsbook "Over 5.5"
                if kalshi_market.line is not None and c.point is not None:
                    # Convert Kalshi integer line to sportsbook-equivalent
                    # Kalshi 6 matches sportsbook 5.5 (exactly)
                    kalshi_equiv = kalshi_market.line - 0.5
                    if abs(c.point - kalshi_equiv) <= 0.1:
                        if c.outcome_name and "over" in c.outcome_name.lower():
                            matched = True
                elif kalshi_market.side and c.outcome_name:
                    # Explicit O/U side
                    side_match = (
                        (kalshi_market.side.upper() == "O" and "over" in c.outcome_name.lower()) or
                        (kalshi_market.side.upper() == "U" and "under" in c.outcome_name.lower())
                    )
                    if side_match and c.point and kalshi_market.line:
                        if abs(c.point - kalshi_market.line) <= 0.1:
                            matched = True

            if matched:
                fair_prob = c.avg_price  # avg_price is already a probability (averaged from implied probs)
                return (fair_prob, c.best_price, c.best_book, c.num_books)

        return None

    def find_opportunities(self, game: LiveGame) -> List[LiveOpportunity]:
        """
        Find all arbitrage opportunities for a live game.
        """
        opportunities = []
        now = datetime.now(timezone.utc)

        if not game.sportsbook_event:
            return []

        for kalshi_market in game.kalshi_markets:
            # Get fair value from sportsbooks
            result = self.calculate_fair_value(game, kalshi_market)
            if not result:
                continue

            fair_prob, best_odds, best_book, num_books = result

            # Check data quality
            if num_books < self.config.min_books_for_consensus:
                continue

            # Calculate edge for YES side (buying YES on Kalshi)
            if kalshi_market.yes_ask and kalshi_market.yes_ask > 0:
                kalshi_yes_prob = kalshi_market.yes_ask / 100
                yes_edge = fair_prob - kalshi_yes_prob

                if yes_edge >= self.config.min_edge:
                    opp = LiveOpportunity(
                        game=game,
                        market_type=kalshi_market.market_type,
                        kalshi_ticker=kalshi_market.ticker,
                        kalshi_title=f"{game.display_name} - {kalshi_market.market_type}",
                        bet_side=BetSide.YES,
                        kalshi_price=kalshi_yes_prob,
                        fair_value=fair_prob,
                        best_book=best_book,
                        best_odds=best_odds,
                        num_books=num_books,
                        edge=yes_edge,
                        edge_pct=yes_edge * 100,
                        kalshi_updated=now,
                        odds_updated=game.odds_last_updated or now,
                    )
                    opportunities.append(opp)

            # Calculate edge for NO side (buying NO on Kalshi)
            # NO on Kalshi = the matched team does NOT win.
            if kalshi_market.yes_bid and kalshi_market.yes_bid > 0:
                kalshi_no_prob = (100 - kalshi_market.yes_bid) / 100
                fair_no = 1 - fair_prob
                no_edge = fair_no - kalshi_no_prob

                if no_edge >= self.config.min_edge:
                    # H-S2: Convert fair_no probability to American odds for display.
                    # Previously used sign-flip of YES-side odds which is wrong --
                    # e.g. YES at +150 (40%) flipped to -150 (60%) but the real
                    # opposing line could be -180 (64.3%).
                    if fair_no > 0 and fair_no < 1:
                        if fair_no >= 0.5:
                            no_display_odds = -(fair_no / (1 - fair_no)) * 100
                        else:
                            no_display_odds = ((1 - fair_no) / fair_no) * 100
                    else:
                        no_display_odds = 0.0

                    opp = LiveOpportunity(
                        game=game,
                        market_type=kalshi_market.market_type,
                        kalshi_ticker=kalshi_market.ticker,
                        kalshi_title=f"{game.display_name} - {kalshi_market.market_type}",
                        bet_side=BetSide.NO,
                        kalshi_price=kalshi_no_prob,
                        fair_value=fair_no,
                        best_book=best_book,
                        best_odds=no_display_odds,
                        num_books=num_books,
                        edge=no_edge,
                        edge_pct=no_edge * 100,
                        kalshi_updated=now,
                        odds_updated=game.odds_last_updated or now,
                    )
                    opportunities.append(opp)

        # Sort by edge
        opportunities.sort(key=lambda x: x.edge, reverse=True)
        return opportunities

    # === Guardrails ===

    def check_guardrails(self, opp: LiveOpportunity) -> Tuple[bool, str]:
        """
        Check all guardrails before placing a bet.

        Returns: (can_bet, reason_if_not)
        """
        # Check stale data
        if opp.is_stale:
            return False, f"Data too stale (>{self.config.stale_data_seconds}s)"

        # Check edge threshold
        min_edge = self.config.min_edge
        if opp.confidence < 1.0:
            min_edge = self.config.min_edge_low_confidence
        if opp.edge < min_edge:
            return False, f"Edge too low ({opp.edge_pct:.1f}% < {min_edge*100:.0f}%)"

        # Check book consensus quality
        if opp.num_books < self.config.min_books_for_consensus:
            return False, f"Not enough books ({opp.num_books} < {self.config.min_books_for_consensus})"

        # Check game state
        if opp.game.game_state == GameState.FINAL_MINUTES:
            return False, "Game in final minutes - too risky"
        if opp.game.game_state == GameState.COMPLETED:
            return False, "Game completed"

        # Check exposure per game
        game_id = opp.game.kalshi_event_ticker
        current_exposure = self._exposure_by_game.get(game_id, 0)
        if current_exposure >= self.config.max_exposure_per_game:
            return False, f"Max exposure per game reached (${current_exposure:.0f})"

        # Check total exposure
        total_exposure = sum(self._exposure_by_game.values())
        if total_exposure >= self.config.max_total_exposure:
            return False, f"Max total exposure reached (${total_exposure:.0f})"

        # Check bet count per game (M9-S: only counts real bets, not dry-runs;
        # M10-S: uses dedicated counter dict instead of deque which silently drops old entries)
        game_bets = self._bets_per_game.get(game_id, 0)
        if game_bets >= self.config.max_bets_per_game:
            return False, f"Max bets per game reached ({game_bets})"

        # Check cooldown
        last_bet = self._last_bet_time.get(opp.kalshi_ticker)
        if last_bet:
            elapsed = (datetime.now(timezone.utc) - last_bet).total_seconds()
            if elapsed < self.config.bet_cooldown_seconds:
                return False, f"Cooldown active ({elapsed:.0f}s < {self.config.bet_cooldown_seconds}s)"

        return True, ""

    # === Execution ===

    def execute_bet(self, opp: LiveOpportunity) -> Optional[BetRecord]:
        """
        Execute a bet on an opportunity.

        Returns BetRecord if successful, None otherwise.
        """
        # Final guardrail check
        can_bet, reason = self.check_guardrails(opp)
        if not can_bet:
            print(f"[LIVE ARB] Blocked: {reason}")
            return None

        # Calculate bet size
        remaining_game_exposure = self.config.max_exposure_per_game - self._exposure_by_game.get(opp.game.kalshi_event_ticker, 0)
        remaining_total_exposure = self.config.max_total_exposure - sum(self._exposure_by_game.values())
        max_exposure = min(remaining_game_exposure, remaining_total_exposure)

        # Size based on edge and confidence
        target_exposure = min(
            max_exposure,
            self.config.max_bet_size * opp.kalshi_price,  # Max contracts * price
        )

        if opp.bet_side == BetSide.YES:
            price = int(opp.kalshi_price * 100) - self.config.price_buffer_cents
            price = max(3, min(97, price))
        else:
            # NO side price
            price = int(opp.kalshi_price * 100) - self.config.price_buffer_cents
            price = max(3, min(97, price))

        size = min(self.config.max_bet_size, int(target_exposure / (price / 100)))
        size = max(1, size)

        now = datetime.now(timezone.utc)

        # DRY RUN MODE
        if self.config.dry_run:
            print(f"[LIVE ARB] DRY RUN: Would bet {opp.bet_side.value.upper()} {size}x @ {price}c on {opp.kalshi_ticker}")
            print(f"           Edge: {opp.edge_pct:.1f}%, FV: {opp.fair_value:.1%}, Best: {opp.best_book}")

            bet = BetRecord(
                opportunity=opp,
                order_id=f"DRY-{int(now.timestamp())}",
                side=opp.bet_side,
                size=size,
                price=price,
                placed_at=now,
                status="dry_run",
            )
            self._bet_history.append(bet)
            self._last_bet_time[opp.kalshi_ticker] = now
            self._notify_bet(bet)
            return bet

        # LIVE EXECUTION
        try:
            result = self.kalshi.place_order(
                ticker=opp.kalshi_ticker,
                side=opp.bet_side.value,
                action="buy",
                count=size,
                price=price,
            )

            if result.get("error"):
                print(f"[LIVE ARB] Order failed: {result.get('message', 'Unknown error')}")
                return None

            order_id = result.get("order", {}).get("order_id", "")

            bet = BetRecord(
                opportunity=opp,
                order_id=order_id,
                side=opp.bet_side,
                size=size,
                price=price,
                placed_at=now,
                status="pending",
            )

            # Update tracking
            with self._lock:
                self._bet_history.append(bet)
                self._last_bet_time[opp.kalshi_ticker] = now

                game_id = opp.game.kalshi_event_ticker
                exposure = size * price / 100
                self._exposure_by_game[game_id] = self._exposure_by_game.get(game_id, 0) + exposure
                # M9-S/M10-S: Increment real bet counter (dry-runs don't reach here)
                self._bets_per_game[game_id] = self._bets_per_game.get(game_id, 0) + 1

            print(f"[LIVE ARB] ORDER PLACED: {opp.bet_side.value.upper()} {size}x @ {price}c on {opp.kalshi_ticker}")
            print(f"           Edge: {opp.edge_pct:.1f}%, Order ID: {order_id}")

            self._notify_bet(bet)
            return bet

        except Exception as e:
            print(f"[LIVE ARB] Execution error: {e}")
            return None

    def verify_pending_bets(self):
        """Check pending bets and update status (filled/cancelled).

        Also corrects exposure tracking: if an order was cancelled or only
        partially filled, subtract the unfilled portion from _exposure_by_game.
        """
        with self._lock:
            pending = [b for b in self._bet_history if b.status == "pending"]

        for bet in pending:
            try:
                status = self.kalshi.get_order_status(bet.order_id)
                if not status:
                    continue

                order_status = status.get("status", "")
                filled_count = status.get("filled_count", 0)

                with self._lock:
                    if order_status in ("filled", "executed"):
                        bet.status = "filled"
                        bet.filled_size = filled_count
                    elif order_status in ("canceled", "cancelled", "expired"):
                        bet.status = "cancelled"
                        bet.filled_size = filled_count

                        # Correct exposure: subtract unfilled portion
                        unfilled = bet.size - filled_count
                        if unfilled > 0:
                            game_id = bet.opportunity.game.kalshi_event_ticker
                            correction = unfilled * bet.price / 100
                            self._exposure_by_game[game_id] = max(
                                0, self._exposure_by_game.get(game_id, 0) - correction
                            )
                            print(f"[LIVE ARB] Exposure corrected: -{correction:.2f} for {game_id} (order {order_status})")
            except Exception as e:
                print(f"[LIVE ARB] Fill verification error for {bet.order_id}: {e}")

    def reconcile_exposure(self):
        """Reconcile _exposure_by_game against real Kalshi positions.

        Runs periodically to catch any drift between tracked and actual exposure.
        Replaces the in-memory accumulator with ground truth from the API.
        """
        try:
            positions = self.kalshi.get_positions()
            if positions is None:
                return  # API error — keep current tracking

            # Build actual exposure by game from real positions
            actual_exposure: Dict[str, float] = {}
            for pos in positions:
                # Sports tickers contain the event ticker as a prefix
                # Map position back to game by checking tracked games
                game_id = self._find_game_for_ticker(pos.ticker)
                if game_id:
                    # Exposure = quantity * avg_price (already in dollars)
                    exposure = pos.quantity * pos.avg_price
                    actual_exposure[game_id] = actual_exposure.get(game_id, 0) + exposure

            with self._lock:
                old_total = sum(self._exposure_by_game.values())
                new_total = sum(actual_exposure.values())

                if abs(old_total - new_total) > 1.0:  # >$1 drift
                    print(f"[LIVE ARB] Exposure reconciled: tracked=${old_total:.2f} -> actual=${new_total:.2f}")

                # Replace tracked exposure with API ground truth
                # Keep entries for games we know about but have 0 actual exposure (game ended)
                self._exposure_by_game = actual_exposure

        except Exception as e:
            print(f"[LIVE ARB] Exposure reconciliation error: {e}")

    def _find_game_for_ticker(self, ticker: str) -> Optional[str]:
        """Find the game event_ticker that a market ticker belongs to."""
        # Check current live games
        with self._lock:
            games_snapshot = list(self._live_games.values())
        for game in games_snapshot:
            for market in game.kalshi_markets:
                if market.ticker == ticker:
                    return game.kalshi_event_ticker
        # Fallback: check bet history
        for bet in self._bet_history:
            if bet.opportunity.kalshi_ticker == ticker:
                return bet.opportunity.game.kalshi_event_ticker
        return None

    # === Main Loop ===

    # Sports supported by The Odds API (only these can be matched for arb)
    MATCHABLE_SPORTS = {
        Sport.NFL, Sport.NBA, Sport.NHL, Sport.MLB, Sport.NCAAF, Sport.NCAAB,
        Sport.EPL, Sport.MLS, Sport.BUNDESLIGA, Sport.LALIGA, Sport.SERIEA,
        Sport.LIGUE1, Sport.UCL, Sport.UEL, Sport.UFC, Sport.BOXING,
        Sport.ATP, Sport.WTA, Sport.PGA, Sport.F1, Sport.NASCAR,
        Sport.WNBA, Sport.EUROLEAGUE,
    }

    def _check_portfolio_risk(self) -> Tuple[bool, str]:
        """Check portfolio-level risk limits from RiskConfig (shared with financial).
        Returns (ok, reason) — if not ok, sports should halt.

        SAFETY: Fails CLOSED on any error — if we can't verify risk, don't trade.
        """
        try:
            risk_config = get_risk_config()
            balance = self.kalshi.get_balance()

            # Fail closed: if balance check fails, halt trading
            if balance is None:
                return False, "Cannot verify balance (API error)"
            if balance <= 0:
                return False, "Balance is zero or negative"

            # Derive dynamic exposure cap from portfolio balance
            max_exposure_dollars = balance * risk_config.max_total_exposure
            self.config.max_total_exposure = max_exposure_dollars

            # Check total portfolio exposure (financial + sports positions combined)
            positions = self.kalshi.get_positions()
            if positions is not None:
                total_exposure = sum(p.quantity * p.avg_price for p in positions)
                if total_exposure >= max_exposure_dollars:
                    return False, (
                        f"Total portfolio exposure ${total_exposure:.0f} >= "
                        f"limit ${max_exposure_dollars:.0f} (includes financial positions)"
                    )

            # Check daily loss limit using the auto-trader's persisted daily state
            state_file = os.path.join(os.path.dirname(__file__), "..", "..", "data", "daily_state.json")
            state_file = os.path.abspath(state_file)
            if os.path.exists(state_file):
                with open(state_file, "r") as f:
                    daily = json.load(f)
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if daily.get("date") == today and daily.get("starting_equity"):
                    starting = daily["starting_equity"]
                    if starting > 0:
                        # Use balance as rough equity proxy (positions not tracked here)
                        pnl_pct = (balance - starting) / starting
                        if pnl_pct < -risk_config.max_daily_loss_pct:
                            return False, f"Portfolio daily loss limit: {pnl_pct*100:.1f}%"

                # A1: Cumulative drawdown protection (matches financial module's 15% max).
                # peak_equity is persisted by the financial auto_trader across sessions.
                peak_equity = daily.get("peak_equity")
                if peak_equity and peak_equity > 0:
                    drawdown_pct = (peak_equity - balance) / peak_equity
                    if drawdown_pct > risk_config.max_drawdown_pct:
                        return False, (
                            f"Max drawdown breached: {drawdown_pct*100:.1f}% from "
                            f"peak ${peak_equity:.2f} (limit {risk_config.max_drawdown_pct*100:.0f}%)"
                        )

            return True, ""
        except Exception as e:
            print(f"[LIVE ARB] Portfolio risk check error (HALTING): {e}")
            return False, f"Risk check failed: {e}"  # Fail CLOSED

    def scan_once(self) -> List[LiveOpportunity]:
        """
        Run one scan cycle: discover games, fetch odds, find opportunities.
        """
        # Check portfolio-level risk before scanning
        ok, reason = self._check_portfolio_risk()
        if not ok:
            print(f"[LIVE ARB] HALTED by portfolio risk: {reason}")
            return []

        all_opportunities = []

        # Discover live games on Kalshi
        live_games = self.discover_live_kalshi_games()

        if not live_games:
            return []

        # Filter to sports that The Odds API supports
        matchable_games = [g for g in live_games if g.sport in self.MATCHABLE_SPORTS]
        print(f"[LIVE ARB] {len(live_games)} events on Kalshi, {len(matchable_games)} matchable")

        # Fetch markets for games that don't have market data yet (limit batch size)
        games_needing_markets = [g for g in matchable_games if not g.kalshi_markets]
        batch_size = 50  # Don't fetch more than 50 at a time to avoid API rate limits
        if games_needing_markets:
            batch = games_needing_markets[:batch_size]
            print(f"[LIVE ARB] Fetching markets for {len(batch)}/{len(games_needing_markets)} games...")
            for game in batch:
                self.refresh_game_markets(game)

        # Filter to only games with markets
        games_with_markets = [g for g in matchable_games if g.kalshi_markets]
        print(f"[LIVE ARB] {len(games_with_markets)} games with active markets")

        # For each game, match to sportsbooks and find opportunities
        for game in games_with_markets:
            # Match and fetch odds if needed
            odds_age = game.odds_age_seconds
            needs_refresh = (
                game.odds_api_event_id == "" or
                odds_age is None or
                odds_age > self.config.odds_refresh_seconds
            )

            if needs_refresh:
                if self.match_to_sportsbooks(game):
                    self._notify_game_update(game)

            # Find opportunities
            opportunities = self.find_opportunities(game)
            all_opportunities.extend(opportunities)

            # Notify for each opportunity
            for opp in opportunities:
                self._notify_opportunity(opp)

        # Sort all opportunities by edge
        all_opportunities.sort(key=lambda x: x.edge, reverse=True)

        with self._lock:
            self._opportunities = all_opportunities

        return all_opportunities

    def _monitor_loop(self):
        """Background monitoring loop"""
        _reconcile_counter = 0

        while self._running:
            try:
                # Verify fills from previous cycle before scanning for new opportunities
                self.verify_pending_bets()

                # Reconcile exposure against real positions every ~10 cycles
                _reconcile_counter += 1
                if _reconcile_counter >= 10:
                    self.reconcile_exposure()
                    _reconcile_counter = 0

                opportunities = self.scan_once()

                if opportunities:
                    print(f"[LIVE ARB] Found {len(opportunities)} opportunities:")
                    for opp in opportunities[:3]:
                        print(f"           {opp.bet_side.value.upper()} {opp.kalshi_ticker[-20:]}: {opp.edge_pct:.1f}% edge")

                # Execute top opportunities (if not dry run or if auto-execute enabled)
                for opp in opportunities[:1]:  # Only execute top opportunity per cycle
                    can_bet, reason = self.check_guardrails(opp)
                    if can_bet:
                        self.execute_bet(opp)
                    else:
                        print(f"[LIVE ARB] Skipped: {reason}")

            except Exception as e:
                print(f"[LIVE ARB] Monitor error: {e}")
                import traceback
                traceback.print_exc()

            time.sleep(self.config.poll_interval_seconds)

    def start(self):
        """Start the live arbitrage monitoring system"""
        if self._running:
            return

        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        print("[LIVE ARB] Started monitoring")

    def stop(self):
        """Stop the monitoring system"""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        print("[LIVE ARB] Stopped monitoring")

    # === Status ===

    def get_status(self) -> dict:
        """Get current status for UI"""
        with self._lock:
            # Build best-edge-by-game lookup from current opportunities
            best_edges: dict[str, float] = {}
            for o in self._opportunities:
                evt = o.game.kalshi_event_ticker
                if evt not in best_edges or o.edge_pct > best_edges[evt]:
                    best_edges[evt] = o.edge_pct

            return {
                "running": self._running,
                "dry_run": self.config.dry_run,
                "live_games": len(self._live_games),
                "opportunities": len(self._opportunities),
                "total_exposure": sum(self._exposure_by_game.values()),
                "max_exposure": self.config.max_total_exposure,
                "bets_placed": len(self._bet_history),
                "min_edge": self.config.min_edge * 100,
                "games": [
                    {
                        "event_ticker": g.kalshi_event_ticker,
                        "display_name": g.display_name,
                        "sport": g.sport.value,
                        "is_live": g.is_live,
                        "markets_count": len(g.kalshi_markets),
                        "odds_age": g.odds_age_seconds,
                        "matched": g.odds_api_event_id != "",
                        "best_edge": best_edges.get(g.kalshi_event_ticker),
                    }
                    for g in self._live_games.values()
                ],
                "top_opportunities": [
                    {
                        "ticker": o.kalshi_ticker,
                        "title": o.kalshi_title,
                        "side": o.bet_side.value,
                        "edge_pct": o.edge_pct,
                        "kalshi_price": o.kalshi_price,
                        "fair_value": o.fair_value,
                        "best_book": o.best_book,
                        "is_stale": o.is_stale,
                    }
                    for o in self._opportunities[:10]
                ],
                "recent_bets": [
                    {
                        "ticker": b.opportunity.kalshi_ticker,
                        "side": b.side.value,
                        "size": b.size,
                        "price": b.price,
                        "edge_pct": b.opportunity.edge_pct,
                        "status": b.status,
                        "placed_at": b.placed_at.isoformat(),
                    }
                    for b in list(self._bet_history)[-10:]
                ],
                "api_quota": self.odds_client.requests_remaining,
            }

    def get_opportunities(self) -> List[LiveOpportunity]:
        """Get current opportunities"""
        with self._lock:
            return list(self._opportunities)

    def get_live_games(self) -> List[LiveGame]:
        """Get all tracked live games"""
        with self._lock:
            return list(self._live_games.values())


# === Factory Function ===

def create_live_arb(
    kalshi_client: KalshiClient,
    odds_api_key: str,
    dry_run: bool = True,
    min_edge: float = 0.05,
) -> LiveSportsArbitrage:
    """
    Create a configured live arbitrage system.

    Args:
        kalshi_client: Authenticated Kalshi client
        odds_api_key: The Odds API key
        dry_run: If True, don't actually place orders
        min_edge: Minimum edge threshold (default 5%)
    """
    config = LiveArbConfig(
        min_edge=min_edge,
        dry_run=dry_run,
    )
    return LiveSportsArbitrage(kalshi_client, odds_api_key, config)
