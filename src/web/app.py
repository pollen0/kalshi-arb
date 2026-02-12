"""
Unified Dashboard - Clean, minimal dark UI

Design principles:
- Dark theme with subtle contrasts
- Generous whitespace
- Minimal visual noise
- Clear data hierarchy
- Smooth micro-animations
"""

import atexit
import functools
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify, request

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.core.client import KalshiClient
from src.core.models import Position, Market, MarketType, Side
from src.core.websocket_client import KalshiWebSocket, create_websocket_client
from src.financial.futures import FuturesClient
from src.financial.fair_value import calculate_range_probability, hours_until, DEFAULT_VOLATILITY
from src.financial.fair_value_v2 import SophisticatedFairValue, calculate_range_probability_v2
from src.financial.orderbook_analyzer import OrderbookAnalyzer, get_order_recommendation
from src.financial.trading_strategy import TradingStrategy, generate_trading_plan, TradingPlan
from src.financial.auto_trader import AutoTrader, TraderConfig, create_auto_trader
from src.financial.risk_manager import RiskManager, get_risk_manager
from src.financial.options_data import OptionsDataClient, get_options_client
from src.financial.event_calendar import EventCalendar, get_event_calendar, load_fomc_dates
from src.financial.market_discovery import MarketDiscovery, MarketRollover, ExpirationSlot
from src.sports.live_arb import LiveSportsArbitrage, LiveArbConfig, create_live_arb


