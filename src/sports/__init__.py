"""
Sports betting module for Kalshi arbitrage

Integrates with The Odds API to fetch consensus odds from major bookmakers
and compare them to Kalshi sports markets.

Supports both pre-game and LIVE in-play arbitrage detection.

API Documentation: https://the-odds-api.com/liveapi/guides/v4/
"""

from .odds_api import (
    OddsAPIClient,
    Sport,
    Market,
    Event,
    Outcome,
    ConsensusOdds,
    american_to_probability,
    american_to_decimal,
    calculate_no_vig_probability,
    KALSHI_TO_ODDS_API_SPORT,
    KALSHI_TO_ODDS_API_MARKET,
)

from .kalshi_matcher import (
    KalshiSportsMarket,
    KalshiSportsMatcher,
    ArbitrageOpportunity,
    parse_kalshi_ticker,
    match_event,
    TEAM_ABBREV,
)

from .live_arb import (
    LiveSportsArbitrage,
    LiveArbConfig,
    LiveGame,
    LiveOpportunity,
    BetRecord,
    GameState,
    BetSide,
    create_live_arb,
)

__all__ = [
    # Odds API client
    "OddsAPIClient",
    "Sport",
    "Market",
    "Event",
    "Outcome",
    "ConsensusOdds",
    "american_to_probability",
    "american_to_decimal",
    "calculate_no_vig_probability",
    "KALSHI_TO_ODDS_API_SPORT",
    "KALSHI_TO_ODDS_API_MARKET",
    # Kalshi matcher
    "KalshiSportsMarket",
    "KalshiSportsMatcher",
    "ArbitrageOpportunity",
    "parse_kalshi_ticker",
    "match_event",
    "TEAM_ABBREV",
    # Live arbitrage
    "LiveSportsArbitrage",
    "LiveArbConfig",
    "LiveGame",
    "LiveOpportunity",
    "BetRecord",
    "GameState",
    "BetSide",
    "create_live_arb",
]