def create_app():
    app = Flask(__name__)

    # --- Authentication ---
    # Set DASHBOARD_SECRET env var to require auth on all mutating endpoints.
    # Without it, endpoints are open (local dev only).
    DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "").strip()

    def require_auth(f):
        """Decorator: require Bearer token on mutating (POST/PUT/DELETE) requests when DASHBOARD_SECRET is set.
        GET requests pass through so read-only endpoints sharing a route are unaffected."""
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if not DASHBOARD_SECRET:
                return f(*args, **kwargs)  # No secret configured — allow (local dev)
            if request.method == "GET":
                return f(*args, **kwargs)  # Read-only requests are unguarded
            auth = request.headers.get("Authorization", "")
            if auth == f"Bearer {DASHBOARD_SECRET}":
                return f(*args, **kwargs)
            if request.args.get("token") == DASHBOARD_SECRET:
                return f(*args, **kwargs)
            return jsonify({"error": "Unauthorized"}), 401
        return decorated

    # Thread-safe lock for state updates
    state_lock = threading.Lock()

    # Global state
    state = {
        "client": None,
        "websocket": None,       # WebSocket client for real-time orderbook
        "futures": FuturesClient(),
        "fair_value_model": None,  # Sophisticated fair value calculator
        "options_client": None,  # Options data for implied volatility
        "risk_manager": None,    # Risk manager for exposure tracking
        "event_calendar": None,  # Economic event calendar
        "market_discovery": None,  # Market discovery for finding all expiration slots
        "market_rollover": None,   # Auto-rollover when contracts expire
        "balance": 0,
        "positions": [],
        "markets": {
            "nasdaq_above": [],
            "nasdaq_range": [],
            "spx_above": [],
            "spx_range": [],
        },
        "expiration_slots": [],  # All discovered expiration slots
        "active_slots": {},      # series_prefix -> active ExpirationSlot
        "orderbooks": {},  # ticker -> Orderbook (from WebSocket or polling)
        "recommendations": {},  # ticker -> OrderRecommendation
        "trading_plan": None,  # Current trading plan
        "auto_trader": None,   # Auto-trading system
        "auto_trading_enabled": False,
        "futures_quotes": {},
        "vix_price": None,     # For sophisticated fair value
        "options_iv": {},      # Options-implied volatility by index
        "last_update": None,
        "last_futures_update": None,
        "last_options_update": None,
        "error": None,
        "websocket_connected": False,
        "trading_paused": False,
        "thread_heartbeats": {},  # thread_name -> last heartbeat timestamp
        "trading_pause_reason": "",
        # Live sports arbitrage
        "live_sports_arb": None,
        "live_sports_enabled": False,
    }

    # Orderbook analyzer
    analyzer = OrderbookAnalyzer(min_edge=0.02, max_position=50)

    def connect():
        """Initialize Kalshi client, WebSocket, and models"""
        try:
            # Support env vars (for Railway/cloud) or credentials.py (for local dev)
            KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY", "").strip()
            KALSHI_PRIVATE_KEY = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()

            # Handle escaped newlines in env var (Railway may store \n as literal)
            if KALSHI_PRIVATE_KEY and "\\n" in KALSHI_PRIVATE_KEY:
                KALSHI_PRIVATE_KEY = KALSHI_PRIVATE_KEY.replace("\\n", "\n")

            if KALSHI_API_KEY and KALSHI_PRIVATE_KEY:
                print(f"[AUTH] Using environment variables (key: ...{KALSHI_API_KEY[-4:]})")
            else:
                # Fall back to credentials.py for local dev
                try:
                    from credentials import KALSHI_API_KEY as _key, KALSHI_PRIVATE_KEY as _pk
                    KALSHI_API_KEY = _key
                    KALSHI_PRIVATE_KEY = _pk
                    print("[AUTH] Using credentials.py")
                except ImportError:
                    print("[AUTH] ERROR: No credentials found. Set KALSHI_API_KEY and KALSHI_PRIVATE_KEY env vars or create credentials.py")
                    return False

            state["client"] = KalshiClient(KALSHI_API_KEY, KALSHI_PRIVATE_KEY)

            # Retry get_balance on startup (3 attempts with 2s delay)
            balance = None
            for attempt in range(3):
                balance = state["client"].get_balance()
                if balance is not None:
                    break
                print(f"[AUTH] get_balance attempt {attempt + 1} failed, retrying in 2s...")
                time.sleep(2)
            if balance is None:
                print("[AUTH] WARNING: All get_balance attempts failed, defaulting to 0")
                balance = 0
            state["balance"] = balance

            # Initialize WebSocket for real-time orderbook (optional - falls back to polling)
            try:
                ws = create_websocket_client(KALSHI_API_KEY, KALSHI_PRIVATE_KEY)
                if ws:
                    # Set up callbacks
                    def on_orderbook_update(ticker, ob):
                        with state_lock:
                            state["orderbooks"][ticker] = ob

                    def on_connection_change(connected):
                        state["websocket_connected"] = connected
                        print(f"[WS] Connection: {'connected' if connected else 'disconnected'}")

                    ws.on_orderbook_update(on_orderbook_update)
                    ws.on_connection_change(on_connection_change)
                    state["websocket"] = ws
                    print("[WS] WebSocket client initialized")
            except Exception as ws_err:
                print(f"[WS] WebSocket not available: {ws_err}")
                state["websocket"] = None

            # Initialize sophisticated fair value model
            state["fair_value_model"] = SophisticatedFairValue(use_fat_tails=True)

            # Initialize options data client for implied volatility
            state["options_client"] = OptionsDataClient(cache_ttl=60.0)
            state["fair_value_model"].set_options_client(state["options_client"])

            # Initialize risk manager
            state["risk_manager"] = RiskManager()

            # Initialize event calendar with FOMC dates
            state["event_calendar"] = EventCalendar()
            load_fomc_dates(state["event_calendar"])
            print("[CALENDAR] Loaded economic event calendar")

            # Initialize market discovery for finding all expiration slots
            state["market_discovery"] = MarketDiscovery(state["client"])
            state["market_rollover"] = MarketRollover(state["market_discovery"])

            # Set up rollover callback to log when contracts expire
            def on_rollover(series_prefix, old_slot, new_slot):
                old_name = old_slot.display_name if old_slot else "None"
                print(f"[ROLLOVER] {series_prefix}: {old_name} -> {new_slot.display_name}")
            state["market_rollover"].on_rollover(on_rollover)

            print("[DISCOVERY] Market discovery initialized")

            # Initialize auto-trader with position-aware config
            # Shared risk params sourced from RiskConfig (persisted to disk)
            from src.core.config import get_risk_config
            _rc = get_risk_config()
            config = TraderConfig(
                dry_run=False,              # LIVE MODE - set True for paper trading
                min_edge=_rc.financial_min_edge,
                min_edge_to_keep=0.005,  # Cancel if edge < 0.5%
                position_size=_rc.financial_position_size,
                max_total_orders=20,        # Max 20 concurrent orders
                max_capital_pct=_rc.max_total_exposure,
                price_buffer_cents=1,       # Place 1c below fair value
                rebalance_threshold=0.01,   # Rebalance if fair value moves 1%
                price_adjust_threshold=3,   # Adjust if price differs by 3c
                max_position_per_market=30, # Max 30 contracts per market
                max_position_pct_per_market=0.10,  # Max 10% of balance in single market

                # Safety limits (daily loss from RiskConfig, loaded in AutoTrader.__init__)
                max_daily_loss_pct=None,    # Will be loaded from RiskConfig
                min_balance=50.0,           # Stop if below $50
                sanity_check_prices=True,   # Validate fair values

                # Error tracking
                error_window_seconds=60,
                max_errors_in_window=10,
                max_network_errors=5,
                max_rate_limit_errors=3,
                max_order_failures=8,
            )
            state["auto_trader"] = AutoTrader(state["client"], config)

            # Initialize live sports arbitrage system (optional - requires ODDS_API_KEY)
            ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
            if not ODDS_API_KEY:
                try:
                    from credentials import ODDS_API_KEY as _odds_key
                    ODDS_API_KEY = _odds_key
                except ImportError:
                    pass
            try:
                if ODDS_API_KEY and ODDS_API_KEY != "your-odds-api-key-here":
                    from src.core.config import get_risk_config
                    _rc = get_risk_config()
                    arb_config = LiveArbConfig(
                        min_edge=_rc.sports_min_edge,
                        max_bet_size=_rc.sports_position_size,
                        max_exposure_per_game=100.0,  # Max $100 per game
                        max_total_exposure=500.0,     # Dynamic — overridden by _check_portfolio_risk
                        stale_data_seconds=30,   # Reject stale odds
                        dry_run=True,            # Start in dry run mode
                        poll_interval_seconds=10.0,
                    )
                    state["live_sports_arb"] = LiveSportsArbitrage(
                        state["client"],
                        ODDS_API_KEY,
                        arb_config
                    )
                    # Auto-start scanning in background (runs in dry-run mode by default)
                    state["live_sports_arb"].start()
                    state["live_sports_enabled"] = True
                    print("[SPORTS] Live sports arbitrage system initialized and scanning (dry run mode)")
                else:
                    print("[SPORTS] ODDS_API_KEY not configured - sports arbitrage disabled")
            except Exception as sports_err:
                print(f"[SPORTS] Error initializing sports arbitrage: {sports_err}")

            return True
        except Exception as e:
            state["error"] = str(e)
            import traceback
            traceback.print_exc()
            return False

    def update_data():
        """Update all market data"""
        if not state["client"]:
            if not connect():
                return

        try:
            # Update balance and positions (lock to keep them consistent)
            bal = state["client"].get_balance()
            new_positions = state["client"].get_positions()
            with state_lock:
                if bal is not None:
                    state["balance"] = bal
                if new_positions is not None:
                    state["positions"] = new_positions

            # Update futures only if cache is empty (fast_futures_updater handles regular updates)
            if not state["futures_quotes"]:
                state["futures_quotes"] = state["futures"].get_all_settlement_prices()

            # Update VIX from cached quotes (no extra API call)
            vix_quote = state["futures_quotes"].get("vix")
            if vix_quote and vix_quote.price:
                state["vix_price"] = vix_quote.price
                fv_model = state.get("fair_value_model")
                if fv_model:
                    fv_model.vix_price = vix_quote.price

            # Check event calendar for trading pauses
            if state.get("event_calendar"):
                should_pause, reason = state["event_calendar"].should_pause_trading()
                with state_lock:
                    state["trading_paused"] = should_pause
                    state["trading_pause_reason"] = reason
                if should_pause:
                    print(f"[CALENDAR] Trading paused: {reason}")

            # Initialize risk manager daily tracking if needed
            if state.get("risk_manager"):
                rm = state["risk_manager"]
                if rm.should_reset_daily_tracking():
                    rm.reset_daily_tracking(state["balance"], state["positions"])

            # Discover all expiration slots using market discovery
            # This finds H1000, H1200, H1600 for equity; "10" for forex; EOD for treasury
            discovery = state.get("market_discovery")
            rollover = state.get("market_rollover")
            if discovery:
                try:
                    # Discover all slots (cached for 60s)
                    all_slots = discovery.discover_all_slots(days_ahead=2)
                    state["expiration_slots"] = all_slots

                    # Log discovery results periodically
                    if all_slots:
                        slot_summary = {}
                        for slot in all_slots:
                            key = slot.series_prefix
                            if key not in slot_summary:
                                slot_summary[key] = []
                            slot_summary[key].append(f"{slot.time_suffix or 'EOD'}({slot.market_count})")
                        print(f"[DISCOVERY] Found slots: {slot_summary}")
                except Exception as e:
                    print(f"[DISCOVERY] Error discovering slots: {e}")

            # Update NASDAQ markets from ALL expiration slots
            # KXNASDAQ100 = Range markets (between X and Y)
            # KXNASDAQ100U = Above/below markets (X or above)
            nq = state["futures_quotes"].get("nasdaq")
            fv_model = state.get("fair_value_model") or SophisticatedFairValue()
            if nq:
                # Get NASDAQ-100 range markets from all active slots
                range_markets = []
                try:
                    # Use get_markets_by_series_prefix which now checks all time patterns
                    ndx_markets = state["client"].get_markets_by_series_prefix("KXNASDAQ100", status="active")
                    for m in ndx_markets:
                        if m.lower_bound is not None and m.upper_bound is not None:
                            hours = hours_until(m.close_time)
                            if hours and hours > 0:
                                fv_result = fv_model.calculate(
                                    current_price=nq.price,
                                    lower_bound=m.lower_bound,
                                    upper_bound=m.upper_bound,
                                    hours_to_expiry=hours,
                                    index="NDX",
                                    close_time=m.close_time,
                                )
                                m.fair_value = fv_result.probability
                                m.fair_value_time = datetime.now(timezone.utc)
                                m.model_source = f"NQ={nq.price:.0f} [{fv_result.vol_source}]"
                                range_markets.append(m)
                except Exception as e:
                    print(f"[MARKETS] Error fetching KXNASDAQ100: {e}")
                state["markets"]["nasdaq_range"] = sorted(range_markets, key=lambda x: x.lower_bound or 0)

                # Get NASDAQ-100 above/below markets from all active slots
                above_markets = []
                try:
                    ndx_above = state["client"].get_markets_by_series_prefix("KXNASDAQ100U", status="active")
                    for m in ndx_above:
                        if m.lower_bound is not None:
                            hours = hours_until(m.close_time)
                            if hours and hours > 0:
                                # For "above" markets, upper_bound is infinity
                                fv_result = fv_model.calculate(
                                    current_price=nq.price,
                                    lower_bound=m.lower_bound,
                                    upper_bound=float('inf'),
                                    hours_to_expiry=hours,
                                    index="NDX",
                                    close_time=m.close_time,
                                )
                                m.fair_value = fv_result.probability
                                m.fair_value_time = datetime.now(timezone.utc)
                                m.model_source = f"NQ={nq.price:.0f} [{fv_result.vol_source}]"
                                above_markets.append(m)
                except Exception as e:
                    print(f"[MARKETS] Error fetching KXNASDAQ100U: {e}")
                state["markets"]["nasdaq_above"] = sorted(above_markets, key=lambda x: x.lower_bound or 0)

            # Update S&P 500 markets from ALL expiration slots
            # KXINX = Range markets (between X and Y)
            # KXINXU = Above/below markets (X or above)
            es = state["futures_quotes"].get("spx")
            if es:
                # Get S&P 500 range markets from all active slots
                range_markets = []
                try:
                    spx_markets = state["client"].get_markets_by_series_prefix("KXINX", status="active")
                    for m in spx_markets:
                        if m.lower_bound is not None and m.upper_bound is not None:
                            hours = hours_until(m.close_time)
                            if hours and hours > 0:
                                fv_result = fv_model.calculate(
                                    current_price=es.price,
                                    lower_bound=m.lower_bound,
                                    upper_bound=m.upper_bound,
                                    hours_to_expiry=hours,
                                    index="SPX",
                                    close_time=m.close_time,
                                )
                                m.fair_value = fv_result.probability
                                m.fair_value_time = datetime.now(timezone.utc)
                                m.model_source = f"ES={es.price:.0f} [{fv_result.vol_source}]"
                                range_markets.append(m)
                except Exception as e:
                    print(f"[MARKETS] Error fetching KXINX: {e}")
                state["markets"]["spx_range"] = sorted(range_markets, key=lambda x: x.lower_bound or 0)

                # Get S&P 500 above/below markets from all active slots
                above_markets = []
                try:
                    spx_above = state["client"].get_markets_by_series_prefix("KXINXU", status="active")
                    for m in spx_above:
                        if m.lower_bound is not None:
                            hours = hours_until(m.close_time)
                            if hours and hours > 0:
                                # For "above" markets, upper_bound is infinity
                                fv_result = fv_model.calculate(
                                    current_price=es.price,
                                    lower_bound=m.lower_bound,
                                    upper_bound=float('inf'),
                                    hours_to_expiry=hours,
                                    index="SPX",
                                    close_time=m.close_time,
                                )
                                m.fair_value = fv_result.probability
                                m.fair_value_time = datetime.now(timezone.utc)
                                m.model_source = f"ES={es.price:.0f} [{fv_result.vol_source}]"
                                above_markets.append(m)
                except Exception as e:
                    print(f"[MARKETS] Error fetching KXINXU: {e}")
                state["markets"]["spx_above"] = sorted(above_markets, key=lambda x: x.lower_bound or 0)

            # Update Treasury 10Y markets (KXTNOTED)
            try:
                treasury_quote = state["futures_quotes"].get("treasury10y")
                if treasury_quote and treasury_quote.price:
                    treasury_price = treasury_quote.price

                    # Get historical volatility for Treasury
                    treasury_vol = state["futures"].get_historical_volatility("treasury10y", 30)
                    if treasury_vol:
                        fv_model.historical_vols["treasury10y"] = treasury_vol

                    treasury_markets = []
                    tnote_markets = state["client"].get_markets(series_ticker="KXTNOTED", status="active")
                    for m in (tnote_markets or []):
                        hours = hours_until(m.close_time)
                        if hours and hours > 0:
                            # Treasury markets: lower_bound and upper_bound are yield values
                            if m.lower_bound is not None and m.upper_bound is not None:
                                # Range market
                                fv_result = fv_model.calculate(
                                    current_price=treasury_price,
                                    lower_bound=m.lower_bound,
                                    upper_bound=m.upper_bound,
                                    hours_to_expiry=hours,
                                    index="treasury10y",
                                    close_time=m.close_time,
                                )
                            elif m.lower_bound is not None:
                                # Above market
                                fv_result = fv_model.calculate(
                                    current_price=treasury_price,
                                    lower_bound=m.lower_bound,
                                    upper_bound=float('inf'),
                                    hours_to_expiry=hours,
                                    index="treasury10y",
                                    close_time=m.close_time,
                                )
                            elif m.upper_bound is not None:
                                # Below market
                                fv_result = fv_model.calculate(
                                    current_price=treasury_price,
                                    lower_bound=0,
                                    upper_bound=m.upper_bound,
                                    hours_to_expiry=hours,
                                    index="treasury10y",
                                    close_time=m.close_time,
                                )
                            else:
                                continue

                            m.fair_value = fv_result.probability
                            m.fair_value_time = datetime.now(timezone.utc)
                            m.model_source = f"10Y={treasury_price:.3f}%"
                            treasury_markets.append(m)

                    state["markets"]["treasury"] = sorted(treasury_markets, key=lambda x: x.lower_bound or 0)
                    if treasury_markets:
                        print(f"[MARKETS] Loaded {len(treasury_markets)} Treasury markets, yield={treasury_price:.3f}%")
            except Exception as e:
                print(f"[MARKETS] Error fetching Treasury: {e}")
                import traceback
                traceback.print_exc()

            # Update USD/JPY markets (KXUSDJPY)
            try:
                usdjpy_quote = state["futures_quotes"].get("usdjpy")
                if usdjpy_quote and usdjpy_quote.price:
                    usdjpy_price = usdjpy_quote.price

                    usdjpy_markets = []
                    jpy_markets = state["client"].get_markets(series_ticker="KXUSDJPY", status="active")
                    for m in (jpy_markets or []):
                        hours = hours_until(m.close_time)
                        if hours and hours > 0:
                            if m.lower_bound is not None and m.upper_bound is not None:
                                # Range market
                                fv_result = fv_model.calculate(
                                    current_price=usdjpy_price,
                                    lower_bound=m.lower_bound,
                                    upper_bound=m.upper_bound,
                                    hours_to_expiry=hours,
                                    index="usdjpy",
                                    close_time=m.close_time,
                                )
                                m.fair_value = fv_result.probability
                                m.fair_value_time = datetime.now(timezone.utc)
                                m.model_source = f"USDJPY={usdjpy_price:.3f}"
                                usdjpy_markets.append(m)

                    state["markets"]["usdjpy"] = sorted(usdjpy_markets, key=lambda x: x.lower_bound or 0)
                    if usdjpy_markets:
                        print(f"[MARKETS] Loaded {len(usdjpy_markets)} USD/JPY markets, price={usdjpy_price:.3f}")
            except Exception as e:
                print(f"[MARKETS] Error fetching USD/JPY: {e}")

            # Update EUR/USD markets (KXEURUSD)
            try:
                eurusd_quote = state["futures_quotes"].get("eurusd")
                if eurusd_quote and eurusd_quote.price:
                    eurusd_price = eurusd_quote.price

                    eurusd_markets = []
                    eur_markets = state["client"].get_markets(series_ticker="KXEURUSD", status="active")
                    for m in (eur_markets or []):
                        hours = hours_until(m.close_time)
                        if hours and hours > 0:
                            if m.lower_bound is not None and m.upper_bound is not None:
                                # Range market
                                fv_result = fv_model.calculate(
                                    current_price=eurusd_price,
                                    lower_bound=m.lower_bound,
                                    upper_bound=m.upper_bound,
                                    hours_to_expiry=hours,
                                    index="eurusd",
                                    close_time=m.close_time,
                                )
                                m.fair_value = fv_result.probability
                                m.fair_value_time = datetime.now(timezone.utc)
                                m.model_source = f"EURUSD={eurusd_price:.5f}"
                                eurusd_markets.append(m)

                    state["markets"]["eurusd"] = sorted(eurusd_markets, key=lambda x: x.lower_bound or 0)
                    if eurusd_markets:
                        print(f"[MARKETS] Loaded {len(eurusd_markets)} EUR/USD markets, price={eurusd_price:.5f}")
            except Exception as e:
                print(f"[MARKETS] Error fetching EUR/USD: {e}")

            # Update position prices
            for pos in state["positions"]:
                # Find matching market in all market types
                for key in ["nasdaq_above", "nasdaq_range", "spx_above", "spx_range", "treasury", "usdjpy", "eurusd"]:
                    for m in state["markets"].get(key, []):
                        if m.ticker == pos.ticker:
                            pos.current_bid = m.yes_bid / 100 if m.yes_bid else None
                            pos.current_ask = m.yes_ask / 100 if m.yes_ask else None
                            pos.fair_value = m.fair_value
                            break

            # Fetch orderbooks for markets with potential edge (top 5 by edge)
            all_markets = (
                state["markets"].get("nasdaq_above", []) +
                state["markets"].get("nasdaq_range", []) +
                state["markets"].get("spx_above", []) +
                state["markets"].get("spx_range", []) +
                state["markets"].get("treasury", []) +
                state["markets"].get("usdjpy", []) +
                state["markets"].get("eurusd", [])
            )
            markets_with_edge = sorted(
                [m for m in all_markets if m.best_edge and m.best_edge >= 0.02],
                key=lambda x: x.best_edge or 0,
                reverse=True
            )[:5]

            for m in markets_with_edge:
                try:
                    ob = state["client"].get_orderbook(m.ticker)
                    if ob:
                        state["orderbooks"][m.ticker] = ob
                        m.orderbook = ob
                        rec = analyzer.analyze(m, ob)
                        if rec:
                            state["recommendations"][m.ticker] = rec
                except Exception as e:
                    print(f"[UPDATER] Error analyzing {m.ticker}: {e}")

            state["last_update"] = datetime.now(timezone.utc)
            state["error"] = None

        except Exception as e:
            state["error"] = str(e)
            import traceback
            traceback.print_exc()

    def update_markets_fast():
        """Fast update of just market prices (not full refresh)"""
        if not state["client"]:
            return

        try:
            # Get fresh quotes for all market types
            for key in ["nasdaq_above", "nasdaq_range", "spx_above", "spx_range"]:
                markets = state["markets"].get(key, [])
                if not markets:
                    continue

                # Get unique event tickers
                event_tickers = set(m.event_ticker for m in markets if m.event_ticker)

                # Fetch fresh data from each event
                fresh_map = {}
                for event_ticker in event_tickers:
                    fresh = state["client"].get_markets(event_ticker=event_ticker)
                    if fresh is None:
                        continue  # API error — keep previous prices
                    for m in fresh:
                        fresh_map[m.ticker] = m

                # Update prices in place (thread-safe)
                with state_lock:
                    for m in markets:
                        if m.ticker in fresh_map:
                            f = fresh_map[m.ticker]
                            m.yes_bid = f.yes_bid
                            m.yes_ask = f.yes_ask
                            m.no_bid = f.no_bid
                            m.no_ask = f.no_ask
                            m.volume = f.volume
        except Exception as e:
            print(f"[FAST-MARKET] Error refreshing market prices: {e}")

    def background_updater():
        """Background thread for full data refresh (positions, new markets)"""
        while True:
            update_data()
            state["thread_heartbeats"]["background_updater"] = time.time()
            time.sleep(30)  # Full refresh every 30 seconds

    def fast_market_updater():
        """Fast background thread for market prices - every 500ms"""
        while True:
            update_markets_fast()
            state["thread_heartbeats"]["fast_market_updater"] = time.time()
            time.sleep(0.5)  # Kalshi allows 20/sec, we use ~4/sec

    def fast_futures_updater():
        """Background thread for futures prices - every 5 seconds (rate-limited)"""
        # Cache volatility (recalculate every 5 minutes)
        cached_vol = {"nasdaq": 0.20, "spx": 0.16}
        last_vol_update = datetime.now(timezone.utc)
        last_options_update = datetime.now(timezone.utc)

        while True:
            try:
                # Update quotes - use settlement prices for fair value calculation
                # get_all_settlement_prices handles overnight sessions by using
                # futures-adjusted prices when cash markets are closed
                new_quotes = state["futures"].get_all_settlement_prices()
                with state_lock:
                    state["futures_quotes"].update(new_quotes)
                    state["last_futures_update"] = datetime.now(timezone.utc)

                    # Update VIX in fair value model (from cached quotes)
                    vix_quote = new_quotes.get("vix") or state["futures_quotes"].get("vix")
                    if vix_quote and vix_quote.price:
                        state["vix_price"] = vix_quote.price
                        fv_model = state.get("fair_value_model")
                        if fv_model:
                            fv_model.vix_price = vix_quote.price

                # Recalculate fair values with fresh futures
                nq = state["futures_quotes"].get("nasdaq")
                es = state["futures_quotes"].get("spx")

                now = datetime.now(timezone.utc)

                # Update historical volatility every 5 minutes
                if (now - last_vol_update).total_seconds() > 300:
                    try:
                        fv_model = state.get("fair_value_model")
                        nq_vol = state["futures"].get_historical_volatility("nasdaq", 20)
                        if nq_vol and 0.10 < nq_vol < 0.50:  # Sanity check
                            cached_vol["nasdaq"] = nq_vol
                            if fv_model:
                                fv_model.historical_vols["nasdaq"] = nq_vol
                        es_vol = state["futures"].get_historical_volatility("spx", 20)
                        if es_vol and 0.08 < es_vol < 0.40:
                            cached_vol["spx"] = es_vol
                            if fv_model:
                                fv_model.historical_vols["spx"] = es_vol
                        last_vol_update = now
                        print(f"[FUTURES] Updated historical vol: NDX={cached_vol['nasdaq']*100:.1f}%, SPX={cached_vol['spx']*100:.1f}%")
                    except Exception as e:
                        print(f"[FUTURES] Error updating historical vol: {e}")

                # Update options-implied volatility every 60 seconds
                if (now - last_options_update).total_seconds() > 60:
                    try:
                        options_client = state.get("options_client")
                        fv_model = state.get("fair_value_model")
                        if options_client and fv_model:
                            for index in ["nasdaq", "spx"]:
                                fv_model.update_options_data(index, hours_to_expiry=8.0)
                            # Log if we got options data
                            if fv_model.options_implied_vols:
                                print(f"[OPTIONS] Updated implied vol: {fv_model.options_implied_vols}")
                        last_options_update = now
                        state["last_options_update"] = now
                    except Exception as e:
                        print(f"[OPTIONS] Error updating options data: {e}")

                fv_model = state.get("fair_value_model") or SophisticatedFairValue()
                with state_lock:
                    if nq and nq.price > 10000:  # Sanity check
                        for m in state["markets"].get("nasdaq_range", []):
                            if m.lower_bound is not None and m.upper_bound is not None:
                                hours = hours_until(m.close_time)
                                if hours and hours > 0:
                                    fv_result = fv_model.calculate(
                                        current_price=nq.price,
                                        lower_bound=m.lower_bound,
                                        upper_bound=m.upper_bound,
                                        hours_to_expiry=hours,
                                        index="NDX",
                                        close_time=m.close_time,
                                    )
                                    m.fair_value = fv_result.probability
                                    m.fair_value_time = datetime.now(timezone.utc)
                                    m.model_source = f"NQ={nq.price:.0f} [{fv_result.vol_source}]"
                    if es and es.price > 3000:  # Sanity check
                        for m in state["markets"].get("spx_range", []):
                            if m.lower_bound is not None and m.upper_bound is not None:
                                hours = hours_until(m.close_time)
                                if hours and hours > 0:
                                    fv_result = fv_model.calculate(
                                        current_price=es.price,
                                        lower_bound=m.lower_bound,
                                        upper_bound=m.upper_bound,
                                        hours_to_expiry=hours,
                                        index="SPX",
                                        close_time=m.close_time,
                                    )
                                    m.fair_value = fv_result.probability
                                    m.fair_value_time = datetime.now(timezone.utc)
                                    m.model_source = f"ES={es.price:.0f} [{fv_result.vol_source}]"
            except Exception as e:
                print(f"[FUTURES] Error: {e}")
            state["thread_heartbeats"]["fast_futures_updater"] = time.time()
            time.sleep(5)  # Update every 5 seconds (rate limited, cached for 10s)

    def orderbook_updater():
        """Background thread for orderbook updates on high-edge markets"""
        while True:
            try:
                if state["client"]:
                    all_markets = (
                        state["markets"].get("nasdaq_above", []) +
                        state["markets"].get("nasdaq_range", []) +
                        state["markets"].get("spx_above", []) +
                        state["markets"].get("spx_range", []) +
                        state["markets"].get("treasury", [])
                    )
                    markets_with_edge = sorted(
                        [m for m in all_markets if m.best_edge and m.best_edge >= 0.02],
                        key=lambda x: x.best_edge or 0,
                        reverse=True
                    )[:3]  # Top 3 only for speed

                    for m in markets_with_edge:
                        ob = state["client"].get_orderbook(m.ticker)
                        if ob:
                            with state_lock:
                                state["orderbooks"][m.ticker] = ob
                                m.orderbook = ob
                                rec = analyzer.analyze(m, ob)
                                if rec:
                                    state["recommendations"][m.ticker] = rec
            except Exception as e:
                print(f"[ORDERBOOK] Error updating orderbooks: {e}")
            state["thread_heartbeats"]["orderbook_updater"] = time.time()
            time.sleep(1)  # Update orderbooks every 1 second

    def trading_plan_updater():
        """Background thread to generate trading plan"""
        strategy = TradingStrategy(min_edge=0.02, max_total_capital=500)
        while True:
            try:
                all_markets = (
                    state["markets"].get("nasdaq_above", []) +
                    state["markets"].get("nasdaq_range", []) +
                    state["markets"].get("spx_above", []) +
                    state["markets"].get("spx_range", []) +
                    state["markets"].get("treasury", [])
                )
                if all_markets:
                    plan = strategy.generate_orders(all_markets)
                    with state_lock:
                        state["trading_plan"] = plan
            except Exception as e:
                print(f"[PLAN] Error updating trading plan: {e}")
            state["thread_heartbeats"]["trading_plan_updater"] = time.time()
            time.sleep(5)  # Update plan every 5 seconds

    def auto_trading_loop():
        """Background thread for auto-trading - runs every 1 second"""
        while True:
            try:
                if state["auto_trading_enabled"] and state["auto_trader"]:
                    # CRITICAL: Check if trading is paused (economic events, etc.)
                    if state.get("trading_paused"):
                        reason = state.get("trading_pause_reason", "Unknown")
                        # Only log occasionally to avoid spam
                        if not hasattr(auto_trading_loop, '_last_pause_log') or \
                           time.time() - auto_trading_loop._last_pause_log > 60:
                            print(f"[AUTO-TRADE] Paused: {reason}")
                            auto_trading_loop._last_pause_log = time.time()
                        time.sleep(1)
                        continue

                    all_markets = (
                        state["markets"].get("nasdaq_above", []) +
                        state["markets"].get("nasdaq_range", []) +
                        state["markets"].get("spx_above", []) +
                        state["markets"].get("spx_range", []) +
                        state["markets"].get("treasury", []) +
                        state["markets"].get("usdjpy", []) +
                        state["markets"].get("eurusd", [])
                    )
                    # Only trade if we have fresh futures data (sanity check)
                    futures_ok = False
                    nq = state["futures_quotes"].get("nasdaq")
                    es = state["futures_quotes"].get("spx")
                    t10y = state["futures_quotes"].get("treasury10y")
                    usdjpy = state["futures_quotes"].get("usdjpy")
                    eurusd = state["futures_quotes"].get("eurusd")
                    if nq and nq.price > 10000:  # NQ should be ~21000+
                        futures_ok = True
                    if es and es.price > 3000:   # ES should be ~6000+
                        futures_ok = True
                    if t10y and t10y.price > 0:  # Treasury yield should be positive
                        futures_ok = True
                    if usdjpy and usdjpy.price > 100:  # USD/JPY should be ~150+
                        futures_ok = True
                    if eurusd and eurusd.price > 0.5:  # EUR/USD should be ~1.0+
                        futures_ok = True

                    # Check thread heartbeats — halt if critical threads are dead
                    critical_threads = ["fast_futures_updater", "fast_market_updater"]
                    now_ts = time.time()
                    for thread_name in critical_threads:
                        last_hb = state["thread_heartbeats"].get(thread_name)
                        if last_hb and (now_ts - last_hb) > 60:
                            print(f"[AUTO-TRADE] HALTING: {thread_name} heartbeat stale ({now_ts - last_hb:.0f}s)")
                            state["auto_trader"]._halt(f"Thread {thread_name} dead (>{now_ts - last_hb:.0f}s)")
                            break

                    if all_markets and futures_ok:
                        state["auto_trader"].trading_loop(all_markets)
                    elif not futures_ok:
                        print("[AUTO-TRADE] Waiting for valid futures data...")
            except Exception as e:
                print(f"[AUTO-TRADE] Error: {e}")
                import traceback
                traceback.print_exc()
            time.sleep(1)  # Run every 1 second for fast reaction

    # Start all background updaters
    threading.Thread(target=background_updater, daemon=True).start()
    threading.Thread(target=fast_market_updater, daemon=True).start()
    threading.Thread(target=fast_futures_updater, daemon=True).start()
    threading.Thread(target=orderbook_updater, daemon=True).start()
    threading.Thread(target=trading_plan_updater, daemon=True).start()
    threading.Thread(target=auto_trading_loop, daemon=True).start()

    # --- Graceful shutdown: cancel all resting orders on exit ---
    def _shutdown_handler(signum=None, frame=None):
        print("\n[SHUTDOWN] Received shutdown signal — cancelling all resting orders...")
        try:
            trader = state.get("auto_trader")
            if trader:
                trader.cancel_all_orders("Process shutdown")
                print("[SHUTDOWN] All orders cancelled.")
            else:
                print("[SHUTDOWN] No auto-trader active, nothing to cancel.")
        except Exception as e:
            print(f"[SHUTDOWN] Error cancelling orders: {e}")
        if signum is not None:
            sys.exit(0)

    atexit.register(_shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    @app.route('/')
    def dashboard():
        return render_template_string(DASHBOARD_HTML)

    @app.route('/api/data')
    def api_data():
        # Format positions
        positions_data = []
        for pos in state["positions"]:
            positions_data.append({
                "ticker": pos.ticker,
                "title": pos.market_title[:50] + "..." if len(pos.market_title) > 50 else pos.market_title,
                "side": pos.side.value.upper(),
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "current_mid": pos.current_mid,
                "fair_value": pos.fair_value,
                "unrealized_pnl": pos.unrealized_pnl,
                "model_edge": pos.model_edge,
            })

        # Format markets
        def format_markets(markets, futures_price, is_yield_based=False, is_forex=False, forex_decimals=3):
            result = []
            for m in markets:
                if m.lower_bound is None:
                    continue

                # Format range (handle above/below markets where upper_bound is None or inf)
                if is_yield_based:
                    # Treasury yields: format as percentage (e.g., "4.50%+" or "4.50-4.55%")
                    if m.upper_bound is None or m.upper_bound == float('inf'):
                        range_str = f"{m.lower_bound:.2f}%+"
                    elif m.lower_bound <= 0:
                        range_str = f"<{m.upper_bound:.2f}%"
                    else:
                        range_str = f"{m.lower_bound:.2f}-{m.upper_bound:.2f}%"
                elif is_forex:
                    # Forex: format with appropriate decimals (e.g., "156.500-156.749" for JPY, "1.18200-1.18399" for EUR)
                    fmt = f"{{:.{forex_decimals}f}}"
                    if m.upper_bound is None or m.upper_bound == float('inf'):
                        range_str = fmt.format(m.lower_bound) + "+"
                    elif m.lower_bound <= 0:
                        range_str = "<" + fmt.format(m.upper_bound)
                    else:
                        range_str = fmt.format(m.lower_bound) + "-" + fmt.format(m.upper_bound)
                else:
                    # Equity indices: format as large integers
                    if m.upper_bound is None or m.upper_bound == float('inf'):
                        range_str = f"{m.lower_bound:,.0f}+"
                    elif m.lower_bound <= 0:
                        range_str = f"<{m.upper_bound:,.0f}"
                    else:
                        range_str = f"{m.lower_bound:,.0f}-{m.upper_bound:,.0f}"

                # Calculate distance from current price
                if futures_price:
                    if m.upper_bound is None or m.upper_bound == float('inf'):
                        # For above markets, use lower_bound as the reference
                        distance = m.lower_bound - futures_price
                    else:
                        mid = (m.lower_bound + m.upper_bound) / 2
                        distance = mid - futures_price
                else:
                    distance = 0

                # Get recommendation if available
                rec = state["recommendations"].get(m.ticker)

                # Calculate market making suggestion for wide spreads
                # Lower threshold (10c) to include range markets which have tighter but still tradeable spreads
                mm_suggestion = None
                yes_bid = m.yes_bid or 0
                yes_ask = m.yes_ask or 100
                spread = yes_ask - yes_bid
                if spread >= 10 and m.fair_value is not None:  # 10c+ spread = market making opportunity
                    fv = m.fair_value
                    fair_no = 1 - fv

                    # Calculate MM prices for both sides
                    # YES side: only if FV >= 8% (otherwise not worth it)
                    yes_mm_price = None
                    yes_edge = 0
                    if fv >= 0.08:
                        yes_mm_price = max(3, min(int(fv * 100 * 0.6), int(fv * 100) - 5))
                        yes_edge = fv - (yes_mm_price / 100)

                    # NO side: only if fair_no >= 8%
                    no_mm_price = None
                    no_edge = 0
                    if fair_no >= 0.08:
                        no_mm_price = max(3, min(int(fair_no * 100 * 0.6), int(fair_no * 100) - 5))
                        no_edge = fair_no - (no_mm_price / 100)

                    # Pick the better side (must have at least 5% edge for tighter spreads, 10% for wide)
                    min_edge = 0.05 if spread < 30 else 0.10
                    if yes_edge >= no_edge and yes_edge >= min_edge and yes_mm_price:
                        mm_suggestion = {"side": "YES", "price": yes_mm_price, "edge": yes_edge}
                    elif no_edge >= min_edge and no_mm_price:
                        mm_suggestion = {"side": "NO", "price": no_mm_price, "edge": no_edge}

                result.append({
                    "ticker": m.ticker,
                    "range": range_str,
                    "lower": m.lower_bound,
                    "upper": m.upper_bound if m.upper_bound != float('inf') else None,
                    "market_price": m.market_price,
                    "fair_value": m.fair_value,
                    "yes_edge": m.yes_edge,
                    "no_edge": m.no_edge,
                    "best_edge": m.best_edge,
                    "best_side": m.best_side,
                    "yes_bid": m.yes_bid,
                    "yes_ask": m.yes_ask,
                    "no_bid": m.no_bid,
                    "no_ask": m.no_ask,
                    "volume": m.volume,
                    "distance": distance,
                    "close_time": m.close_time.isoformat() if m.close_time else None,
                    "title": m.title,
                    "spread": spread,
                    "mm_suggestion": mm_suggestion,
                    "recommendation": {
                        "side": rec.side.value.upper(),
                        "price": rec.price,
                        "size": rec.size,
                        "edge": rec.edge_at_price,
                        "fill_prob": rec.fill_probability,
                        "ev": rec.expected_value,
                    } if rec else None,
                })
            return result

        nq = state["futures_quotes"].get("nasdaq")
        es = state["futures_quotes"].get("spx")
        t10y = state["futures_quotes"].get("treasury10y")
        usdjpy = state["futures_quotes"].get("usdjpy")
        eurusd = state["futures_quotes"].get("eurusd")

        return jsonify({
            "balance": state["balance"],
            "positions": positions_data,
            "nasdaq": {
                "futures": {
                    "price": nq.price if nq else None,
                    "change": nq.change if nq else None,
                    "change_pct": nq.change_pct if nq else None,
                },
                "above": format_markets(state["markets"].get("nasdaq_above", []), nq.price if nq else None),
                "range": format_markets(state["markets"].get("nasdaq_range", []), nq.price if nq else None),
            },
            "spx": {
                "futures": {
                    "price": es.price if es else None,
                    "change": es.change if es else None,
                    "change_pct": es.change_pct if es else None,
                },
                "above": format_markets(state["markets"].get("spx_above", []), es.price if es else None),
                "range": format_markets(state["markets"].get("spx_range", []), es.price if es else None),
            },
            "treasury10y": {
                "quote": {
                    "yield": t10y.price if t10y else None,
                    "change": t10y.change if t10y else None,
                    "change_pct": t10y.change_pct if t10y else None,
                },
                "markets": format_markets(state["markets"].get("treasury", []), t10y.price if t10y else None, is_yield_based=True),
            },
            "usdjpy": {
                "quote": {
                    "price": usdjpy.price if usdjpy else None,
                    "change": usdjpy.change if usdjpy else None,
                    "change_pct": usdjpy.change_pct if usdjpy else None,
                },
                "markets": format_markets(state["markets"].get("usdjpy", []), usdjpy.price if usdjpy else None, is_forex=True, forex_decimals=3),
            },
            "eurusd": {
                "quote": {
                    "price": eurusd.price if eurusd else None,
                    "change": eurusd.change if eurusd else None,
                    "change_pct": eurusd.change_pct if eurusd else None,
                },
                "markets": format_markets(state["markets"].get("eurusd", []), eurusd.price if eurusd else None, is_forex=True, forex_decimals=5),
            },
            "last_update": state["last_update"].isoformat() if state["last_update"] else None,
            "last_futures_update": state["last_futures_update"].isoformat() if state["last_futures_update"] else None,
            "error": state["error"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    @app.route('/api/refresh', methods=['POST'])
    @require_auth
    def refresh():
        update_data()
        return jsonify({"success": True})

    @app.route('/api/expiration-slots')
    def api_expiration_slots():
        """Get all discovered expiration slots across all series"""
        slots = state.get("expiration_slots", [])
        active_slots = state.get("active_slots", {})

        # Format slots for API response
        slots_data = []
        for slot in slots:
            slots_data.append({
                "series_prefix": slot.series_prefix,
                "event_ticker": slot.event_ticker,
                "date": slot.date.isoformat() if slot.date else None,
                "time_suffix": slot.time_suffix,
                "market_count": slot.market_count,
                "close_time": slot.close_time.isoformat() if slot.close_time else None,
                "hours_until_expiry": slot.hours_until_expiry,
                "is_expired": slot.is_expired,
                "is_tradeable": slot.is_tradeable,
                "display_name": slot.display_name,
            })

        # Group by series prefix for easier UI display
        by_series = {}
        for slot_data in slots_data:
            prefix = slot_data["series_prefix"]
            if prefix not in by_series:
                by_series[prefix] = []
            by_series[prefix].append(slot_data)

        return jsonify({
            "slots": slots_data,
            "by_series": by_series,
            "active_slots": {k: v.event_ticker for k, v in active_slots.items()},
            "total_markets": sum(s["market_count"] for s in slots_data),
        })

    @app.route('/api/expiration-slots/refresh', methods=['POST'])
    @require_auth
    def api_refresh_expiration_slots():
        """Force refresh of expiration slot discovery"""
        discovery = state.get("market_discovery")
        if not discovery:
            return jsonify({"error": "Market discovery not initialized"})

        try:
            slots = discovery.discover_all_slots(days_ahead=3, force_refresh=True)
            state["expiration_slots"] = slots
            return jsonify({
                "success": True,
                "slots_found": len(slots),
                "total_markets": sum(s.market_count for s in slots),
            })
        except Exception as e:
            return jsonify({"error": str(e)})

    @app.route('/api/orderbook/<ticker>')
    def api_orderbook(ticker):
        """Get orderbook and recommendation for a specific market"""
        if not state["client"]:
            return jsonify({"error": "Not connected"})

        # Find the market
        market = None
        for idx_markets in state["markets"].values():
            for m in idx_markets:
                if m.ticker == ticker:
                    market = m
                    break

        if not market:
            return jsonify({"error": "Market not found"})

        # Fetch fresh orderbook
        ob = state["client"].get_orderbook(ticker)
        if not ob:
            return jsonify({"error": "Failed to fetch orderbook"})

        state["orderbooks"][ticker] = ob
        market.orderbook = ob

        # Get recommendation
        rec = analyzer.analyze(market, ob)
        if rec:
            state["recommendations"][ticker] = rec

        # Format response
        return jsonify({
            "ticker": ticker,
            "orderbook": {
                "yes_bids": [{"price": l.price, "qty": l.quantity} for l in ob.yes_bids[:10]],
                "yes_asks": [{"price": l.price, "qty": l.quantity} for l in ob.yes_asks[:10]],
                "spread": ob.spread,
                "mid": ob.mid,
            },
            "recommendation": {
                "side": rec.side.value.upper() if rec else None,
                "price": rec.price if rec else None,
                "size": rec.size if rec else None,
                "edge": rec.edge_at_price if rec else None,
                "fill_prob": rec.fill_probability if rec else None,
                "expected_value": rec.expected_value if rec else None,
                "reasoning": rec.reasoning if rec else None,
            } if rec else None,
            "market": {
                "title": market.title,
                "subtitle": market.subtitle,
                "close_time": market.close_time.isoformat() if market.close_time else None,
                "fair_value": market.fair_value,
                "yes_edge": market.yes_edge,
                "no_edge": market.no_edge,
                "best_side": market.best_side,
            }
        })

    @app.route('/api/recommendations')
    def api_recommendations():
        """Get all current order recommendations"""
        recs = []
        for ticker, rec in state["recommendations"].items():
            # Find market
            market = None
            for idx_markets in state["markets"].values():
                for m in idx_markets:
                    if m.ticker == ticker:
                        market = m
                        break

            recs.append({
                "ticker": ticker,
                "title": market.title if market else ticker,
                "side": rec.side.value.upper(),
                "price": rec.price,
                "size": rec.size,
                "edge": rec.edge_at_price,
                "fill_prob": rec.fill_probability,
                "ev": rec.expected_value,
                "reasoning": rec.reasoning,
            })

        # Sort by expected value
        recs.sort(key=lambda x: x["ev"], reverse=True)
        return jsonify({"recommendations": recs})

    @app.route('/api/trading-plan')
    def api_trading_plan():
        """Get current trading plan with all limit orders"""
        plan = state.get("trading_plan")
        if not plan:
            return jsonify({"orders": [], "total_ev": 0, "capital": 0})

        orders = []
        for o in plan.orders[:50]:  # Limit to top 50
            # Find the market to get its title
            market_title = o.ticker
            for idx_markets in state["markets"].values():
                for m in idx_markets:
                    if m.ticker == o.ticker:
                        market_title = m.title
                        break
            orders.append({
                "ticker": o.ticker,
                "title": market_title,
                "side": o.side.upper(),
                "price": o.price,
                "size": o.size,
                "edge": o.edge,
                "fill_prob": o.fill_prob,
                "ev": o.ev,
                "reason": o.reason,
            })

        return jsonify({
            "orders": orders,
            "total_ev": plan.total_ev,
            "capital": plan.total_capital_at_risk,
            "order_count": len(plan.orders),
        })

    @app.route('/api/place-order', methods=['POST'])
    @require_auth
    def api_place_order():
        """Place a single order"""
        data = request.json

        if not state["client"]:
            return jsonify({"error": "Not connected"})

        try:
            result = state["client"].place_order(
                ticker=data["ticker"],
                side=data["side"].lower(),
                action="buy",
                count=data["size"],
                price=data["price"],
            )
            if result and result.get("error"):
                return jsonify({"error": result.get("message", "Order rejected")}), 400
            return jsonify({"success": True, "result": result})
        except Exception as e:
            return jsonify({"error": str(e)})

    @app.route('/api/place-all-orders', methods=['POST'])
    @require_auth
    def api_place_all_orders():
        """Place all orders in the current trading plan"""
        if not state["client"]:
            return jsonify({"error": "Not connected"})

        plan = state.get("trading_plan")
        if not plan or not plan.orders:
            return jsonify({"error": "No trading plan"})

        results = []
        for order in plan.orders[:20]:  # Limit to 20 orders at a time
            try:
                result = state["client"].place_order(
                    ticker=order.ticker,
                    side=order.side,
                    action="buy",
                    count=order.size,
                    price=order.price,
                )
                if result and result.get("error"):
                    results.append({"ticker": order.ticker, "error": result.get("message", "Rejected")})
                else:
                    results.append({"ticker": order.ticker, "success": True})
            except Exception as e:
                results.append({"ticker": order.ticker, "error": str(e)})

        return jsonify({
            "placed": len([r for r in results if r.get("success")]),
            "failed": len([r for r in results if r.get("error")]),
            "results": results,
        })

    @app.route('/api/futures')
    def api_futures():
        """Get just futures prices (for fast polling)"""
        nq = state["futures_quotes"].get("nasdaq")
        es = state["futures_quotes"].get("spx")
        return jsonify({
            "nasdaq": {"price": nq.price, "change": nq.change, "change_pct": nq.change_pct} if nq else None,
            "spx": {"price": es.price, "change": es.change, "change_pct": es.change_pct} if es else None,
            "last_update": state["last_futures_update"].isoformat() if state["last_futures_update"] else None,
        })

    @app.route('/api/auto-trader/status')
    def api_auto_trader_status():
        """Get auto-trader status"""
        trader = state.get("auto_trader")
        if not trader:
            return jsonify({"enabled": False, "error": "Not initialized"})

        status = trader.get_status()
        status["enabled"] = state["auto_trading_enabled"]
        status["balance"] = state["balance"]
        status["dry_run"] = trader.config.dry_run

        # Add real API order count (not just internally tracked)
        if state["client"]:
            try:
                api_orders = state["client"].get_orders(status="resting")
                status["api_order_count"] = len(api_orders)
            except Exception:
                status["api_order_count"] = status["active_orders"]

        return jsonify(status)

    @app.route('/api/auto-trader/start', methods=['POST'])
    @require_auth
    def api_auto_trader_start():
        """Start auto-trading"""
        if not state.get("auto_trader"):
            return jsonify({"error": "Auto-trader not initialized"})

        state["auto_trading_enabled"] = True
        mode = "DRY RUN" if state["auto_trader"].config.dry_run else "LIVE"
        print(f"[AUTO-TRADE] Started ({mode} MODE)")
        return jsonify({"success": True, "message": f"Auto-trading started ({mode} MODE)"})

    @app.route('/api/auto-trader/stop', methods=['POST'])
    @require_auth
    def api_auto_trader_stop():
        """Stop auto-trading"""
        state["auto_trading_enabled"] = False
        print("[AUTO-TRADE] Stopped")
        return jsonify({"success": True, "message": "Auto-trading stopped"})

    @app.route('/api/auto-trader/config', methods=['GET', 'POST'])
    @require_auth
    def api_auto_trader_config():
        """Get or update auto-trader config"""
        trader = state.get("auto_trader")
        if not trader:
            return jsonify({"error": "Not initialized"})

        if request.method == 'POST':
            data = request.json
            if "min_edge" in data:
                trader.config.min_edge = float(data["min_edge"])
            if "position_size" in data:
                trader.config.position_size = int(data["position_size"])
            if "max_orders" in data:
                trader.config.max_total_orders = int(data["max_orders"])
            if "max_capital_pct" in data:
                trader.config.max_capital_pct = float(data["max_capital_pct"])

        return jsonify({
            "min_edge": trader.config.min_edge,
            "min_edge_to_keep": trader.config.min_edge_to_keep,
            "position_size": trader.config.position_size,
            "max_total_orders": trader.config.max_total_orders,
            "max_capital_pct": trader.config.max_capital_pct,
            "price_buffer_cents": trader.config.price_buffer_cents,
            "rebalance_threshold": trader.config.rebalance_threshold,
        })

    @app.route('/api/trading-config', methods=['GET', 'POST'])
    @require_auth
    def api_trading_config():
        """Unified trading config — shared risk + strategy-specific settings.
        GET returns current config; POST updates + persists to disk."""
        from src.core.config import get_risk_config, save_config

        rc = get_risk_config()

        if request.method == 'POST':
            data = request.json or {}

            # Bounds validation — reject obviously dangerous values
            _BOUNDS = {
                "max_daily_loss_pct": (0.01, 0.20),       # 1%-20%
                "max_drawdown_pct": (0.05, 0.30),          # 5%-30%
                "max_total_exposure": (0.10, 1.0),          # 10%-100%
                "financial_min_edge": (0.005, 0.20),        # 0.5%-20%
                "sports_min_edge": (0.01, 0.30),            # 1%-30%
                "financial_position_size": (1, 200),
                "sports_position_size": (1, 200),
            }
            for key, (lo, hi) in _BOUNDS.items():
                if key in data:
                    val = float(data[key])
                    if not (lo <= val <= hi):
                        return jsonify({"error": f"{key} must be between {lo} and {hi}"}), 400

            # Update RiskConfig shared fields
            if "max_daily_loss_pct" in data:
                rc.max_daily_loss_pct = float(data["max_daily_loss_pct"])
            if "max_drawdown_pct" in data:
                rc.max_drawdown_pct = float(data["max_drawdown_pct"])
            if "max_total_exposure" in data:
                rc.max_total_exposure = float(data["max_total_exposure"])

            # Update strategy-specific fields
            if "financial_min_edge" in data:
                rc.financial_min_edge = float(data["financial_min_edge"])
            if "financial_position_size" in data:
                rc.financial_position_size = int(data["financial_position_size"])
            if "sports_min_edge" in data:
                rc.sports_min_edge = float(data["sports_min_edge"])
            if "sports_position_size" in data:
                rc.sports_position_size = int(data["sports_position_size"])

            # Propagate to live subsystems
            trader = state.get("auto_trader")
            if trader:
                trader.config.min_edge = rc.financial_min_edge
                trader.config.position_size = rc.financial_position_size
                trader.config.max_daily_loss_pct = rc.max_daily_loss_pct
                trader.config.max_capital_pct = rc.max_total_exposure
                trader._max_drawdown_pct = rc.max_drawdown_pct

            arb = state.get("live_sports_arb")
            if arb:
                arb.config.min_edge = rc.sports_min_edge
                arb.config.max_bet_size = rc.sports_position_size

            # Persist to disk
            save_config()

        return jsonify({
            "max_daily_loss_pct": rc.max_daily_loss_pct,
            "max_drawdown_pct": rc.max_drawdown_pct,
            "max_total_exposure": rc.max_total_exposure,
            "financial_min_edge": rc.financial_min_edge,
            "financial_position_size": rc.financial_position_size,
            "sports_min_edge": rc.sports_min_edge,
            "sports_position_size": rc.sports_position_size,
        })

    @app.route('/api/auto-trader/cancel-all', methods=['POST'])
    @require_auth
    def api_auto_trader_cancel_all():
        """Cancel ALL resting orders (from API, not just tracked ones)"""
        if not state["client"]:
            return jsonify({"error": "Not initialized"})

        # Use client's cancel_all which fetches from API
        result = state["client"].cancel_all_orders()

        # Also clear auto-trader's tracking
        trader = state.get("auto_trader")
        if trader:
            trader.active_orders.clear()
            trader.market_orders.clear()

        return jsonify(result)

    @app.route('/api/auto-trader/fill-stats')
    def api_fill_stats():
        """Get detailed fill probability statistics"""
        trader = state.get("auto_trader")
        if not trader:
            return jsonify({"error": "Not initialized"})

        stats = trader.tracker.get_stats()
        return jsonify({
            "total_orders": stats.total_orders,
            "filled": stats.filled,
            "cancelled": stats.cancelled,
            "expired": stats.expired,
            "pending": stats.pending,
            "fill_rate": stats.fill_rate,
            "avg_time_to_fill_seconds": stats.avg_time_to_fill_seconds,
            "avg_adverse_selection": stats.avg_adverse_selection,
            "fill_rate_by_spread": stats.fill_rate_by_spread,
            "fill_rate_by_expiry": stats.fill_rate_by_expiry,
            "fill_rate_by_volume": stats.fill_rate_by_volume,
        })

    @app.route('/api/orders')
    def api_orders():
        """Get all open orders from Kalshi (with pagination)"""
        if not state["client"]:
            return jsonify({"orders": [], "count": 0})

        orders = state["client"].get_orders(status="resting")
        return jsonify({"orders": orders, "count": len(orders)})

    @app.route('/api/orders/cancel-all', methods=['POST'])
    @require_auth
    def api_cancel_all_orders():
        """Cancel ALL resting orders on the account"""
        if not state["client"]:
            return jsonify({"error": "Not initialized"}), 400

        result = state["client"].cancel_all_orders()

        # Also clear auto-trader's internal order tracking
        trader = state.get("auto_trader")
        if trader:
            trader.active_orders.clear()
            print("[API] Cleared auto-trader order tracking")

        return jsonify(result)

    @app.route('/api/auto-trader/dry-run', methods=['POST'])
    @require_auth
    def api_auto_trader_dry_run():
        """Toggle dry run mode"""
        trader = state.get("auto_trader")
        if not trader:
            return jsonify({"error": "Not initialized"})

        data = request.json or {}
        if "enabled" in data:
            trader.config.dry_run = bool(data["enabled"])

        return jsonify({
            "dry_run": trader.config.dry_run,
            "message": "DRY RUN MODE" if trader.config.dry_run else "LIVE MODE"
        })

    @app.route('/api/auto-trader/reset-halt', methods=['POST'])
    @require_auth
    def api_auto_trader_reset_halt():
        """Reset safety halt state"""
        trader = state.get("auto_trader")
        if not trader:
            print("[API] reset-halt: trader not initialized")
            return jsonify({"error": "Auto-trader not initialized. Please wait for startup."}), 400

        try:
            prev_halted = trader._halted
            prev_reason = trader._halt_reason
            trader.reset_halt()
            print(f"[API] reset-halt: halted={prev_halted}->{trader._halted}, reason='{prev_reason}'")
            return jsonify({
                "success": True,
                "halted": trader._halted,
                "previous_reason": prev_reason
            })
        except Exception as e:
            print(f"[API] reset-halt error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route('/api/auto-trader/reset-daily', methods=['POST'])
    @require_auth
    def api_auto_trader_reset_daily():
        """Reset daily P&L tracking"""
        trader = state.get("auto_trader")
        if not trader:
            return jsonify({"error": "Not initialized"})

        trader.reset_daily_tracking()
        return jsonify({"success": True, "starting_balance": trader._starting_balance})

    @app.route('/api/positions/analysis')
    def api_positions_analysis():
        """
        Get position exit recommendations.

        Returns analysis of all positions with recommendations
        for whether to hold or exit each one.
        """
        from src.financial.position_manager import get_position_manager

        if not state["client"]:
            return jsonify({"error": "Not connected"})

        pm = get_position_manager()

        # Get real positions from Kalshi
        positions = state.get("positions", [])
        if not positions:
            return jsonify({"recommendations": [], "summary": "No positions"})

        # Build market lookup from all tracked markets
        market_map = {}
        for key in ["nasdaq_above", "nasdaq_range", "spx_above", "spx_range", "treasury"]:
            for m in state["markets"].get(key, []):
                market_map[m.ticker] = m

        # Analyze positions
        recommendations = pm.analyze_all_positions(positions, market_map)

        # Format for API response
        recs_data = []
        for rec in recommendations:
            recs_data.append({
                "ticker": rec.ticker,
                "title": rec.position.market_title if hasattr(rec.position, 'market_title') else rec.ticker,
                "action": rec.action,
                "reason": rec.reason.value,
                "urgency": rec.urgency,
                "side": rec.position.side.value.upper(),
                "quantity": rec.position.quantity,
                "entry_price": rec.entry_price,
                "current_exit_price": rec.current_exit_price,
                "fair_value": rec.current_fair_value,
                "unrealized_pnl": rec.unrealized_pnl,
                "expected_settlement_pnl": rec.expected_settlement_pnl,
                "hours_to_expiry": rec.hours_to_expiry,
                "suggested_size": rec.suggested_size,
                "suggested_price": rec.suggested_price,
                "reasoning": rec.reasoning,
            })

        # Summary stats
        exits = [r for r in recs_data if r["action"] in ["EXIT", "REDUCE"]]
        holds = [r for r in recs_data if r["action"] == "HOLD"]
        total_unrealized = sum(r["unrealized_pnl"] for r in recs_data)

        return jsonify({
            "recommendations": recs_data,
            "summary": {
                "total_positions": len(recs_data),
                "exit_recommendations": len(exits),
                "hold_recommendations": len(holds),
                "total_unrealized_pnl": total_unrealized,
            }
        })

    # =========================================================================
    # LIVE SPORTS ARBITRAGE API
    # =========================================================================

    @app.route('/api/sports/status')
    def api_sports_status():
        """Get live sports arbitrage status"""
        arb = state.get("live_sports_arb")
        if not arb:
            return jsonify({
                "enabled": False,
                "error": "Sports arbitrage not configured (missing ODDS_API_KEY)",
            })

        return jsonify(arb.get_status())

    @app.route('/api/sports/start', methods=['POST'])
    @require_auth
    def api_sports_start():
        """Start live sports arbitrage monitoring"""
        arb = state.get("live_sports_arb")
        if not arb:
            return jsonify({"error": "Sports arbitrage not configured"}), 400

        arb.start()
        state["live_sports_enabled"] = True
        return jsonify({"success": True, "running": arb._running})

    @app.route('/api/sports/stop', methods=['POST'])
    @require_auth
    def api_sports_stop():
        """Stop live sports arbitrage monitoring"""
        arb = state.get("live_sports_arb")
        if not arb:
            return jsonify({"error": "Sports arbitrage not configured"}), 400

        arb.stop()
        state["live_sports_enabled"] = False
        return jsonify({"success": True, "running": arb._running})

    @app.route('/api/sports/scan', methods=['POST'])
    @require_auth
    def api_sports_scan():
        """Run one scan cycle manually"""
        arb = state.get("live_sports_arb")
        if not arb:
            return jsonify({"error": "Sports arbitrage not configured"}), 400

        try:
            opportunities = arb.scan_once()
            games = arb.get_live_games()
            return jsonify({
                "success": True,
                "games_found": len(games),
                "opportunities_found": len(opportunities),
                "games": [
                    {
                        "event_ticker": g.kalshi_event_ticker,
                        "display_name": g.display_name,
                        "sport": g.sport.value,
                        "is_live": g.is_live,
                        "markets_count": len(g.kalshi_markets),
                        "odds_age": g.odds_age_seconds,
                        "matched": g.odds_api_event_id != "",
                    }
                    for g in games
                ],
                "opportunities": [
                    {
                        "ticker": o.kalshi_ticker,
                        "title": o.kalshi_title,
                        "side": o.bet_side.value,
                        "edge_pct": o.edge_pct,
                        "kalshi_price": o.kalshi_price,
                        "fair_value": o.fair_value,
                        "best_book": o.best_book,
                        "num_books": o.num_books,
                        "is_stale": o.is_stale,
                    }
                    for o in opportunities[:10]
                ],
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    @app.route('/api/sports/config', methods=['GET', 'POST'])
    @require_auth
    def api_sports_config():
        """Get or update sports arbitrage config"""
        arb = state.get("live_sports_arb")
        if not arb:
            return jsonify({"error": "Sports arbitrage not configured"}), 400

        if request.method == 'POST':
            data = request.json or {}

            # Update config values
            if "min_edge" in data:
                arb.config.min_edge = float(data["min_edge"])
            if "max_bet_size" in data:
                arb.config.max_bet_size = int(data["max_bet_size"])
            if "max_exposure_per_game" in data:
                arb.config.max_exposure_per_game = float(data["max_exposure_per_game"])
            if "max_total_exposure" in data:
                arb.config.max_total_exposure = float(data["max_total_exposure"])
            if "dry_run" in data:
                arb.config.dry_run = bool(data["dry_run"])
            if "stale_data_seconds" in data:
                arb.config.stale_data_seconds = int(data["stale_data_seconds"])
            if "min_books_for_consensus" in data:
                arb.config.min_books_for_consensus = int(data["min_books_for_consensus"])

        return jsonify({
            "min_edge": arb.config.min_edge,
            "min_edge_pct": arb.config.min_edge * 100,
            "max_bet_size": arb.config.max_bet_size,
            "max_exposure_per_game": arb.config.max_exposure_per_game,
            "max_total_exposure": arb.config.max_total_exposure,
            "stale_data_seconds": arb.config.stale_data_seconds,
            "min_books_for_consensus": arb.config.min_books_for_consensus,
            "dry_run": arb.config.dry_run,
        })

    @app.route('/api/sports/games')
    def api_sports_games():
        """Get all tracked live games"""
        arb = state.get("live_sports_arb")
        if not arb:
            return jsonify({"error": "Sports arbitrage not configured"}), 400

        games = arb.get_live_games()
        return jsonify({
            "games": [
                {
                    "event_ticker": g.kalshi_event_ticker,
                    "display_name": g.display_name,
                    "sport": g.sport.value,
                    "home_team": g.home_team,
                    "away_team": g.away_team,
                    "is_live": g.is_live,
                    "game_state": g.game_state.value,
                    "home_score": g.home_score,
                    "away_score": g.away_score,
                    "markets_count": len(g.kalshi_markets),
                    "odds_matched": g.odds_api_event_id != "",
                    "odds_age_seconds": g.odds_age_seconds,
                }
                for g in games
            ],
            "count": len(games),
        })

    @app.route('/api/sports/opportunities')
    def api_sports_opportunities():
        """Get current arbitrage opportunities"""
        arb = state.get("live_sports_arb")
        if not arb:
            return jsonify({"error": "Sports arbitrage not configured"}), 400

        opportunities = arb.get_opportunities()
        return jsonify({
            "opportunities": [
                {
                    "ticker": o.kalshi_ticker,
                    "title": o.kalshi_title,
                    "side": o.bet_side.value,
                    "edge": o.edge,
                    "edge_pct": o.edge_pct,
                    "kalshi_price": o.kalshi_price,
                    "fair_value": o.fair_value,
                    "best_book": o.best_book,
                    "best_odds": o.best_odds,
                    "num_books": o.num_books,
                    "is_stale": o.is_stale,
                    "confidence": o.confidence,
                    "game": o.game.display_name,
                }
                for o in opportunities
            ],
            "count": len(opportunities),
        })

    @app.route('/api/sports/bets')
    def api_sports_bets():
        """Get recent bet history"""
        arb = state.get("live_sports_arb")
        if not arb:
            return jsonify({"error": "Sports arbitrage not configured"}), 400

        return jsonify({
            "bets": [
                {
                    "ticker": b.opportunity.kalshi_ticker,
                    "game": b.opportunity.game.display_name,
                    "side": b.side.value,
                    "size": b.size,
                    "price": b.price,
                    "edge_pct": b.opportunity.edge_pct,
                    "status": b.status,
                    "order_id": b.order_id,
                    "placed_at": b.placed_at.isoformat(),
                }
                for b in list(arb._bet_history)
            ],
            "count": len(arb._bet_history),
        })

    return app


# =============================================================================
# DASHBOARD HTML - Clean, Minimal Dark UI
# =============================================================================

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <title>Kalshi Arbitrage</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-0: #09090b;
            --bg-1: #0c0c0e;
            --bg-2: #111113;
            --bg-3: #18181b;
            --bg-hover: #1f1f23;
            --border: #27272a;
            --border-light: #3f3f46;
            --text-0: #fafafa;
            --text-1: #e4e4e7;
            --text-2: #a1a1aa;
            --text-3: #71717a;
            --green: #22c55e;
            --green-dim: #166534;
            --red: #ef4444;
            --red-dim: #991b1b;
            --blue: #3b82f6;
            --purple: #a855f7;
            --orange: #f97316;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Inter', -apple-system, sans-serif;
            background: var(--bg-0);
            color: var(--text-1);
            line-height: 1.5;
            -webkit-font-smoothing: antialiased;
            min-height: 100vh;
        }

        /* Layout */
        .app {
            max-width: 1400px;
            margin: 0 auto;
            padding: 48px 24px;
        }

        /* Header */
        .header {
            margin-bottom: 48px;
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
        }

        .header h1 {
            font-size: 24px;
            font-weight: 600;
            color: var(--text-0);
            margin-bottom: 4px;
        }

        .header-sub {
            font-size: 14px;
            color: var(--text-3);
        }

        .data-age {
            font-size: 11px;
            font-weight: 600;
            padding: 6px 12px;
            border-radius: 100px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .data-age.live {
            background: var(--green-dim);
            color: var(--green);
            animation: pulse 2s infinite;
        }

        .data-age.stale {
            background: var(--red-dim);
            color: var(--red);
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }

        /* Stats Row */
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 48px;
        }

        .stat-card {
            background: var(--bg-2);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px 24px;
            transition: all 0.2s ease;
        }

        .stat-card:hover {
            border-color: var(--border-light);
        }

        .stat-label {
            font-size: 12px;
            font-weight: 500;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }

        .stat-value {
            font-size: 28px;
            font-weight: 600;
            color: var(--text-0);
        }

        .stat-value.positive { color: var(--green); }
        .stat-value.negative { color: var(--red); }

        .stat-change {
            font-size: 13px;
            color: var(--text-2);
            margin-top: 4px;
        }

        /* Tabs */
        .tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 32px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 16px;
        }

        .tab {
            padding: 10px 20px;
            font-size: 14px;
            font-weight: 500;
            color: var(--text-2);
            background: none;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.15s ease;
        }

        .tab:hover {
            color: var(--text-1);
            background: var(--bg-3);
        }

        .tab.active {
            color: var(--text-0);
            background: var(--bg-3);
        }

        /* Section */
        .section {
            display: none;
        }

        .section.active {
            display: block;
        }

        .section-title {
            font-size: 16px;
            font-weight: 600;
            color: var(--text-0);
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .section-title .badge {
            font-size: 12px;
            font-weight: 500;
            padding: 4px 10px;
            border-radius: 100px;
            background: var(--bg-3);
            color: var(--text-2);
        }

        /* Table */
        .table-container {
            background: var(--bg-1);
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
            margin-bottom: 32px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        th {
            text-align: left;
            font-size: 11px;
            font-weight: 600;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            padding: 14px 20px;
            background: var(--bg-2);
            border-bottom: 1px solid var(--border);
        }

        td {
            padding: 16px 20px;
            font-size: 14px;
            color: var(--text-1);
            border-bottom: 1px solid var(--border);
        }

        tr:last-child td {
            border-bottom: none;
        }

        tr:hover td {
            background: var(--bg-hover);
        }

        .mono {
            font-family: 'SF Mono', 'Fira Code', monospace;
            font-size: 13px;
        }

        .text-green { color: var(--green); }
        .text-red { color: var(--red); }
        .text-blue { color: var(--blue); }
        .text-muted { color: var(--text-3); }
        .text-right { text-align: right; }

        /* Price Comparison Bar */
        .price-bar {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .price-bar-track {
            flex: 1;
            height: 6px;
            background: var(--bg-3);
            border-radius: 3px;
            position: relative;
            overflow: hidden;
        }

        .price-bar-fill {
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            border-radius: 3px;
            transition: width 0.3s ease;
        }

        .price-bar-fill.market { background: var(--blue); }
        .price-bar-fill.model { background: var(--purple); opacity: 0.7; }

        .price-bar-value {
            font-size: 12px;
            font-weight: 500;
            min-width: 48px;
            text-align: right;
        }

        /* Edge Indicator */
        .edge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            font-size: 12px;
            font-weight: 600;
            padding: 4px 8px;
            border-radius: 4px;
        }

        .edge.positive {
            background: var(--green-dim);
            color: var(--green);
        }

        .edge.negative {
            background: var(--red-dim);
            color: var(--red);
        }

        /* Futures Badge */
        .futures-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 14px;
            background: var(--bg-3);
            border-radius: 8px;
            margin-bottom: 20px;
        }

        .futures-badge .price {
            font-size: 18px;
            font-weight: 600;
            color: var(--text-0);
        }

        .futures-badge .change {
            font-size: 13px;
            font-weight: 500;
        }

        /* Empty State */
        .empty {
            text-align: center;
            padding: 60px 20px;
            color: var(--text-3);
        }

        .empty-icon {
            font-size: 48px;
            margin-bottom: 16px;
            opacity: 0.3;
        }

        /* Loading */
        .loading {
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 40px;
            color: var(--text-3);
        }

        .spinner {
            width: 20px;
            height: 20px;
            border: 2px solid var(--border);
            border-top-color: var(--text-2);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin-right: 12px;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        /* Position Card */
        .position-card {
            background: var(--bg-2);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
        }

        .position-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 16px;
        }

        .position-title {
            font-size: 14px;
            font-weight: 500;
            color: var(--text-0);
            margin-bottom: 4px;
        }

        .position-ticker {
            font-size: 12px;
            color: var(--text-3);
            font-family: 'SF Mono', monospace;
        }

        .position-side {
            font-size: 12px;
            font-weight: 600;
            padding: 4px 10px;
            border-radius: 4px;
        }

        .position-side.yes {
            background: var(--green-dim);
            color: var(--green);
        }

        .position-side.no {
            background: var(--red-dim);
            color: var(--red);
        }

        .position-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
        }

        .position-metric {
            text-align: center;
        }

        .position-metric-label {
            font-size: 11px;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
        }

        .position-metric-value {
            font-size: 16px;
            font-weight: 600;
            color: var(--text-0);
        }

        /* Auto-Trader Panel */
        .auto-trader-panel {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 24px;
            background: var(--bg-2);
            border: 1px solid var(--border);
            border-radius: 12px;
            margin-bottom: 20px;
        }

        .auto-trader-status {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: var(--red);
        }

        .status-indicator.running {
            background: var(--green);
            animation: pulse 2s infinite;
        }

        .auto-trader-stats {
            display: flex;
            gap: 32px;
        }

        .auto-trader-controls {
            display: flex;
            gap: 8px;
        }

        .start-btn {
            padding: 10px 20px;
            font-size: 14px;
            font-weight: 600;
            color: var(--bg-0);
            background: var(--green);
            border: none;
            border-radius: 8px;
            cursor: pointer;
        }

        .start-btn:hover { background: #16a34a; }

        .stop-btn {
            padding: 10px 20px;
            font-size: 14px;
            font-weight: 600;
            color: var(--text-0);
            background: var(--red);
            border: none;
            border-radius: 8px;
            cursor: pointer;
        }

        .cancel-btn {
            padding: 10px 20px;
            font-size: 14px;
            font-weight: 500;
            color: var(--text-1);
            background: var(--bg-3);
            border: 1px solid var(--border);
            border-radius: 8px;
            cursor: pointer;
        }

        .cancel-btn:hover { border-color: var(--red); color: var(--red); }

        /* Config Panel */
        .config-panel {
            padding: 20px 24px;
            background: var(--bg-1);
            border: 1px solid var(--border);
            border-radius: 12px;
            margin-bottom: 24px;
        }

        .config-panel h4 {
            font-size: 12px;
            font-weight: 600;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 16px;
        }

        .config-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px;
            margin-bottom: 16px;
        }

        .config-item {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .config-item label {
            font-size: 12px;
            color: var(--text-3);
        }

        .config-item input {
            padding: 8px 12px;
            font-size: 14px;
            color: var(--text-0);
            background: var(--bg-2);
            border: 1px solid var(--border);
            border-radius: 6px;
            width: 80px;
        }

        .config-item input:focus {
            outline: none;
            border-color: var(--blue);
        }

        .save-config-btn {
            padding: 8px 16px;
            font-size: 13px;
            font-weight: 500;
            color: var(--text-1);
            background: var(--bg-3);
            border: 1px solid var(--border);
            border-radius: 6px;
            cursor: pointer;
        }

        .save-config-btn:hover { background: var(--bg-hover); }

        /* Trading Section */
        .trading-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding: 20px;
            background: var(--bg-2);
            border: 1px solid var(--border);
            border-radius: 12px;
        }

        .trading-stats {
            display: flex;
            gap: 32px;
        }

        .trading-stat {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .trading-stat .label {
            font-size: 11px;
            font-weight: 600;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .trading-stat .value {
            font-size: 20px;
            font-weight: 600;
            color: var(--text-0);
        }

        .place-all-btn {
            padding: 12px 24px;
            font-size: 14px;
            font-weight: 600;
            color: var(--bg-0);
            background: var(--green);
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.15s ease;
        }

        .place-all-btn:hover {
            background: #16a34a;
        }

        .place-all-btn:disabled {
            background: var(--text-3);
            cursor: not-allowed;
        }

        .place-btn {
            padding: 6px 12px;
            font-size: 12px;
            font-weight: 500;
            color: var(--text-0);
            background: var(--bg-3);
            border: 1px solid var(--border);
            border-radius: 4px;
            cursor: pointer;
        }

        .place-btn:hover {
            background: var(--green-dim);
            border-color: var(--green);
        }

        /* Clickable rows */
        tr.clickable {
            cursor: pointer;
        }

        tr.clickable:hover td {
            background: var(--bg-3);
        }

        /* Modal */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.8);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }

        .modal {
            background: var(--bg-1);
            border: 1px solid var(--border);
            border-radius: 16px;
            width: 90%;
            max-width: 600px;
            max-height: 80vh;
            overflow: auto;
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 24px;
            border-bottom: 1px solid var(--border);
        }

        .modal-header h3 {
            font-size: 16px;
            font-weight: 600;
            color: var(--text-0);
            font-family: 'SF Mono', monospace;
        }

        .modal-close {
            background: none;
            border: none;
            font-size: 24px;
            color: var(--text-3);
            cursor: pointer;
        }

        .modal-close:hover {
            color: var(--text-1);
        }

        .modal-body {
            padding: 24px;
        }

        .ob-section, .rec-section {
            margin-bottom: 24px;
        }

        .ob-section h4, .rec-section h4 {
            font-size: 12px;
            font-weight: 600;
            color: var(--text-3);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 12px;
        }

        .ob-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }

        .ob-header {
            font-size: 11px;
            font-weight: 600;
            color: var(--text-3);
            margin-bottom: 8px;
        }

        .ob-level {
            display: flex;
            justify-content: space-between;
            padding: 6px 10px;
            font-family: 'SF Mono', monospace;
            font-size: 13px;
            border-radius: 4px;
            margin-bottom: 4px;
        }

        .ob-level.bid {
            background: var(--green-dim);
            color: var(--green);
        }

        .ob-level.ask {
            background: var(--red-dim);
            color: var(--red);
        }

        .ob-level .qty {
            color: var(--text-3);
        }

        .ob-stats {
            margin-top: 12px;
            font-size: 13px;
            color: var(--text-2);
        }

        .rec-card {
            background: var(--bg-2);
            border: 1px solid var(--green-dim);
            border-radius: 8px;
            padding: 16px;
        }

        .rec-action {
            font-size: 18px;
            font-weight: 600;
            color: var(--green);
            margin-bottom: 8px;
        }

        .rec-stats {
            font-size: 13px;
            color: var(--text-2);
            margin-bottom: 8px;
        }

        .rec-reasoning {
            font-size: 12px;
            color: var(--text-3);
        }

        /* Responsive */
        @media (max-width: 768px) {
            .app { padding: 24px 16px; }
            .stats { grid-template-columns: 1fr 1fr; }
            .position-grid { grid-template-columns: repeat(2, 1fr); }
            th, td { padding: 12px 16px; }
        }
    </style>
</head>
<body>
    <div class="app">
        <!-- Header -->
        <header class="header">
            <div>
                <h1>Kalshi Arbitrage</h1>
                <p class="header-sub">Financial markets vs futures pricing</p>
            </div>
            <div class="data-age live" id="data-age">LIVE</div>
        </header>

        <!-- Stats -->
        <div class="stats">
            <div class="stat-card">
                <div class="stat-label">Account Balance</div>
                <div class="stat-value" id="balance">$0.00</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Open Positions</div>
                <div class="stat-value" id="position-count">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Unrealized P&L</div>
                <div class="stat-value" id="unrealized-pnl">$0.00</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Best Edge</div>
                <div class="stat-value" id="best-edge">-</div>
            </div>
        </div>

        <!-- Tabs -->
        <div class="tabs">
            <button class="tab active" data-tab="positions">Positions</button>
            <button class="tab" data-tab="trading">Trading Plan</button>
            <button class="tab" data-tab="nasdaq-above">NDX Above</button>
            <button class="tab" data-tab="nasdaq-range">NDX Range</button>
            <button class="tab" data-tab="spx-above">SPX Above</button>
            <button class="tab" data-tab="spx-range">SPX Range</button>
            <button class="tab" data-tab="treasury">10Y Treasury</button>
            <button class="tab" data-tab="usdjpy">USD/JPY</button>
            <button class="tab" data-tab="eurusd">EUR/USD</button>
            <button class="tab" data-tab="sports">Sports</button>
        </div>

        <!-- Positions Section -->
        <section class="section active" id="section-positions">
            <div id="positions-container">
                <div class="loading">
                    <div class="spinner"></div>
                    Loading positions...
                </div>
            </div>
        </section>

        <!-- Trading Plan Section -->
        <section class="section" id="section-trading">
            <div class="section-title" style="margin-bottom: 12px;">Auto-Trader</div>
            <!-- Auto-Trader Controls -->
            <div class="auto-trader-panel">
                <div class="auto-trader-status">
                    <div class="status-indicator" id="trader-status-light"></div>
                    <span id="trader-status-text">Stopped</span>
                    <span id="trader-mode-badge" style="margin-left: 12px; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;">LIVE</span>
                </div>
                <div class="auto-trader-stats">
                    <div class="trading-stat">
                        <span class="label">Active Orders</span>
                        <span class="value" id="active-order-count">0</span>
                    </div>
                    <div class="trading-stat">
                        <span class="label">Positions</span>
                        <span class="value" id="total-positions">0</span>
                    </div>
                    <div class="trading-stat">
                        <span class="label">Exposure</span>
                        <span class="value" id="total-exposure">$0</span>
                    </div>
                    <div class="trading-stat">
                        <span class="label">Balance</span>
                        <span class="value" id="trader-balance">$0</span>
                    </div>
                    <div class="trading-stat">
                        <span class="label">Daily P&L</span>
                        <span class="value" id="daily-pnl">$0</span>
                    </div>
                    <div class="trading-stat">
                        <span class="label">Errors</span>
                        <span class="value" id="error-count">0</span>
                    </div>
                </div>
                <div class="auto-trader-controls">
                    <button class="start-btn" id="start-trading-btn" onclick="startAutoTrading()">Start Trading</button>
                    <button class="stop-btn" id="stop-trading-btn" onclick="stopAutoTrading()" style="display:none">Stop</button>
                    <button class="cancel-btn" onclick="cancelAllOrders()">Cancel All</button>
                    <button id="dry-run-toggle" onclick="toggleDryRun()" style="margin-left: 8px; padding: 8px 12px; border-radius: 4px; border: none; cursor: pointer;">DRY RUN</button>
                    <button id="reset-halt-btn" onclick="resetHalt()" style="display:none; margin-left: 8px; padding: 8px 12px; border-radius: 4px; border: none; cursor: pointer; background: var(--red); color: white;">Reset Halt</button>
                </div>
                <!-- Sports stats row -->
                <div style="border-top: 1px solid var(--border); margin-top: 12px; padding-top: 12px;">
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                        <div class="status-indicator" id="sports-status-light"></div>
                        <span style="font-size: 12px; font-weight: 500;" id="sports-status-text">Not Configured</span>
                        <span id="sports-mode-badge" style="padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 600;">DRY RUN</span>
                        <span style="font-size: 11px; color: var(--text-3); margin-left: auto;">Sports</span>
                        <button id="sports-dry-toggle" onclick="toggleSportsDryRun()" style="padding: 4px 10px; border-radius: 4px; border: none; cursor: pointer; font-size: 11px;">DRY RUN</button>
                    </div>
                    <div class="auto-trader-stats">
                        <div class="trading-stat">
                            <span class="label">Live Games</span>
                            <span class="value" id="sports-game-count">0</span>
                        </div>
                        <div class="trading-stat">
                            <span class="label">Opportunities</span>
                            <span class="value" id="sports-opp-count">0</span>
                        </div>
                        <div class="trading-stat">
                            <span class="label">Bets Placed</span>
                            <span class="value" id="sports-bet-count">0</span>
                        </div>
                        <div class="trading-stat">
                            <span class="label">Exposure</span>
                            <span class="value" id="sports-exposure">$0</span>
                        </div>
                        <div class="trading-stat">
                            <span class="label">P&L</span>
                            <span class="value" id="sports-pnl">$0</span>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Fill Probability Stats -->
            <div class="fill-stats-panel" style="margin-top: 16px; padding: 16px; background: var(--bg-1); border-radius: 8px;">
                <h4 style="margin: 0 0 12px 0; font-size: 14px; color: var(--text-2);">Learned Fill Probabilities</h4>
                <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;">
                    <div class="trading-stat">
                        <span class="label">Total Orders</span>
                        <span class="value" id="fill-total">0</span>
                    </div>
                    <div class="trading-stat">
                        <span class="label">Fill Rate</span>
                        <span class="value" id="fill-rate">--</span>
                    </div>
                    <div class="trading-stat">
                        <span class="label">Avg Fill Time</span>
                        <span class="value" id="fill-time">--</span>
                    </div>
                    <div class="trading-stat">
                        <span class="label">Adverse Selection</span>
                        <span class="value" id="adverse-selection">--</span>
                    </div>
                </div>
                <div id="fill-rates-by-spread" style="margin-top: 12px; font-size: 12px; color: var(--text-2);"></div>
            </div>

            <!-- Unified Trading Config Panel -->
            <div class="config-panel">
                <h4>Trading Config</h4>
                <div style="font-size: 11px; color: var(--text-3); margin-bottom: 12px;">Shared risk — applies to financial + sports</div>
                <div class="config-grid">
                    <div class="config-item">
                        <label>Daily Loss Limit</label>
                        <input type="number" id="cfg-daily-loss" value="5" step="0.5" min="1" max="20"> %
                    </div>
                    <div class="config-item">
                        <label>Max Drawdown</label>
                        <input type="number" id="cfg-max-drawdown" value="15" step="1" min="5" max="50"> %
                    </div>
                    <div class="config-item">
                        <label>Max Exposure</label>
                        <input type="number" id="cfg-max-exposure" value="80" step="5" min="10" max="100"> %
                    </div>
                </div>
                <div style="font-size: 11px; color: var(--text-3); margin: 12px 0 8px;">Strategy-specific</div>
                <div class="config-grid">
                    <div class="config-item">
                        <label>Fin Min Edge</label>
                        <input type="number" id="cfg-fin-min-edge" value="2" step="0.5" min="0.5" max="20"> %
                    </div>
                    <div class="config-item">
                        <label>Fin Position Size</label>
                        <input type="number" id="cfg-fin-position-size" value="10" step="5" min="1" max="100">
                    </div>
                    <div class="config-item">
                        <label>Sports Min Edge</label>
                        <input type="number" id="cfg-sports-min-edge" value="5" step="0.5" min="1" max="20"> %
                    </div>
                    <div class="config-item">
                        <label>Sports Bet Size</label>
                        <input type="number" id="cfg-sports-bet-size" value="20" step="5" min="1" max="100">
                    </div>
                </div>
                <button class="save-config-btn" onclick="saveConfig()">Save Config</button>
            </div>

            <!-- Active Orders Table -->
            <h4 style="margin: 24px 0 16px; color: var(--text-2)">Active Orders</h4>
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Market</th>
                            <th>Side</th>
                            <th class="text-right">Price</th>
                            <th class="text-right">Size</th>
                            <th class="text-right">Fair Value</th>
                            <th class="text-right">Age</th>
                        </tr>
                    </thead>
                    <tbody id="active-orders-table">
                        <tr><td colspan="6" class="text-muted" style="text-align:center">No active orders</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- Sports config and controls are merged into the auto-trader panel above -->
        </section>

        <!-- NASDAQ Above Section -->
        <section class="section" id="section-nasdaq-above">
            <div class="futures-badge">
                <span style="color: var(--text-3)">NDX Index (^NDX):</span>
                <span class="price" id="nq-price-1">-</span>
                <span class="change" id="nq-change-1">-</span>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Strike</th>
                            <th class="text-right">Market (YES)</th>
                            <th class="text-right">Model (YES)</th>
                            <th class="text-right">Best Trade</th>
                            <th class="text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="nasdaq-above-table">
                        <tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- NASDAQ Range Section -->
        <section class="section" id="section-nasdaq-range">
            <div class="futures-badge">
                <span style="color: var(--text-3)">NDX Index (^NDX):</span>
                <span class="price" id="nq-price-2">-</span>
                <span class="change" id="nq-change-2">-</span>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Range</th>
                            <th class="text-right">Market (YES)</th>
                            <th class="text-right">Model (YES)</th>
                            <th class="text-right">Best Trade</th>
                            <th class="text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="nasdaq-range-table">
                        <tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- S&P Above Section -->
        <section class="section" id="section-spx-above">
            <div class="futures-badge">
                <span style="color: var(--text-3)">SPX Index (^GSPC):</span>
                <span class="price" id="es-price-1">-</span>
                <span class="change" id="es-change-1">-</span>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Strike</th>
                            <th class="text-right">Market (YES)</th>
                            <th class="text-right">Model (YES)</th>
                            <th class="text-right">Best Trade</th>
                            <th class="text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="spx-above-table">
                        <tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- S&P Range Section -->
        <section class="section" id="section-spx-range">
            <div class="futures-badge">
                <span style="color: var(--text-3)">SPX Index (^GSPC):</span>
                <span class="price" id="es-price-2">-</span>
                <span class="change" id="es-change-2">-</span>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Range</th>
                            <th class="text-right">Market (YES)</th>
                            <th class="text-right">Model (YES)</th>
                            <th class="text-right">Best Trade</th>
                            <th class="text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="spx-range-table">
                        <tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- Treasury 10Y Section -->
        <section class="section" id="section-treasury">
            <div class="futures-badge">
                <span style="color: var(--text-3)">10Y Yield:</span>
                <span class="price" id="t10y-yield">-</span>
                <span class="change" id="t10y-change">-</span>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Yield Range</th>
                            <th class="text-right">Market (YES)</th>
                            <th class="text-right">Model (YES)</th>
                            <th class="text-right">Best Trade</th>
                            <th class="text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="treasury-table">
                        <tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- USD/JPY Section -->
        <section class="section" id="section-usdjpy">
            <div class="futures-badge">
                <span style="color: var(--text-3)">USD/JPY:</span>
                <span class="price" id="usdjpy-price">-</span>
                <span class="change" id="usdjpy-change">-</span>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Price Range</th>
                            <th class="text-right">Market (YES)</th>
                            <th class="text-right">Model (YES)</th>
                            <th class="text-right">Best Trade</th>
                            <th class="text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="usdjpy-table">
                        <tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- EUR/USD Section -->
        <section class="section" id="section-eurusd">
            <div class="futures-badge">
                <span style="color: var(--text-3)">EUR/USD:</span>
                <span class="price" id="eurusd-price">-</span>
                <span class="change" id="eurusd-change">-</span>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>Price Range</th>
                            <th class="text-right">Market (YES)</th>
                            <th class="text-right">Model (YES)</th>
                            <th class="text-right">Best Trade</th>
                            <th class="text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="eurusd-table">
                        <tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- Sports Arbitrage Section -->
        <section class="section" id="section-sports">
            <!-- Read-only status bar -->
            <div style="display: flex; align-items: center; gap: 16px; padding: 10px 16px; background: var(--bg-1); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 16px; flex-wrap: wrap;">
                <div style="display: flex; align-items: center; gap: 6px;">
                    <div class="status-indicator" id="sports-status-light-ro"></div>
                    <span id="sports-status-text-ro" style="font-weight: 500;">Stopped</span>
                </div>
                <span id="sports-mode-badge-ro" style="padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;">DRY RUN</span>
                <span style="color: var(--text-3);">|</span>
                <span style="font-size: 12px; color: var(--text-2);"><span id="sports-game-count-ro">0</span> games</span>
                <span style="font-size: 12px; color: var(--text-2);"><span id="sports-opp-count-ro">0</span> opps</span>
                <span style="font-size: 12px; color: var(--text-2);"><span id="sports-bet-count-ro">0</span> bets</span>
                <span style="margin-left: auto; font-size: 11px; color: var(--text-3);">Configure in Trading Plan tab</span>
            </div>

            <!-- Live Games -->
            <div class="section-title">Live Games <span class="badge" id="games-badge">0</span></div>
            <div class="table-container" id="sports-games-container">
                <table>
                    <thead>
                        <tr>
                            <th>Sport</th>
                            <th>Game</th>
                            <th>Status</th>
                            <th>Kalshi Markets</th>
                            <th class="text-right">Best Edge</th>
                        </tr>
                    </thead>
                    <tbody id="sports-games-table">
                        <tr><td colspan="5" class="empty" style="padding: 40px; text-align: center; color: var(--text-3);">
                            <div style="font-size: 14px; font-weight: 500; margin-bottom: 8px;">No live games</div>
                            <div>Click "Scan Once" to search for live games</div>
                            <div style="font-size: 12px; margin-top: 8px;">Requires ODDS_API_KEY in credentials.py</div>
                        </td></tr>
                    </tbody>
                </table>
            </div>

            <!-- Arbitrage Opportunities -->
            <div class="section-title">Arbitrage Opportunities <span class="badge" id="opps-badge">0</span></div>
            <div class="table-container" id="sports-opps-container">
                <table>
                    <thead>
                        <tr>
                            <th>Game</th>
                            <th>Market Type</th>
                            <th class="text-right">Kalshi Price</th>
                            <th class="text-right">Fair Value</th>
                            <th class="text-right">Edge</th>
                            <th class="text-right">Rec. Size</th>
                            <th class="text-right">Action</th>
                        </tr>
                    </thead>
                    <tbody id="sports-opps-table">
                        <tr><td colspan="7" class="text-muted" style="text-align:center; padding: 24px;">No opportunities found</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- Recent Bets -->
            <div class="section-title">Recent Bets <span class="badge" id="bets-badge">0</span></div>
            <div class="table-container" id="sports-bets-container">
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Game</th>
                            <th>Side</th>
                            <th class="text-right">Price</th>
                            <th class="text-right">Size</th>
                            <th class="text-right">Edge</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody id="sports-bets-table">
                        <tr><td colspan="7" class="text-muted" style="text-align:center; padding: 24px;">No bets placed</td></tr>
                    </tbody>
                </table>
            </div>
        </section>
    </div>

    <script>
        // Tab switching
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById('section-' + tab.dataset.tab).classList.add('active');
            });
        });

        // Escape HTML entities in strings to prevent XSS
        function esc(s) {
            if (s === null || s === undefined) return '';
            const div = document.createElement('div');
            div.textContent = String(s);
            return div.innerHTML;
        }

        // Format number with commas
        function fmt(n, decimals = 2) {
            if (n === null || n === undefined) return '-';
            return n.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
        }

        // Format percent
        function pct(n) {
            if (n === null || n === undefined) return '-';
            return (n * 100).toFixed(1) + '%';
        }

        // Format edge badge with side indicator
        function edgeBadge(edge, side) {
            if (edge === null || edge === undefined || edge < 0.01) return '<span class="text-muted">-</span>';
            const cls = 'positive';
            return `<span class="edge ${cls}">${side} +${(edge * 100).toFixed(1)}%</span>`;
        }

        // Render positions
        function renderPositions(positions) {
            const container = document.getElementById('positions-container');

            if (!positions || positions.length === 0) {
                container.innerHTML = `
                    <div class="empty">
                        <div class="empty-icon" style="font-size: 14px; font-weight: 500;">--</div>
                        <p>No open positions</p>
                    </div>
                `;
                return;
            }

            container.innerHTML = positions.map(pos => `
                <div class="position-card">
                    <div class="position-header">
                        <div>
                            <div class="position-title">${esc(pos.title)}</div>
                            <div class="position-ticker">${esc(pos.ticker)}</div>
                        </div>
                        <div class="position-side ${pos.side.toLowerCase()}">${pos.side} × ${pos.quantity}</div>
                    </div>
                    <div class="position-grid">
                        <div class="position-metric">
                            <div class="position-metric-label">Entry</div>
                            <div class="position-metric-value">${pct(pos.avg_price)}</div>
                        </div>
                        <div class="position-metric">
                            <div class="position-metric-label">Market</div>
                            <div class="position-metric-value">${pct(pos.current_mid)}</div>
                        </div>
                        <div class="position-metric">
                            <div class="position-metric-label">Model</div>
                            <div class="position-metric-value">${pct(pos.fair_value)}</div>
                        </div>
                        <div class="position-metric">
                            <div class="position-metric-label">P&L</div>
                            <div class="position-metric-value ${pos.unrealized_pnl >= 0 ? 'text-green' : 'text-red'}">
                                $${fmt(pos.unrealized_pnl)}
                            </div>
                        </div>
                    </div>
                </div>
            `).join('');
        }

        // Render market table
        function renderMarkets(markets, tableId, futuresPrice, isYieldBased = false) {
            const tbody = document.getElementById(tableId);

            if (!markets || markets.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="text-muted" style="text-align:center">No markets</td></tr>';
                return;
            }

            // Sort by distance from current price
            markets.sort((a, b) => Math.abs(a.distance) - Math.abs(b.distance));

            tbody.innerHTML = markets.slice(0, 25).map(m => {
                // Near money threshold: 500 points for equities, 0.1% for yields
                const nearMoneyThreshold = isYieldBased ? 0.1 : 500;
                const isNearMoney = Math.abs(m.distance) < nearMoneyThreshold;
                const hasEdge = m.best_edge && m.best_edge >= 0.02;
                const hasRec = m.recommendation;

                // Show recommendation if available, market making suggestion, or bid/ask
                let actionText = '';
                const hasMM = m.mm_suggestion;  // Market making opportunity

                if (hasRec) {
                    const r = m.recommendation;
                    actionText = `<span class="text-green">${r.side} ${r.size}x @ ${r.price}c</span>`;
                } else if (hasMM) {
                    // Market making suggestion (wide spread liquidity provision)
                    const mm = m.mm_suggestion;
                    const edgePct = Math.round(mm.edge * 100);
                    actionText = `<span class="text-blue" title="Market Making: ${edgePct}% edge if filled">MM: ${mm.side} @ ${mm.price}c</span>`;
                } else if (hasEdge) {
                    // Calculate fair price and suggest a limit order with edge
                    const fairYes = Math.round(m.fair_value * 100);
                    const fairNo = 100 - fairYes;

                    if (m.best_side === 'YES' && fairYes > 2) {
                        // Bid for YES: aggressive near ask when FV > ask, else passive near bid
                        const yesAsk = m.yes_ask || 0;
                        let bidPrice;
                        if (yesAsk > 0 && fairYes > yesAsk) {
                            bidPrice = Math.max(2, yesAsk - 1);
                        } else {
                            bidPrice = Math.max(2, Math.min(fairYes - 1, (m.yes_bid || 0) + 1));
                        }
                        actionText = `<span class="text-green">Bid YES @ ${bidPrice}c</span>`;
                    } else if (m.best_side === 'NO' && fairNo > 2) {
                        // Bid for NO: aggressive near ask when FV > ask, else passive near bid
                        const noAsk = m.yes_bid ? (100 - m.yes_bid) : 0;
                        const noBid = m.yes_ask ? (100 - m.yes_ask) : 0;
                        let bidPrice;
                        if (noAsk > 0 && fairNo > noAsk) {
                            bidPrice = Math.max(2, noAsk - 1);
                        } else {
                            bidPrice = Math.max(2, Math.min(fairNo - 1, (noBid || 0) + 1));
                        }
                        actionText = `<span class="text-green">Bid NO @ ${bidPrice}c</span>`;
                    } else {
                        actionText = `${m.yes_bid}c / ${m.yes_ask}c`;
                    }
                } else {
                    actionText = `${m.yes_bid}c / ${m.yes_ask}c`;
                }

                // Format close time for display
                let expiryStr = '';
                if (m.close_time) {
                    const closeDate = new Date(m.close_time);
                    const now = new Date();
                    const diffMs = closeDate - now;
                    const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
                    const diffMins = Math.floor((diffMs % (1000 * 60 * 60)) / (1000 * 60));
                    if (diffHours < 24) {
                        expiryStr = diffHours > 0 ? `${diffHours}h ${diffMins}m` : `${diffMins}m`;
                    } else {
                        expiryStr = closeDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
                    }
                }

                // Truncate title for display
                const shortTitle = m.title && m.title.length > 60 ? m.title.slice(0, 57) + '...' : (m.title || m.ticker);

                return `
                    <tr style="${isNearMoney ? 'background: var(--bg-2)' : ''}"
                        onclick="showOrderbook('${m.ticker}')"
                        class="${hasEdge ? 'clickable' : ''}"
                        title="${esc(m.title || m.ticker)}">
                        <td>
                            <span class="mono">${esc(m.range)}</span>
                            <div style="font-size: 10px; color: var(--text-3); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                                ${esc(shortTitle)}${expiryStr ? ' · ' + expiryStr : ''}
                            </div>
                        </td>
                        <td class="text-right mono">${pct(m.market_price)}</td>
                        <td class="text-right mono">${pct(m.fair_value)}</td>
                        <td class="text-right">${edgeBadge(m.best_edge, m.best_side)}</td>
                        <td class="text-right mono">${actionText}</td>
                    </tr>
                `;
            }).join('');
        }

        // Show orderbook modal
        async function showOrderbook(ticker) {
            try {
                const resp = await fetch('/api/orderbook/' + ticker);
                const data = await resp.json();
                if (data.error) {
                    console.error(data.error);
                    return;
                }

                // Format title and close time
                const marketTitle = data.market?.title || ticker;
                const closeTime = data.market?.close_time ? new Date(data.market.close_time).toLocaleString() : '';

                // Build modal content
                let html = `<div class="modal-overlay" onclick="closeModal()">
                    <div class="modal" onclick="event.stopPropagation()">
                        <div class="modal-header">
                            <h3 style="font-size: 14px; line-height: 1.3;">${marketTitle}</h3>
                            <button onclick="closeModal()" class="modal-close">&times;</button>
                        </div>
                        ${closeTime ? `<div style="padding: 0 16px; font-size: 11px; color: var(--text-3);">Expires: ${closeTime}</div>` : ''}
                        <div class="modal-body">
                            <div class="ob-section">
                                <h4>Orderbook</h4>
                                <div class="ob-grid">
                                    <div class="ob-side">
                                        <div class="ob-header">BIDS (YES)</div>
                                        ${data.orderbook.yes_bids.map(l =>
                                            `<div class="ob-level bid">${l.price}c <span class="qty">${l.qty}</span></div>`
                                        ).join('')}
                                    </div>
                                    <div class="ob-side">
                                        <div class="ob-header">ASKS (YES)</div>
                                        ${data.orderbook.yes_asks.map(l =>
                                            `<div class="ob-level ask">${l.price}c <span class="qty">${l.qty}</span></div>`
                                        ).join('')}
                                    </div>
                                </div>
                                <div class="ob-stats">
                                    Spread: ${data.orderbook.spread}c | Mid: ${fmt(data.orderbook.mid, 1)}c
                                </div>
                            </div>`;

                if (data.recommendation) {
                    const r = data.recommendation;
                    html += `
                            <div class="rec-section">
                                <h4>Recommendation</h4>
                                <div class="rec-card">
                                    <div class="rec-action">Buy ${r.size} ${r.side} @ ${r.price}c</div>
                                    <div class="rec-stats">
                                        Edge: ${(r.edge * 100).toFixed(1)}% |
                                        Fill Prob: ${(r.fill_prob * 100).toFixed(0)}% |
                                        EV: $${r.expected_value.toFixed(2)}
                                    </div>
                                    <div class="rec-reasoning">${r.reasoning}</div>
                                </div>
                            </div>`;
                }

                html += `
                        </div>
                    </div>
                </div>`;

                document.body.insertAdjacentHTML('beforeend', html);
            } catch (err) {
                console.error('Failed to load orderbook:', err);
            }
        }

        function closeModal() {
            const modal = document.querySelector('.modal-overlay');
            if (modal) modal.remove();
        }

        // Auto-trader functions
        async function loadAutoTraderStatus() {
            try {
                const resp = await fetch('/api/auto-trader/status');
                const data = await resp.json();

                const light = document.getElementById('trader-status-light');
                const text = document.getElementById('trader-status-text');
                const startBtn = document.getElementById('start-trading-btn');
                const stopBtn = document.getElementById('stop-trading-btn');
                const modeBadge = document.getElementById('trader-mode-badge');
                const resetHaltBtn = document.getElementById('reset-halt-btn');
                const dryRunBtn = document.getElementById('dry-run-toggle');

                // Update mode badge
                if (data.dry_run) {
                    modeBadge.textContent = 'DRY RUN';
                    modeBadge.style.background = 'var(--yellow)';
                    modeBadge.style.color = '#000';
                    dryRunBtn.textContent = 'GO LIVE';
                    dryRunBtn.style.background = 'var(--green)';
                    dryRunBtn.style.color = 'white';
                } else {
                    modeBadge.textContent = 'LIVE';
                    modeBadge.style.background = 'var(--green)';
                    modeBadge.style.color = 'white';
                    dryRunBtn.textContent = 'DRY RUN';
                    dryRunBtn.style.background = 'var(--bg-2)';
                    dryRunBtn.style.color = 'var(--text-1)';
                }

                // Update status
                if (data.halted) {
                    light.classList.remove('running');
                    light.style.background = 'var(--red)';
                    text.textContent = `HALTED: ${data.halt_reason || 'Unknown'}`;
                    text.style.color = 'var(--red)';
                    startBtn.style.display = 'none';
                    stopBtn.style.display = 'none';
                    resetHaltBtn.style.display = 'inline-block';
                } else if (data.enabled) {
                    light.classList.add('running');
                    light.style.background = '';
                    text.textContent = `Running (${data.active_orders} orders)`;
                    text.style.color = '';
                    startBtn.style.display = 'none';
                    stopBtn.style.display = 'block';
                    resetHaltBtn.style.display = 'none';
                } else {
                    light.classList.remove('running');
                    light.style.background = '';
                    text.textContent = 'Stopped';
                    text.style.color = '';
                    startBtn.style.display = 'block';
                    stopBtn.style.display = 'none';
                    resetHaltBtn.style.display = 'none';
                }

                // Update stats (use API order count if available, shows real Kalshi orders)
                const orderCount = data.api_order_count !== undefined ? data.api_order_count : (data.active_orders || 0);
                document.getElementById('active-order-count').textContent = orderCount;
                document.getElementById('total-positions').textContent = data.total_positions || 0;
                document.getElementById('total-exposure').textContent = '$' + fmt(data.total_exposure || 0);
                document.getElementById('trader-balance').textContent = '$' + fmt(data.balance || 0);

                // Update daily P&L with color
                const dailyPnl = data.daily_pnl || 0;
                const dailyPnlPct = data.daily_pnl_pct || 0;
                const pnlEl = document.getElementById('daily-pnl');
                pnlEl.textContent = `$${fmt(dailyPnl)} (${dailyPnlPct >= 0 ? '+' : ''}${dailyPnlPct.toFixed(1)}%)`;
                pnlEl.style.color = dailyPnl >= 0 ? 'var(--green)' : 'var(--red)';

                // Update error count
                const errors = data.errors || {};
                const errorCount = errors.total_in_window || 0;
                const errorEl = document.getElementById('error-count');
                errorEl.textContent = errorCount;
                errorEl.style.color = errorCount > 0 ? 'var(--yellow)' : '';

                // Render active orders
                const tbody = document.getElementById('active-orders-table');
                if (!data.orders || data.orders.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="text-muted" style="text-align:center">No active orders</td></tr>';
                } else {
                    tbody.innerHTML = data.orders.map(o => {
                        const shortTitle = o.title && o.title.length > 50 ? o.title.slice(0, 47) + '...' : (o.title || o.ticker);
                        return `
                        <tr title="${o.title || o.ticker}">
                            <td>
                                <div class="mono" style="font-size: 10px; color: var(--text-3);">${o.ticker.substring(0, 25)}...</div>
                                <div style="font-size: 11px; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${shortTitle}</div>
                            </td>
                            <td><span class="edge positive">${o.side.toUpperCase()}</span></td>
                            <td class="text-right mono">${o.price}c</td>
                            <td class="text-right">${o.size}</td>
                            <td class="text-right">${(o.fair_value * 100).toFixed(1)}%</td>
                            <td class="text-right text-muted">${Math.round(o.age_seconds)}s</td>
                        </tr>
                    `}).join('');
                }

                // Update fill stats
                if (data.fill_stats) {
                    const fs = data.fill_stats;
                    document.getElementById('fill-total').textContent = fs.total_orders || 0;
                    document.getElementById('fill-rate').textContent = fs.total_orders > 0
                        ? (fs.fill_rate * 100).toFixed(1) + '%'
                        : '--';
                    document.getElementById('fill-time').textContent = fs.avg_time_to_fill > 0
                        ? Math.round(fs.avg_time_to_fill) + 's'
                        : '--';
                    document.getElementById('adverse-selection').textContent = fs.total_orders > 0
                        ? (fs.avg_adverse_selection * 100).toFixed(2) + '%'
                        : '--';

                    // Display learned fill rates by spread
                    const spreadRates = fs.fill_rate_by_spread || {};
                    const spreadEntries = Object.entries(spreadRates).sort((a, b) => parseInt(a[0]) - parseInt(b[0]));
                    if (spreadEntries.length > 0 && fs.total_orders >= 10) {
                        const ratesHtml = spreadEntries.map(([spread, rate]) =>
                            `Spread ≤${spread}c: <strong>${(rate * 100).toFixed(0)}%</strong>`
                        ).join(' | ');
                        document.getElementById('fill-rates-by-spread').innerHTML =
                            '<span style="color: var(--text-3);">Learned rates:</span> ' + ratesHtml;
                    } else {
                        document.getElementById('fill-rates-by-spread').innerHTML =
                            '<span style="color: var(--text-3);">Need 10+ orders to calculate learned rates</span>';
                    }
                }
            } catch (err) {
                console.error('Failed to load auto-trader status:', err);
            }
        }

        async function startAutoTrading() {
            if (!confirm('Start auto-trading? This will automatically place limit orders.')) return;
            try {
                await fetch('/api/auto-trader/start', {method: 'POST'});
                loadAutoTraderStatus();
            } catch (err) {
                alert('Failed to start: ' + err);
            }
        }

        async function stopAutoTrading() {
            try {
                await fetch('/api/auto-trader/stop', {method: 'POST'});
                loadAutoTraderStatus();
            } catch (err) {
                alert('Failed to stop: ' + err);
            }
        }

        async function cancelAllOrders() {
            if (!confirm('Cancel all active orders?')) return;
            try {
                const resp = await fetch('/api/auto-trader/cancel-all', {method: 'POST'});
                const data = await resp.json();
                alert(`Cancelled ${data.cancelled} orders`);
                loadAutoTraderStatus();
            } catch (err) {
                alert('Failed to cancel: ' + err);
            }
        }

        async function toggleDryRun() {
            try {
                // Get current state first
                const statusResp = await fetch('/api/auto-trader/status');
                const status = await statusResp.json();
                const currentDryRun = status.dry_run;

                // If going from dry run to live, confirm
                if (currentDryRun) {
                    if (!confirm('Switch to LIVE MODE? Real orders will be placed!')) return;
                }

                const resp = await fetch('/api/auto-trader/dry-run', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({enabled: !currentDryRun})
                });
                const data = await resp.json();
                alert(data.message);
                loadAutoTraderStatus();
            } catch (err) {
                alert('Failed to toggle mode: ' + err);
            }
        }

        async function resetHalt() {
            if (!confirm('Reset safety halt? Make sure you understand why it halted.')) return;
            try {
                const resp = await fetch('/api/auto-trader/reset-halt', {method: 'POST'});
                const data = await resp.json();
                if (data.error) {
                    alert('Failed to reset halt: ' + data.error);
                    return;
                }
                if (data.success) {
                    console.log('Halt reset successfully. Previous reason:', data.previous_reason);
                }
                loadAutoTraderStatus();
            } catch (err) {
                alert('Failed to reset: ' + err);
            }
        }

        async function saveConfig() {
            try {
                const config = {
                    max_daily_loss_pct: parseFloat(document.getElementById('cfg-daily-loss').value) / 100,
                    max_drawdown_pct: parseFloat(document.getElementById('cfg-max-drawdown').value) / 100,
                    max_total_exposure: parseFloat(document.getElementById('cfg-max-exposure').value) / 100,
                    financial_min_edge: parseFloat(document.getElementById('cfg-fin-min-edge').value) / 100,
                    financial_position_size: parseInt(document.getElementById('cfg-fin-position-size').value),
                    sports_min_edge: parseFloat(document.getElementById('cfg-sports-min-edge').value) / 100,
                    sports_position_size: parseInt(document.getElementById('cfg-sports-bet-size').value),
                };
                const resp = await fetch('/api/trading-config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(config)
                });
                const data = await resp.json();
                if (data.error) {
                    alert('Error: ' + data.error);
                } else {
                    alert('Config saved');
                }
            } catch (err) {
                alert('Failed to save: ' + err);
            }
        }

        async function loadConfig() {
            try {
                const resp = await fetch('/api/trading-config');
                const data = await resp.json();
                document.getElementById('cfg-daily-loss').value = (data.max_daily_loss_pct * 100).toFixed(1);
                document.getElementById('cfg-max-drawdown').value = (data.max_drawdown_pct * 100).toFixed(0);
                document.getElementById('cfg-max-exposure').value = (data.max_total_exposure * 100).toFixed(0);
                document.getElementById('cfg-fin-min-edge').value = (data.financial_min_edge * 100).toFixed(1);
                document.getElementById('cfg-fin-position-size').value = data.financial_position_size;
                document.getElementById('cfg-sports-min-edge').value = (data.sports_min_edge * 100).toFixed(1);
                document.getElementById('cfg-sports-bet-size').value = data.sports_position_size;
            } catch (err) {}
        }

        // Load trading plan
        async function loadTradingPlan() {
            try {
                const resp = await fetch('/api/trading-plan');
                const data = await resp.json();

                document.getElementById('plan-order-count').textContent = data.order_count || 0;
                document.getElementById('plan-capital').textContent = '$' + fmt(data.capital || 0);
                document.getElementById('plan-ev').textContent = '$' + fmt(data.total_ev || 0);

                const tbody = document.getElementById('trading-table');
                if (!data.orders || data.orders.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center">No orders in plan</td></tr>';
                    return;
                }

                tbody.innerHTML = data.orders.map(o => {
                    const shortTitle = o.title && o.title.length > 45 ? o.title.slice(0, 42) + '...' : (o.title || o.ticker);
                    return `
                    <tr title="${o.title || o.ticker}">
                        <td>
                            <div style="font-size: 11px; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${shortTitle}</div>
                        </td>
                        <td><span class="edge positive">${o.side}</span></td>
                        <td class="text-right mono">${o.price}c</td>
                        <td class="text-right">${o.size}</td>
                        <td class="text-right text-green">${(o.edge * 100).toFixed(1)}%</td>
                        <td class="text-right">${(o.fill_prob * 100).toFixed(0)}%</td>
                        <td class="text-right text-green">$${o.ev.toFixed(2)}</td>
                        <td>
                            <button class="place-btn" onclick="placeOrder('${o.ticker}', '${o.side}', ${o.price}, ${o.size})">
                                Place
                            </button>
                        </td>
                    </tr>
                `}).join('');
            } catch (err) {
                console.error('Failed to load trading plan:', err);
            }
        }

        // Place single order
        async function placeOrder(ticker, side, price, size) {
            if (!confirm(`Place order: Buy ${size}x ${side} @ ${price}c?`)) return;

            try {
                const resp = await fetch('/api/place-order', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ticker, side, price, size})
                });
                const data = await resp.json();
                if (data.error) {
                    alert('Error: ' + data.error);
                } else {
                    alert('Order placed!');
                    loadTradingPlan();
                }
            } catch (err) {
                alert('Failed to place order: ' + err);
            }
        }

        // Place all orders
        async function placeAllOrders() {
            if (!confirm('Place ALL orders in the trading plan? This will submit multiple limit orders.')) return;

            const btn = document.querySelector('.place-all-btn');
            btn.disabled = true;
            btn.textContent = 'Placing...';

            try {
                const resp = await fetch('/api/place-all-orders', {method: 'POST'});
                const data = await resp.json();
                if (data.error) {
                    alert('Error: ' + data.error);
                } else {
                    alert(`Placed ${data.placed} orders, ${data.failed} failed`);
                    loadTradingPlan();
                    loadData();  // Refresh positions
                }
            } catch (err) {
                alert('Failed to place orders: ' + err);
            }

            btn.disabled = false;
            btn.textContent = 'Place All Orders';
        }

        // Track data freshness
        let lastDataTime = Date.now();

        // Fetch and render data
        async function loadData() {
            try {
                const resp = await fetch('/api/data');
                const data = await resp.json();
                lastDataTime = Date.now();

                // Update stats
                document.getElementById('balance').textContent = '$' + fmt(data.balance);
                document.getElementById('position-count').textContent = data.positions.length;

                // Calculate total unrealized P&L
                const totalPnl = data.positions.reduce((sum, p) => sum + (p.unrealized_pnl || 0), 0);
                const pnlEl = document.getElementById('unrealized-pnl');
                pnlEl.textContent = '$' + fmt(totalPnl);
                pnlEl.className = 'stat-value ' + (totalPnl >= 0 ? 'positive' : 'negative');

                // Find best edge across all markets
                let bestEdge = 0;
                let bestSide = '';
                [
                    ...(data.nasdaq?.above || []),
                    ...(data.nasdaq?.range || []),
                    ...(data.spx?.above || []),
                    ...(data.spx?.range || [])
                ].forEach(m => {
                    if (m.best_edge && m.best_edge > bestEdge) {
                        bestEdge = m.best_edge;
                        bestSide = m.best_side;
                    }
                });
                const bestEdgeEl = document.getElementById('best-edge');
                if (bestEdge >= 0.01) {
                    bestEdgeEl.textContent = bestSide + ' +' + (bestEdge * 100).toFixed(1) + '%';
                    bestEdgeEl.className = 'stat-value positive';
                } else {
                    bestEdgeEl.textContent = '-';
                    bestEdgeEl.className = 'stat-value';
                }

                // Render positions
                renderPositions(data.positions);

                // Render NASDAQ futures prices
                if (data.nasdaq?.futures?.price) {
                    ['nq-price-1', 'nq-price-2'].forEach(id => {
                        document.getElementById(id).textContent = fmt(data.nasdaq.futures.price, 2);
                    });
                    const change = data.nasdaq.futures.change_pct || 0;
                    ['nq-change-1', 'nq-change-2'].forEach(id => {
                        const el = document.getElementById(id);
                        el.textContent = (change >= 0 ? '+' : '') + change.toFixed(2) + '%';
                        el.className = 'change ' + (change >= 0 ? 'text-green' : 'text-red');
                    });
                }
                renderMarkets(data.nasdaq?.above, 'nasdaq-above-table', data.nasdaq?.futures?.price);
                renderMarkets(data.nasdaq?.range, 'nasdaq-range-table', data.nasdaq?.futures?.price);

                // Render S&P futures prices
                if (data.spx?.futures?.price) {
                    ['es-price-1', 'es-price-2'].forEach(id => {
                        document.getElementById(id).textContent = fmt(data.spx.futures.price, 2);
                    });
                    const change = data.spx.futures.change_pct || 0;
                    ['es-change-1', 'es-change-2'].forEach(id => {
                        const el = document.getElementById(id);
                        el.textContent = (change >= 0 ? '+' : '') + change.toFixed(2) + '%';
                        el.className = 'change ' + (change >= 0 ? 'text-green' : 'text-red');
                    });
                }
                renderMarkets(data.spx?.above, 'spx-above-table', data.spx?.futures?.price);
                renderMarkets(data.spx?.range, 'spx-range-table', data.spx?.futures?.price);

                // Render Treasury 10Y
                if (data.treasury10y?.quote?.yield) {
                    const yieldEl = document.getElementById('t10y-yield');
                    yieldEl.textContent = data.treasury10y.quote.yield.toFixed(3) + '%';
                    const change = data.treasury10y.quote.change || 0;
                    const changeEl = document.getElementById('t10y-change');
                    changeEl.textContent = (change >= 0 ? '+' : '') + change.toFixed(3) + '%';
                    changeEl.className = 'change ' + (change >= 0 ? 'text-red' : 'text-green');  // Inverted for yields
                }
                renderMarkets(data.treasury10y?.markets, 'treasury-table', data.treasury10y?.quote?.yield, true);

                // Render USD/JPY
                if (data.usdjpy?.quote?.price) {
                    const priceEl = document.getElementById('usdjpy-price');
                    priceEl.textContent = data.usdjpy.quote.price.toFixed(3);
                    const change = data.usdjpy.quote.change || 0;
                    const changeEl = document.getElementById('usdjpy-change');
                    changeEl.textContent = (change >= 0 ? '+' : '') + change.toFixed(3);
                    changeEl.className = 'change ' + (change >= 0 ? 'text-green' : 'text-red');
                }
                renderMarkets(data.usdjpy?.markets, 'usdjpy-table', data.usdjpy?.quote?.price);

                // Render EUR/USD
                if (data.eurusd?.quote?.price) {
                    const priceEl = document.getElementById('eurusd-price');
                    priceEl.textContent = data.eurusd.quote.price.toFixed(5);
                    const change = data.eurusd.quote.change || 0;
                    const changeEl = document.getElementById('eurusd-change');
                    changeEl.textContent = (change >= 0 ? '+' : '') + change.toFixed(5);
                    changeEl.className = 'change ' + (change >= 0 ? 'text-green' : 'text-red');
                }
                renderMarkets(data.eurusd?.markets, 'eurusd-table', data.eurusd?.quote?.price);

            } catch (err) {
                console.error('Failed to load data:', err);
            }
        }

        // Initial load and fast refresh
        loadData();
        loadTradingPlan();
        loadAutoTraderStatus();
        loadConfig();
        loadSportsStatus();
        setInterval(loadData, 1000);  // Market data every 1 second
        setInterval(loadTradingPlan, 5000);  // Trading plan every 5 seconds
        setInterval(loadAutoTraderStatus, 2000);  // Auto-trader status every 2 seconds

        // Show data freshness indicator
        function updateFreshness() {
            const age = Math.floor((Date.now() - lastDataTime) / 1000);
            const el = document.getElementById('data-age');
            if (el) {
                if (age < 3) {
                    el.textContent = 'LIVE';
                    el.className = 'data-age live';
                } else {
                    el.textContent = age + 's ago';
                    el.className = 'data-age stale';
                }
            }
        }
        setInterval(updateFreshness, 500);

        // ================== SPORTS ARBITRAGE ==================

        let sportsData = {
            running: false,
            configured: false,
            configLoaded: false,
            dry_run: true,
            games: [],
            opportunities: [],
            bets: []
        };

        // Load sports status
        async function loadSportsStatus() {
            try {
                const resp = await fetch('/api/sports/status');
                const data = await resp.json();

                if (data.error) {
                    sportsData.configured = false;
                    document.getElementById('sports-status-text').textContent = 'Not Configured';
                    document.getElementById('sports-status-light').classList.remove('running');
                    const roText = document.getElementById('sports-status-text-ro');
                    if (roText) roText.textContent = 'Not Configured';
                    const roLight = document.getElementById('sports-status-light-ro');
                    if (roLight) roLight.classList.remove('running');
                    return;
                }

                sportsData.configured = true;
                sportsData.running = data.running;
                sportsData.dry_run = data.dry_run ?? true;

                const statusLight = document.getElementById('sports-status-light');
                const statusText = document.getElementById('sports-status-text');
                const modeBadge = document.getElementById('sports-mode-badge');
                const dryToggle = document.getElementById('sports-dry-toggle');

                if (data.running) {
                    statusLight.classList.add('running');
                    statusText.textContent = 'Scanning...';
                } else {
                    statusLight.classList.remove('running');
                    statusText.textContent = 'Stopped';
                }

                // Dry run badge
                if (sportsData.dry_run) {
                    modeBadge.textContent = 'DRY RUN';
                    modeBadge.style.background = 'var(--orange)';
                    modeBadge.style.color = 'var(--bg-0)';
                    dryToggle.textContent = 'DRY RUN';
                    dryToggle.style.background = 'var(--orange)';
                    dryToggle.style.color = 'var(--bg-0)';
                } else {
                    modeBadge.textContent = 'LIVE';
                    modeBadge.style.background = 'var(--green)';
                    modeBadge.style.color = 'var(--bg-0)';
                    dryToggle.textContent = 'LIVE';
                    dryToggle.style.background = 'var(--green)';
                    dryToggle.style.color = 'var(--bg-0)';
                }

                // Stats - align with get_status() response
                document.getElementById('sports-game-count').textContent = data.live_games || 0;
                document.getElementById('sports-opp-count').textContent = data.opportunities || 0;
                document.getElementById('sports-bet-count').textContent = data.bets_placed || 0;
                document.getElementById('sports-exposure').textContent = '$' + fmt(data.total_exposure || 0);
                document.getElementById('sports-pnl').textContent = '$0';  // TODO: track P&L

                // Store data for rendering
                sportsData.games = data.games || [];
                sportsData.opportunities = data.top_opportunities || [];
                sportsData.bets = data.recent_bets || [];

                // Update badges
                document.getElementById('games-badge').textContent = sportsData.games.length;
                document.getElementById('opps-badge').textContent = sportsData.opportunities.length;
                document.getElementById('bets-badge').textContent = sportsData.bets.length;

                // Render tables
                renderSportsGames(sportsData.games);
                renderSportsOpportunities(sportsData.opportunities);
                renderSportsBets(sportsData.bets);

                // Update read-only elements on Sports tab
                const roLight = document.getElementById('sports-status-light-ro');
                const roText = document.getElementById('sports-status-text-ro');
                const roBadge = document.getElementById('sports-mode-badge-ro');
                if (roLight && roText) {
                    if (data.running) {
                        roLight.classList.add('running');
                        roText.textContent = 'Scanning...';
                    } else {
                        roLight.classList.remove('running');
                        roText.textContent = 'Stopped';
                    }
                }
                if (roBadge) {
                    if (sportsData.dry_run) {
                        roBadge.textContent = 'DRY RUN';
                        roBadge.style.background = 'var(--orange)';
                        roBadge.style.color = 'var(--bg-0)';
                    } else {
                        roBadge.textContent = 'LIVE';
                        roBadge.style.background = 'var(--green)';
                        roBadge.style.color = 'var(--bg-0)';
                    }
                }
                const roGames = document.getElementById('sports-game-count-ro');
                const roOpps = document.getElementById('sports-opp-count-ro');
                const roBets = document.getElementById('sports-bet-count-ro');
                if (roGames) roGames.textContent = data.live_games || 0;
                if (roOpps) roOpps.textContent = data.opportunities || 0;
                if (roBets) roBets.textContent = data.bets_placed || 0;

                // Sports config is now part of unified trading config (loaded separately)

            } catch (err) {
                console.error('Failed to load sports status:', err);
            }
        }

        // Render live games table
        function renderSportsGames(games) {
            const tbody = document.getElementById('sports-games-table');

            if (!games || games.length === 0) {
                tbody.innerHTML = `
                    <tr><td colspan="5" class="empty" style="padding: 40px; text-align: center; color: var(--text-3);">
                        <div style="font-size: 14px; font-weight: 500; margin-bottom: 8px;">No live games found</div>
                    </td></tr>
                `;
                return;
            }

            tbody.innerHTML = games.map(g => {
                // Format best edge for display
                const hasEdge = g.best_edge != null;
                const edgeStr = hasEdge ? '+' + g.best_edge.toFixed(1) + '%' : '-';
                const edgeColor = hasEdge && g.best_edge > 0 ? 'var(--green)' : '';

                return `
                    <tr>
                        <td>${esc(formatSportName(g.sport))}</td>
                        <td>
                            <div style="font-weight: 500;">${esc(g.display_name || 'Unknown Game')}</div>
                            <div style="font-size: 11px; color: var(--text-3);">${esc(g.event_ticker || '')}</div>
                        </td>
                        <td>
                            <span style="color: ${g.is_live ? 'var(--green)' : 'var(--text-3)'}">
                                ${g.is_live ? 'LIVE' : 'Scheduled'}
                            </span>
                            ${g.matched ? '<span style="color: var(--blue); margin-left: 8px; font-size: 11px;" title="Matched to sportsbook odds">MATCHED</span>' : ''}
                        </td>
                        <td>${g.markets_count || 0} markets</td>
                        <td class="text-right" style="${edgeColor ? 'color: ' + edgeColor + '; font-weight: 600;' : ''}">${edgeStr}</td>
                    </tr>
                `;
            }).join('');
        }

        // Format sport name for display
        function formatSportName(sport) {
            if (!sport) return 'Unknown';
            const names = {
                'basketball_nba': 'NBA',
                'americanfootball_nfl': 'NFL',
                'icehockey_nhl': 'NHL',
                'baseball_mlb': 'MLB',
                'soccer_usa_mls': 'MLS',
                'mma_ufc': 'UFC'
            };
            return names[sport.toLowerCase()] || sport.replace(/_/g, ' ');
        }

        // Render opportunities table
        function renderSportsOpportunities(opps) {
            const tbody = document.getElementById('sports-opps-table');

            if (!opps || opps.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center; padding: 24px;">No opportunities found</td></tr>';
                return;
            }

            tbody.innerHTML = opps.map(o => {
                const edgePct = o.edge_pct || 0;
                const edgeClass = edgePct >= 5 ? 'positive' : '';
                const staleClass = o.is_stale ? 'text-muted' : '';

                return `
                    <tr class="${staleClass}">
                        <td>
                            <div style="font-weight: 500;">${esc(o.title || o.ticker)}</div>
                            <div style="font-size: 11px; color: var(--text-3);">${esc(o.best_book || '')}</div>
                        </td>
                        <td>${esc(o.side || 'Unknown')}</td>
                        <td class="text-right mono">${pct(o.kalshi_price)}</td>
                        <td class="text-right mono">${pct(o.fair_value)}</td>
                        <td class="text-right"><span class="edge ${edgeClass}">+${edgePct.toFixed(1)}%</span></td>
                        <td class="text-right">10</td>
                        <td class="text-right">
                            <button class="place-btn" onclick="placeSportsBet('${o.ticker}', '${o.side}', ${o.kalshi_price}, 10)">
                                ${o.side} @ ${Math.round((o.kalshi_price || 0) * 100)}c
                            </button>
                        </td>
                    </tr>
                `;
            }).join('');
        }

        // Render recent bets table
        function renderSportsBets(bets) {
            const tbody = document.getElementById('sports-bets-table');

            if (!bets || bets.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center; padding: 24px;">No bets placed</td></tr>';
                return;
            }

            tbody.innerHTML = bets.slice(0, 20).map(b => {
                const timeStr = b.placed_at ? new Date(b.placed_at).toLocaleTimeString() : '-';
                const statusClass = b.status === 'filled' ? 'text-green' : (b.status === 'pending' ? 'text-blue' : 'text-muted');

                return `
                    <tr>
                        <td class="mono" style="font-size: 12px;">${esc(timeStr)}</td>
                        <td>${esc(b.ticker || '-')}</td>
                        <td><span class="position-side ${b.side?.toLowerCase()}">${esc(b.side || '-')}</span></td>
                        <td class="text-right mono">${b.price ? Math.round(b.price * 100) + 'c' : '-'}</td>
                        <td class="text-right">${b.size || 0}</td>
                        <td class="text-right">${b.edge_pct ? '+' + b.edge_pct.toFixed(1) + '%' : '-'}</td>
                        <td class="${statusClass}">${b.status || 'unknown'}</td>
                    </tr>
                `;
            }).join('');
        }

        // Toggle dry run mode
        async function toggleSportsDryRun() {
            const newValue = !sportsData.dry_run;

            if (!newValue && !confirm('Switch to LIVE mode? Real money will be at risk.')) {
                return;
            }

            try {
                const resp = await fetch('/api/sports/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({dry_run: newValue})
                });
                const data = await resp.json();
                if (data.error) {
                    alert('Error: ' + data.error);
                } else {
                    sportsData.dry_run = newValue;
                    loadSportsStatus();
                }
            } catch (err) {
                alert('Failed to toggle: ' + err);
            }
        }

        // Sports config is now part of the unified Trading Config panel.
        // The /api/sports/config endpoint remains for programmatic access.

        // Place a sports bet
        async function placeSportsBet(ticker, side, price, size) {
            if (sportsData.dry_run) {
                alert('Cannot place bets in DRY RUN mode. Switch to LIVE mode first.');
                return;
            }

            if (!confirm(`Place ${side} order for ${size} contracts at ${Math.round(price * 100)}c?`)) {
                return;
            }

            try {
                const resp = await fetch('/api/place-order', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ticker, side, price: Math.round(price * 100), size})
                });
                const data = await resp.json();
                if (data.error) {
                    alert('Error: ' + data.error);
                } else {
                    alert('Order placed!');
                    loadSportsStatus();
                }
            } catch (err) {
                alert('Failed to place bet: ' + err);
            }
        }

        // Load sports data on section switch (Trading Plan or Sports tab)
        document.querySelectorAll('.tab').forEach(tab => {
            tab.addEventListener('click', () => {
                if (tab.dataset.tab === 'sports' || tab.dataset.tab === 'trading') {
                    loadSportsStatus();
                }
            });
        });

        // Periodic refresh when Trading Plan or Sports tab is active (every 5 seconds)
        setInterval(() => {
            const tradingActive = document.getElementById('section-trading')?.classList.contains('active');
            const sportsActive = document.getElementById('section-sports')?.classList.contains('active');
            if (tradingActive || sportsActive) {
                loadSportsStatus();
            }
        }, 5000);
    </script>
</body>
</html>
'''


if __name__ == '__main__':
    import os
    app = create_app()
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=5050, debug=debug_mode, use_reloader=debug_mode)
