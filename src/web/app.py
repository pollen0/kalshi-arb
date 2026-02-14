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
from src.financial.fair_value_v2 import SophisticatedFairValue, calculate_range_probability_v2, hours_until
from src.financial.orderbook_analyzer import OrderbookAnalyzer, get_order_recommendation
from src.financial.trading_strategy import TradingStrategy, generate_trading_plan, TradingPlan
from src.financial.auto_trader import AutoTrader, TraderConfig, create_auto_trader
from src.financial.risk_manager import RiskManager, get_risk_manager
from src.financial.options_data import OptionsDataClient, get_options_client
from src.financial.event_calendar import EventCalendar, get_event_calendar, load_fomc_dates
from src.financial.market_discovery import MarketDiscovery, MarketRollover, ExpirationSlot


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
    }

    # All market category keys (single source of truth)
    ALL_MARKET_KEYS = [
        "nasdaq_above", "nasdaq_range", "spx_above", "spx_range",
        "treasury", "usdjpy", "eurusd", "wti",
        "bitcoin", "ethereum", "solana", "dogecoin", "xrp",
    ]

    def get_all_markets() -> list:
        """Get all markets from all categories (snapshot under lock)."""
        with state_lock:
            result = []
            for key in ALL_MARKET_KEYS:
                result.extend(state["markets"].get(key, []))
            return result

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
            state["fair_value_model"].event_calendar = state["event_calendar"]
            print("[CALENDAR] Loaded economic event calendar (linked to FV model for vol boost)")

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
                max_total_orders=50,        # Max 50 concurrent orders
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
                    ndx_markets = state["client"].get_markets_by_series_prefix("KXNASDAQ100", status="open")
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
                with state_lock:
                    state["markets"]["nasdaq_range"] = sorted(range_markets, key=lambda x: x.lower_bound or 0)

                # Get NASDAQ-100 above/below markets from all active slots
                above_markets = []
                try:
                    ndx_above = state["client"].get_markets_by_series_prefix("KXNASDAQ100U", status="open")
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
                with state_lock:
                    state["markets"]["nasdaq_above"] = sorted(above_markets, key=lambda x: x.lower_bound or 0)

            # Update S&P 500 markets from ALL expiration slots
            # KXINX = Range markets (between X and Y)
            # KXINXU = Above/below markets (X or above)
            es = state["futures_quotes"].get("spx")
            if es:
                # Get S&P 500 range markets from all active slots
                range_markets = []
                try:
                    spx_markets = state["client"].get_markets_by_series_prefix("KXINX", status="open")
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
                with state_lock:
                    state["markets"]["spx_range"] = sorted(range_markets, key=lambda x: x.lower_bound or 0)

                # Get S&P 500 above/below markets from all active slots
                above_markets = []
                try:
                    spx_above = state["client"].get_markets_by_series_prefix("KXINXU", status="open")
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
                with state_lock:
                    state["markets"]["spx_above"] = sorted(above_markets, key=lambda x: x.lower_bound or 0)

            # Update Treasury 10Y markets (KXTNOTED)
            try:
                # Always fetch markets from Kalshi, regardless of quote availability
                tnote_markets = state["client"].get_markets_by_series_prefix("KXTNOTED", status="open")
                treasury_markets = []

                # Try to get treasury quote (with Yahoo Finance fallback via get_quote)
                treasury_quote = state["futures_quotes"].get("treasury10y")
                treasury_price = None
                if treasury_quote and treasury_quote.price:
                    treasury_price = treasury_quote.price
                else:
                    # Fallback: fetch directly (triggers Yahoo Finance fallback inside get_quote)
                    fallback_quote = state["futures"].get_quote("treasury10y")
                    if fallback_quote and fallback_quote.price:
                        treasury_price = fallback_quote.price
                        print(f"[MARKETS] Treasury quote from fallback: {treasury_price:.3f}%")

                if treasury_price:
                    # Get historical volatility for Treasury
                    treasury_vol = state["futures"].get_historical_volatility("treasury10y", 30)
                    if treasury_vol:
                        fv_model.historical_vols["treasury10y"] = treasury_vol

                for m in (tnote_markets or []):
                    hours = hours_until(m.close_time)
                    if hours and hours > 0:
                        if treasury_price:
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

                with state_lock:
                    state["markets"]["treasury"] = sorted(treasury_markets, key=lambda x: x.lower_bound or 0)
                if treasury_markets:
                    price_str = f", yield={treasury_price:.3f}%" if treasury_price else " (no quote)"
                    print(f"[MARKETS] Loaded {len(treasury_markets)} Treasury markets{price_str}")
            except Exception as e:
                print(f"[MARKETS] Error fetching Treasury: {e}")
                import traceback
                traceback.print_exc()

            # Update USD/JPY markets (KXUSDJPY)
            try:
                # Always fetch markets from Kalshi, regardless of quote availability
                jpy_markets = state["client"].get_markets_by_series_prefix("KXUSDJPY", status="open")
                usdjpy_markets = []

                usdjpy_quote = state["futures_quotes"].get("usdjpy")
                usdjpy_price = None
                if usdjpy_quote and usdjpy_quote.price:
                    usdjpy_price = usdjpy_quote.price
                else:
                    fallback_quote = state["futures"].get_quote("usdjpy")
                    if fallback_quote and fallback_quote.price:
                        usdjpy_price = fallback_quote.price
                        print(f"[MARKETS] USD/JPY quote from fallback: {usdjpy_price:.3f}")

                for m in (jpy_markets or []):
                    hours = hours_until(m.close_time)
                    if hours and hours > 0:
                        if usdjpy_price:
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
                            elif m.lower_bound is not None:
                                # Above market
                                fv_result = fv_model.calculate(
                                    current_price=usdjpy_price,
                                    lower_bound=m.lower_bound,
                                    upper_bound=float('inf'),
                                    hours_to_expiry=hours,
                                    index="usdjpy",
                                    close_time=m.close_time,
                                )
                            elif m.upper_bound is not None:
                                # Below market
                                fv_result = fv_model.calculate(
                                    current_price=usdjpy_price,
                                    lower_bound=0,
                                    upper_bound=m.upper_bound,
                                    hours_to_expiry=hours,
                                    index="usdjpy",
                                    close_time=m.close_time,
                                )
                            else:
                                continue
                            m.fair_value = fv_result.probability
                            m.fair_value_time = datetime.now(timezone.utc)
                            m.model_source = f"USDJPY={usdjpy_price:.3f}"
                        usdjpy_markets.append(m)

                with state_lock:
                    state["markets"]["usdjpy"] = sorted(usdjpy_markets, key=lambda x: x.lower_bound or 0)
                if usdjpy_markets:
                    price_str = f", price={usdjpy_price:.3f}" if usdjpy_price else " (no quote)"
                    print(f"[MARKETS] Loaded {len(usdjpy_markets)} USD/JPY markets{price_str}")
            except Exception as e:
                print(f"[MARKETS] Error fetching USD/JPY: {e}")

            # Update EUR/USD markets (KXEURUSD)
            try:
                # Always fetch markets from Kalshi, regardless of quote availability
                eur_markets = state["client"].get_markets_by_series_prefix("KXEURUSD", status="open")
                eurusd_markets = []

                eurusd_quote = state["futures_quotes"].get("eurusd")
                eurusd_price = None
                if eurusd_quote and eurusd_quote.price:
                    eurusd_price = eurusd_quote.price
                else:
                    fallback_quote = state["futures"].get_quote("eurusd")
                    if fallback_quote and fallback_quote.price:
                        eurusd_price = fallback_quote.price
                        print(f"[MARKETS] EUR/USD quote from fallback: {eurusd_price:.5f}")

                for m in (eur_markets or []):
                    hours = hours_until(m.close_time)
                    if hours and hours > 0:
                        if eurusd_price:
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
                            elif m.lower_bound is not None:
                                # Above market
                                fv_result = fv_model.calculate(
                                    current_price=eurusd_price,
                                    lower_bound=m.lower_bound,
                                    upper_bound=float('inf'),
                                    hours_to_expiry=hours,
                                    index="eurusd",
                                    close_time=m.close_time,
                                )
                            elif m.upper_bound is not None:
                                # Below market
                                fv_result = fv_model.calculate(
                                    current_price=eurusd_price,
                                    lower_bound=0,
                                    upper_bound=m.upper_bound,
                                    hours_to_expiry=hours,
                                    index="eurusd",
                                    close_time=m.close_time,
                                )
                            else:
                                continue
                            m.fair_value = fv_result.probability
                            m.fair_value_time = datetime.now(timezone.utc)
                            m.model_source = f"EURUSD={eurusd_price:.5f}"
                        eurusd_markets.append(m)

                with state_lock:
                    state["markets"]["eurusd"] = sorted(eurusd_markets, key=lambda x: x.lower_bound or 0)
                if eurusd_markets:
                    price_str = f", price={eurusd_price:.5f}" if eurusd_price else " (no quote)"
                    print(f"[MARKETS] Loaded {len(eurusd_markets)} EUR/USD markets{price_str}")
            except Exception as e:
                print(f"[MARKETS] Error fetching EUR/USD: {e}")

            # Update WTI Crude Oil markets (KXWTI daily + KXWTIW weekly)
            try:
                wti_quote = state["futures_quotes"].get("wti")
                if not wti_quote or not wti_quote.price:
                    fallback_quote = state["futures"].get_quote("wti")
                    if fallback_quote and fallback_quote.price:
                        wti_quote = fallback_quote

                wti_price = wti_quote.price if wti_quote else None

                wti_markets = []
                # Fetch daily and weekly WTI markets
                for series in ["KXWTI", "KXWTIW"]:
                    raw = state["client"].get_markets_by_series_prefix(series, status="open")
                    for m in (raw or []):
                        hours = hours_until(m.close_time)
                        if hours and hours > 0:
                            if wti_price:
                                if m.lower_bound is not None and m.upper_bound is not None:
                                    fv_result = fv_model.calculate(
                                        current_price=wti_price,
                                        lower_bound=m.lower_bound,
                                        upper_bound=m.upper_bound,
                                        hours_to_expiry=hours,
                                        index="wti",
                                        close_time=m.close_time,
                                    )
                                elif m.lower_bound is not None:
                                    fv_result = fv_model.calculate(
                                        current_price=wti_price,
                                        lower_bound=m.lower_bound,
                                        upper_bound=float('inf'),
                                        hours_to_expiry=hours,
                                        index="wti",
                                        close_time=m.close_time,
                                    )
                                elif m.upper_bound is not None:
                                    fv_result = fv_model.calculate(
                                        current_price=wti_price,
                                        lower_bound=0,
                                        upper_bound=m.upper_bound,
                                        hours_to_expiry=hours,
                                        index="wti",
                                        close_time=m.close_time,
                                    )
                                else:
                                    continue
                                m.fair_value = fv_result.probability
                                m.fair_value_time = datetime.now(timezone.utc)
                                m.model_source = f"WTI=${wti_price:.2f}"
                            wti_markets.append(m)

                with state_lock:
                    state["markets"]["wti"] = sorted(wti_markets, key=lambda x: x.lower_bound or 0)
                if wti_markets:
                    price_str = f", price=${wti_price:.2f}" if wti_price else " (no quote)"
                    print(f"[MARKETS] Loaded {len(wti_markets)} WTI markets{price_str}")
            except Exception as e:
                print(f"[MARKETS] Error fetching WTI: {e}")

            # Update Crypto markets (BTC, ETH, SOL, DOGE, XRP — each with 15min, hourly, daily)
            CRYPTO_COINS = {
                "bitcoin": {"series": ["KXBTC", "KXBTC15M", "KXBTCD"], "label": "BTC"},
                "ethereum": {"series": ["KXETH", "KXETH15M", "KXETHD"], "label": "ETH"},
                "solana": {"series": ["KXSOL", "KXSOL15M", "KXSOLD"], "label": "SOL"},
                "dogecoin": {"series": ["KXDOGE", "KXDOGE15M", "KXDOGED"], "label": "DOGE"},
                "xrp": {"series": ["KXXRP", "KXXRP15M", "KXXRPD"], "label": "XRP"},
            }
            for coin_key, coin_info in CRYPTO_COINS.items():
                try:
                    coin_quote = state["futures_quotes"].get(coin_key)
                    if not coin_quote or not coin_quote.price:
                        fallback = state["futures"].get_quote(coin_key)
                        if fallback and fallback.price:
                            coin_quote = fallback

                    coin_price = coin_quote.price if coin_quote else None
                    coin_markets = []

                    for series in coin_info["series"]:
                        raw = state["client"].get_markets_by_series_prefix(series, status="open")
                        for m in (raw or []):
                            hours = hours_until(m.close_time)
                            if hours and hours > 0:
                                if coin_price:
                                    if m.lower_bound is not None and m.upper_bound is not None:
                                        fv_result = fv_model.calculate(
                                            current_price=coin_price,
                                            lower_bound=m.lower_bound,
                                            upper_bound=m.upper_bound,
                                            hours_to_expiry=hours,
                                            index=coin_key,
                                            close_time=m.close_time,
                                        )
                                    elif m.lower_bound is not None:
                                        fv_result = fv_model.calculate(
                                            current_price=coin_price,
                                            lower_bound=m.lower_bound,
                                            upper_bound=float('inf'),
                                            hours_to_expiry=hours,
                                            index=coin_key,
                                            close_time=m.close_time,
                                        )
                                    elif m.upper_bound is not None:
                                        fv_result = fv_model.calculate(
                                            current_price=coin_price,
                                            lower_bound=0,
                                            upper_bound=m.upper_bound,
                                            hours_to_expiry=hours,
                                            index=coin_key,
                                            close_time=m.close_time,
                                        )
                                    else:
                                        continue
                                    m.fair_value = fv_result.probability
                                    m.fair_value_time = datetime.now(timezone.utc)
                                    m.model_source = f"{coin_info['label']}=${coin_price:,.2f}"
                                coin_markets.append(m)

                    with state_lock:
                        state["markets"][coin_key] = sorted(coin_markets, key=lambda x: x.lower_bound or 0)
                    if coin_markets:
                        price_str = f", price=${coin_price:,.2f}" if coin_price else " (no quote)"
                        print(f"[MARKETS] Loaded {len(coin_markets)} {coin_info['label']} markets{price_str}")
                except Exception as e:
                    print(f"[MARKETS] Error fetching {coin_info['label']}: {e}")

            # Update position prices
            for pos in state["positions"]:
                # Find matching market in all market types
                for key in ["nasdaq_above", "nasdaq_range", "spx_above", "spx_range", "treasury", "usdjpy", "eurusd", "wti",
                            "bitcoin", "ethereum", "solana", "dogecoin", "xrp"]:
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
                state["markets"].get("eurusd", []) +
                state["markets"].get("wti", []) +
                state["markets"].get("bitcoin", []) +
                state["markets"].get("ethereum", []) +
                state["markets"].get("solana", []) +
                state["markets"].get("dogecoin", []) +
                state["markets"].get("xrp", [])
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

    # Rotation counter for non-equity asset classes (spread API load)
    _fast_market_rotation = {"counter": 0}

    def update_markets_fast():
        """Fast update of just market prices (not full refresh)"""
        if not state["client"]:
            return

        try:
            # Always refresh equity markets (highest priority, every 500ms)
            equity_keys = ["nasdaq_above", "nasdaq_range", "spx_above", "spx_range"]

            # Rotate through other asset classes (one group per iteration ≈ every 500ms)
            # This keeps API calls manageable while refreshing non-equity markets every ~2.5s
            other_groups = [
                ["treasury", "wti"],
                ["usdjpy", "eurusd"],
                ["bitcoin", "ethereum"],
                ["solana", "dogecoin", "xrp"],
            ]
            rotation = _fast_market_rotation["counter"] % len(other_groups)
            _fast_market_rotation["counter"] += 1
            current_keys = equity_keys + other_groups[rotation]

            for key in current_keys:
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
            try:
                update_data()
            except Exception as e:
                print(f"[THREAD] background_updater error: {e}")
            state["thread_heartbeats"]["background_updater"] = time.time()
            time.sleep(30)  # Full refresh every 30 seconds

    def fast_market_updater():
        """Fast background thread for market prices - every 500ms"""
        while True:
            try:
                update_markets_fast()
            except Exception as e:
                print(f"[THREAD] fast_market_updater error: {e}")
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

                    # Recalc above-type markets (same prices, upper_bound=inf)
                    if nq and nq.price > 10000:
                        for m in state["markets"].get("nasdaq_above", []):
                            if m.lower_bound is not None:
                                hours = hours_until(m.close_time)
                                if hours and hours > 0:
                                    ub = m.upper_bound if (m.upper_bound is not None and m.upper_bound != float('inf')) else float('inf')
                                    fv_result = fv_model.calculate(
                                        current_price=nq.price, lower_bound=m.lower_bound,
                                        upper_bound=ub, hours_to_expiry=hours,
                                        index="NDX", close_time=m.close_time,
                                    )
                                    m.fair_value = fv_result.probability
                                    m.fair_value_time = datetime.now(timezone.utc)
                                    m.model_source = f"NQ={nq.price:.0f} [{fv_result.vol_source}]"
                    if es and es.price > 3000:
                        for m in state["markets"].get("spx_above", []):
                            if m.lower_bound is not None:
                                hours = hours_until(m.close_time)
                                if hours and hours > 0:
                                    ub = m.upper_bound if (m.upper_bound is not None and m.upper_bound != float('inf')) else float('inf')
                                    fv_result = fv_model.calculate(
                                        current_price=es.price, lower_bound=m.lower_bound,
                                        upper_bound=ub, hours_to_expiry=hours,
                                        index="SPX", close_time=m.close_time,
                                    )
                                    m.fair_value = fv_result.probability
                                    m.fair_value_time = datetime.now(timezone.utc)
                                    m.model_source = f"ES={es.price:.0f} [{fv_result.vol_source}]"

                    # Recalc crypto fair values (5s refresh instead of 30s)
                    crypto_map = {
                        "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
                        "dogecoin": "DOGE", "xrp": "XRP",
                    }
                    for coin_key, label in crypto_map.items():
                        coin_quote = state["futures_quotes"].get(coin_key)
                        if not coin_quote or not coin_quote.price:
                            continue
                        cp = coin_quote.price
                        for m in state["markets"].get(coin_key, []):
                            if m.lower_bound is None and m.upper_bound is None:
                                continue
                            hours = hours_until(m.close_time)
                            if not hours or hours <= 0:
                                continue
                            lb = m.lower_bound if m.lower_bound is not None else 0
                            ub = m.upper_bound if (m.upper_bound is not None and m.upper_bound != float('inf')) else float('inf')
                            fv_result = fv_model.calculate(
                                current_price=cp, lower_bound=lb,
                                upper_bound=ub, hours_to_expiry=hours,
                                index=coin_key, close_time=m.close_time,
                            )
                            m.fair_value = fv_result.probability
                            m.fair_value_time = datetime.now(timezone.utc)
                            m.model_source = f"{label}=${cp:,.2f}"

                    # Recalc treasury, forex, WTI fair values
                    for mkt_key, quote_key, index_name in [
                        ("treasury", "treasury10y", "treasury10y"),
                        ("usdjpy", "usdjpy", "usdjpy"),
                        ("eurusd", "eurusd", "eurusd"),
                        ("wti", "wti", "wti"),
                    ]:
                        q = state["futures_quotes"].get(quote_key)
                        if not q or not q.price:
                            continue
                        for m in state["markets"].get(mkt_key, []):
                            if m.lower_bound is None and m.upper_bound is None:
                                continue
                            hours = hours_until(m.close_time)
                            if not hours or hours <= 0:
                                continue
                            lb = m.lower_bound if m.lower_bound is not None else 0
                            ub = m.upper_bound if (m.upper_bound is not None and m.upper_bound != float('inf')) else float('inf')
                            fv_result = fv_model.calculate(
                                current_price=q.price, lower_bound=lb,
                                upper_bound=ub, hours_to_expiry=hours,
                                index=index_name, close_time=m.close_time,
                            )
                            m.fair_value = fv_result.probability
                            m.fair_value_time = datetime.now(timezone.utc)
            except Exception as e:
                print(f"[FUTURES] Error: {e}")
            state["thread_heartbeats"]["fast_futures_updater"] = time.time()
            time.sleep(5)  # Update every 5 seconds (rate limited, cached for 10s)

    def orderbook_updater():
        """Background thread for orderbook updates on high-edge markets"""
        while True:
            try:
                if state["client"]:
                    all_markets = get_all_markets()
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
                all_markets = get_all_markets()
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
                    # If trade_through_events is enabled, skip the pause (vol boost handles risk)
                    if state.get("trading_paused"):
                        trader = state["auto_trader"]
                        if trader and trader.config.trade_through_events:
                            # Trading through event — vol boost in FV model handles the risk
                            if not hasattr(auto_trading_loop, '_last_event_log') or \
                               time.time() - auto_trading_loop._last_event_log > 60:
                                reason = state.get("trading_pause_reason", "Unknown")
                                print(f"[AUTO-TRADE] Trading through event (vol-boosted): {reason}")
                                auto_trading_loop._last_event_log = time.time()
                        else:
                            reason = state.get("trading_pause_reason", "Unknown")
                            # Only log occasionally to avoid spam
                            if not hasattr(auto_trading_loop, '_last_pause_log') or \
                               time.time() - auto_trading_loop._last_pause_log > 60:
                                print(f"[AUTO-TRADE] Paused: {reason}")
                                auto_trading_loop._last_pause_log = time.time()
                            time.sleep(1)
                            continue

                    all_markets = get_all_markets()
                    # Only trade if we have fresh futures data (sanity check)
                    futures_ok = False
                    with state_lock:
                        nq = state["futures_quotes"].get("nasdaq")
                        es = state["futures_quotes"].get("spx")
                        t10y = state["futures_quotes"].get("treasury10y")
                        usdjpy_q = state["futures_quotes"].get("usdjpy")
                        eurusd_q = state["futures_quotes"].get("eurusd")
                        wti_q = state["futures_quotes"].get("wti")
                    if nq and nq.price > 10000:  # NQ should be ~21000+
                        futures_ok = True
                    if es and es.price > 3000:   # ES should be ~6000+
                        futures_ok = True
                    if t10y and t10y.price > 0:  # Treasury yield should be positive
                        futures_ok = True
                    if usdjpy_q and usdjpy_q.price > 100:  # USD/JPY should be ~150+
                        futures_ok = True
                    if eurusd_q and eurusd_q.price > 0.5:  # EUR/USD should be ~1.0+
                        futures_ok = True
                    if wti_q and wti_q.price > 20:  # WTI should be ~$60+
                        futures_ok = True
                    with state_lock:
                        btc = state["futures_quotes"].get("bitcoin")
                    if btc and btc.price > 1000:  # BTC should be ~$90000+
                        futures_ok = True

                    # Check thread heartbeats — halt if critical threads are dead
                    # All data-producing threads are critical: if any dies, we trade on stale data
                    critical_threads = {
                        "fast_futures_updater": 30,    # Updates every 5s, stale after 30s
                        "fast_market_updater": 15,     # Updates every 0.5s, stale after 15s
                        "background_updater": 120,     # Updates every 30s, stale after 120s
                        "orderbook_updater": 30,       # Updates every 1s, stale after 30s
                    }
                    now_ts = time.time()
                    for thread_name, max_age in critical_threads.items():
                        last_hb = state["thread_heartbeats"].get(thread_name)
                        if last_hb and (now_ts - last_hb) > max_age:
                            print(f"[AUTO-TRADE] HALTING: {thread_name} heartbeat stale ({now_ts - last_hb:.0f}s > {max_age}s)")
                            state["auto_trader"]._halt(f"Thread {thread_name} dead (>{now_ts - last_hb:.0f}s)")
                            break

                    if all_markets and futures_ok:
                        # Pass VIX price for dynamic order scaling
                        if state.get("vix_price"):
                            state["auto_trader"]._vix_price = state["vix_price"]
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
        def format_markets(markets, futures_price, is_yield_based=False, is_forex=False, forex_decimals=3, is_commodity=False, is_crypto=False):
            result = []
            for m in markets:
                # Handle missing bounds: "below" markets have lower_bound=None, upper_bound set
                lower = m.lower_bound
                upper = m.upper_bound
                if lower is None and upper is not None:
                    lower = 0  # "below" market
                elif lower is None:
                    continue   # truly no bounds — skip

                # Format range (handle above/below markets where upper_bound is None or inf)
                if is_yield_based:
                    # Treasury yields: format as percentage (e.g., "4.50%+" or "4.50-4.55%")
                    if upper is None or upper == float('inf'):
                        range_str = f"{lower:.2f}%+"
                    elif lower <= 0:
                        range_str = f"<{upper:.2f}%"
                    else:
                        range_str = f"{lower:.2f}-{upper:.2f}%"
                elif is_forex:
                    # Forex: format with appropriate decimals (e.g., "156.500-156.749" for JPY, "1.18200-1.18399" for EUR)
                    fmt = f"{{:.{forex_decimals}f}}"
                    if upper is None or upper == float('inf'):
                        range_str = fmt.format(lower) + "+"
                    elif lower <= 0:
                        range_str = "<" + fmt.format(upper)
                    else:
                        range_str = fmt.format(lower) + "-" + fmt.format(upper)
                elif is_commodity:
                    # Commodity (oil): format as dollar prices with 2 decimals
                    if upper is None or upper == float('inf'):
                        range_str = f"${lower:.2f}+"
                    elif lower <= 0:
                        range_str = f"<${upper:.2f}"
                    else:
                        range_str = f"${lower:.2f}-{upper:.2f}"
                elif is_crypto:
                    # Crypto: format as dollar prices (BTC uses commas, small coins use decimals)
                    if lower >= 100:
                        # Large values (BTC, ETH): use commas, no decimals
                        if upper is None or upper == float('inf'):
                            range_str = f"${lower:,.0f}+"
                        elif lower <= 0:
                            range_str = f"<${upper:,.0f}"
                        else:
                            range_str = f"${lower:,.0f}-{upper:,.0f}"
                    else:
                        # Small values (DOGE, XRP, SOL): use decimals
                        if upper is None or upper == float('inf'):
                            range_str = f"${lower:.4f}+"
                        elif lower <= 0:
                            range_str = f"<${upper:.4f}"
                        else:
                            range_str = f"${lower:.4f}-{upper:.4f}"
                else:
                    # Equity indices: format as large integers
                    if upper is None or upper == float('inf'):
                        range_str = f"{lower:,.0f}+"
                    elif lower <= 0:
                        range_str = f"<{upper:,.0f}"
                    else:
                        range_str = f"{lower:,.0f}-{upper:,.0f}"

                # Calculate distance from current price
                if futures_price:
                    if upper is None or upper == float('inf'):
                        # For above markets, use lower_bound as the reference
                        distance = lower - futures_price
                    else:
                        mid = (lower + upper) / 2
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
        wti = state["futures_quotes"].get("wti")
        btc = state["futures_quotes"].get("bitcoin")
        eth = state["futures_quotes"].get("ethereum")
        sol = state["futures_quotes"].get("solana")
        doge = state["futures_quotes"].get("dogecoin")
        xrp = state["futures_quotes"].get("xrp")

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
            "wti": {
                "quote": {
                    "price": wti.price if wti else None,
                    "change": wti.change if wti else None,
                    "change_pct": wti.change_pct if wti else None,
                },
                "markets": format_markets(state["markets"].get("wti", []), wti.price if wti else None, is_commodity=True),
            },
            "bitcoin": {
                "quote": {
                    "price": btc.price if btc else None,
                    "change": btc.change if btc else None,
                    "change_pct": btc.change_pct if btc else None,
                },
                "markets": format_markets(state["markets"].get("bitcoin", []), btc.price if btc else None, is_crypto=True),
            },
            "ethereum": {
                "quote": {
                    "price": eth.price if eth else None,
                    "change": eth.change if eth else None,
                    "change_pct": eth.change_pct if eth else None,
                },
                "markets": format_markets(state["markets"].get("ethereum", []), eth.price if eth else None, is_crypto=True),
            },
            "solana": {
                "quote": {
                    "price": sol.price if sol else None,
                    "change": sol.change if sol else None,
                    "change_pct": sol.change_pct if sol else None,
                },
                "markets": format_markets(state["markets"].get("solana", []), sol.price if sol else None, is_crypto=True),
            },
            "dogecoin": {
                "quote": {
                    "price": doge.price if doge else None,
                    "change": doge.change if doge else None,
                    "change_pct": doge.change_pct if doge else None,
                },
                "markets": format_markets(state["markets"].get("dogecoin", []), doge.price if doge else None, is_crypto=True),
            },
            "xrp": {
                "quote": {
                    "price": xrp.price if xrp else None,
                    "change": xrp.change if xrp else None,
                    "change_pct": xrp.change_pct if xrp else None,
                },
                "markets": format_markets(state["markets"].get("xrp", []), xrp.price if xrp else None, is_crypto=True),
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
                "financial_position_size": (1, 200),
                "max_total_orders": (5, 200),
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
            # Handle max_total_orders (lives on TraderConfig, not RiskConfig)
            if "max_total_orders" in data:
                max_orders_val = int(data["max_total_orders"])
                trader = state.get("auto_trader")
                if trader:
                    trader.config.max_total_orders = max_orders_val

            # Propagate to live subsystems
            trader = state.get("auto_trader")
            if trader:
                trader.config.min_edge = rc.financial_min_edge
                trader.config.position_size = rc.financial_position_size
                trader.config.max_daily_loss_pct = rc.max_daily_loss_pct
                trader.config.max_capital_pct = rc.max_total_exposure
                trader._max_drawdown_pct = rc.max_drawdown_pct

            # Persist to disk
            save_config()

        trader = state.get("auto_trader")
        return jsonify({
            "max_daily_loss_pct": rc.max_daily_loss_pct,
            "max_drawdown_pct": rc.max_drawdown_pct,
            "max_total_exposure": rc.max_total_exposure,
            "financial_min_edge": rc.financial_min_edge,
            "financial_position_size": rc.financial_position_size,
            "max_total_orders": trader.config.max_total_orders if trader else 20,
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

    return app


# =============================================================================
# DASHBOARD HTML - Clean, Minimal Dark UI
# =============================================================================

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <title>Kalshi Trading</title>
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
            --yellow: #eab308;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', -apple-system, sans-serif;
            background: var(--bg-0); color: var(--text-1);
            line-height: 1.5; -webkit-font-smoothing: antialiased; min-height: 100vh;
        }
        body.dry-run { border-top: 3px solid var(--orange); }
        .app { max-width: 1400px; margin: 0 auto; padding: 16px 24px; }

        /* Risk Bar */
        .risk-bar {
            display: flex; align-items: center; gap: 20px;
            padding: 12px 20px; background: var(--bg-2);
            border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px;
            flex-wrap: wrap;
        }
        body.dry-run .risk-bar { background: #1a1510; }
        .rb-item { display: flex; flex-direction: column; gap: 2px; }
        .rb-label {
            font-size: 10px; font-weight: 600; color: var(--text-3);
            text-transform: uppercase; letter-spacing: 0.5px;
        }
        .rb-value { font-size: 18px; font-weight: 600; color: var(--text-0); }
        .rb-gauge { width: 120px; }
        .gauge-track {
            height: 6px; background: var(--bg-3); border-radius: 3px;
            overflow: hidden; margin-top: 4px;
        }
        .gauge-fill {
            height: 100%; border-radius: 3px;
            transition: width 0.3s ease, background 0.3s ease;
        }
        .gauge-fill.ok { background: var(--green); }
        .gauge-fill.warn { background: var(--yellow); }
        .gauge-fill.danger { background: var(--red); }

        .status-dot {
            display: inline-block; width: 10px; height: 10px;
            border-radius: 50%; margin-right: 6px;
        }
        .status-dot.running { background: var(--green); animation: pulse 2s infinite; }
        .status-dot.halted { background: var(--red); }
        .status-dot.stopped { background: var(--text-3); }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.7} }

        .mode-badge {
            padding: 4px 12px; border-radius: 4px;
            font-size: 12px; font-weight: 700; letter-spacing: 0.5px;
        }
        .mode-badge.live { background: var(--green); color: #000; }
        .mode-badge.dry-run { background: var(--orange); color: #000; font-size: 14px; padding: 6px 16px; }

        .kill-btn {
            margin-left: auto; padding: 10px 24px;
            font-size: 14px; font-weight: 700; color: white;
            background: var(--red); border: none; border-radius: 8px;
            cursor: pointer; text-transform: uppercase; letter-spacing: 1px;
        }
        .kill-btn:hover { background: #dc2626; }

        .error-count { cursor: pointer; padding: 2px 8px; border-radius: 4px; }
        .error-count.has-errors { background: var(--red-dim); color: var(--red); }

        .error-panel {
            display: none; padding: 16px 20px; background: var(--bg-1);
            border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px;
        }
        .error-panel.open { display: block; }
        .error-bar-container {
            display: flex; gap: 4px; height: 8px;
            border-radius: 4px; overflow: hidden; margin-bottom: 12px;
        }
        .error-bar-segment { height: 100%; border-radius: 2px; }
        .error-list { list-style: none; }
        .error-list li {
            padding: 6px 0; border-bottom: 1px solid var(--border);
            font-size: 12px; color: var(--text-2); display: flex; gap: 8px; align-items: center;
        }
        .error-list li:last-child { border-bottom: none; }
        .error-type-badge {
            padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: 600;
            background: var(--bg-3); color: var(--text-2); white-space: nowrap;
        }
        .error-threshold { font-size: 11px; color: var(--text-3); margin-top: 8px; }

        .data-age {
            font-size: 10px; font-weight: 600; padding: 4px 10px;
            border-radius: 100px; text-transform: uppercase; letter-spacing: 0.5px;
        }
        .data-age.live { background: var(--green-dim); color: var(--green); animation: pulse 2s infinite; }
        .data-age.stale { background: var(--red-dim); color: var(--red); }

        /* Tabs */
        .tabs {
            display: flex; gap: 8px; margin-bottom: 24px;
            border-bottom: 1px solid var(--border); padding-bottom: 12px;
        }
        .tab {
            padding: 10px 20px; font-size: 14px; font-weight: 500;
            color: var(--text-2); background: none; border: none;
            border-radius: 8px; cursor: pointer; transition: all 0.15s ease;
        }
        .tab:hover { color: var(--text-1); background: var(--bg-3); }
        .tab.active { color: var(--text-0); background: var(--bg-3); }
        .section { display: none; }
        .section.active { display: block; }

        /* Tables */
        .table-container {
            background: var(--bg-1); border: 1px solid var(--border);
            border-radius: 12px; overflow: hidden; margin-bottom: 24px;
        }
        table { width: 100%; border-collapse: collapse; }
        th {
            text-align: left; font-size: 11px; font-weight: 600; color: var(--text-3);
            text-transform: uppercase; letter-spacing: 0.5px;
            padding: 12px 16px; background: var(--bg-2);
            border-bottom: 1px solid var(--border);
        }
        td {
            padding: 12px 16px; font-size: 13px; color: var(--text-1);
            border-bottom: 1px solid var(--border);
        }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: var(--bg-hover); }
        .mono { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; }
        .text-green { color: var(--green); }
        .text-red { color: var(--red); }
        .text-blue { color: var(--blue); }
        .text-muted { color: var(--text-3); }
        .text-right { text-align: right; }

        .edge {
            display: inline-flex; align-items: center; gap: 4px;
            font-size: 12px; font-weight: 600; padding: 3px 8px; border-radius: 4px;
        }
        .edge.positive { background: var(--green-dim); color: var(--green); }
        tr.clickable { cursor: pointer; }
        tr.clickable:hover td { background: var(--bg-3); }
        .asset-badge {
            font-size: 11px; font-weight: 600; padding: 2px 6px;
            border-radius: 3px; background: var(--bg-3); color: var(--text-2);
        }

        /* Accordion */
        .accordion-section { margin-bottom: 8px; }
        .accordion-section summary {
            cursor: pointer; padding: 12px 16px; background: var(--bg-2);
            border: 1px solid var(--border); border-radius: 8px;
            font-size: 14px; font-weight: 500; color: var(--text-1);
            list-style: none; display: flex; align-items: center; gap: 12px;
        }
        .accordion-section summary::-webkit-details-marker { display: none; }
        .accordion-section summary::before {
            content: '\25B8'; font-size: 12px; color: var(--text-3); transition: transform 0.2s;
        }
        .accordion-section[open] summary::before { transform: rotate(90deg); }
        .accordion-section[open] summary { border-radius: 8px 8px 0 0; }
        .accordion-content {
            border: 1px solid var(--border); border-top: none;
            border-radius: 0 0 8px 8px; overflow: hidden;
        }
        .futures-inline {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 2px 8px; background: var(--bg-3); border-radius: 4px; font-size: 12px;
        }
        .futures-inline .fp { font-weight: 600; color: var(--text-0); }
        .futures-inline .fc { font-size: 11px; font-weight: 500; }

        /* Position Cards */
        .position-card {
            background: var(--bg-2); border: 1px solid var(--border);
            border-radius: 12px; padding: 16px 20px; margin-bottom: 12px;
        }
        .position-header {
            display: flex; justify-content: space-between;
            align-items: flex-start; margin-bottom: 12px;
        }
        .position-title { font-size: 13px; font-weight: 500; color: var(--text-0); margin-bottom: 2px; }
        .position-ticker { font-size: 11px; color: var(--text-3); font-family: 'SF Mono', monospace; }
        .position-side {
            font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 4px;
        }
        .position-side.yes { background: var(--green-dim); color: var(--green); }
        .position-side.no { background: var(--red-dim); color: var(--red); }
        .position-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
        .position-metric { text-align: center; }
        .position-metric-label {
            font-size: 10px; color: var(--text-3);
            text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px;
        }
        .position-metric-value { font-size: 15px; font-weight: 600; color: var(--text-0); }

        /* Order Age Colors */
        .age-normal { color: var(--text-2); }
        .age-stale { color: var(--orange); }
        .age-critical { color: var(--red); }
        .age-expired { color: var(--red); font-weight: 700; }

        /* Controls */
        .controls-bar {
            display: flex; align-items: center; gap: 12px;
            padding: 16px 20px; background: var(--bg-2);
            border: 1px solid var(--border); border-radius: 12px; margin-bottom: 20px;
        }
        .start-btn {
            padding: 8px 20px; font-size: 13px; font-weight: 600;
            color: var(--bg-0); background: var(--green);
            border: none; border-radius: 6px; cursor: pointer;
        }
        .start-btn:hover { background: #16a34a; }
        .stop-btn {
            padding: 8px 20px; font-size: 13px; font-weight: 600;
            color: var(--text-0); background: var(--red);
            border: none; border-radius: 6px; cursor: pointer;
        }
        .cancel-btn {
            padding: 8px 16px; font-size: 13px; font-weight: 500;
            color: var(--text-1); background: var(--bg-3);
            border: 1px solid var(--border); border-radius: 6px; cursor: pointer;
        }
        .cancel-btn:hover { border-color: var(--red); color: var(--red); }

        /* Config */
        .config-panel {
            padding: 20px 24px; background: var(--bg-1);
            border: 1px solid var(--border); border-radius: 12px; margin-bottom: 24px;
        }
        .config-panel h4 {
            font-size: 12px; font-weight: 600; color: var(--text-3);
            text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 16px;
        }
        .config-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px; margin-bottom: 16px;
        }
        .config-item { display: flex; flex-direction: column; gap: 6px; }
        .config-item label { font-size: 12px; color: var(--text-3); }
        .config-item input {
            padding: 8px 12px; font-size: 14px; color: var(--text-0);
            background: var(--bg-2); border: 1px solid var(--border);
            border-radius: 6px; width: 80px;
        }
        .config-item input:focus { outline: none; border-color: var(--blue); }
        .save-config-btn {
            padding: 8px 16px; font-size: 13px; font-weight: 500;
            color: var(--text-1); background: var(--bg-3);
            border: 1px solid var(--border); border-radius: 6px; cursor: pointer;
        }
        .save-config-btn:hover { background: var(--bg-hover); }

        /* Fill stats */
        .fill-stats-panel {
            padding: 16px; background: var(--bg-1);
            border: 1px solid var(--border); border-radius: 12px; margin-bottom: 20px;
        }
        .fill-stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
        .fill-stat { text-align: center; }
        .fill-stat .label {
            font-size: 10px; color: var(--text-3);
            text-transform: uppercase; letter-spacing: 0.5px;
        }
        .fill-stat .value { font-size: 18px; font-weight: 600; color: var(--text-0); }

        /* Modal */
        .modal-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.8); display: flex;
            align-items: center; justify-content: center; z-index: 1000;
        }
        .modal {
            background: var(--bg-1); border: 1px solid var(--border);
            border-radius: 16px; width: 90%; max-width: 600px;
            max-height: 80vh; overflow: auto;
        }
        .modal-header {
            display: flex; justify-content: space-between; align-items: center;
            padding: 20px 24px; border-bottom: 1px solid var(--border);
        }
        .modal-header h3 {
            font-size: 14px; font-weight: 600; color: var(--text-0);
            font-family: 'SF Mono', monospace;
        }
        .modal-close {
            background: none; border: none; font-size: 24px;
            color: var(--text-3); cursor: pointer;
        }
        .modal-close:hover { color: var(--text-1); }
        .modal-body { padding: 24px; }
        .ob-section, .rec-section { margin-bottom: 24px; }
        .ob-section h4, .rec-section h4 {
            font-size: 12px; font-weight: 600; color: var(--text-3);
            text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px;
        }
        .ob-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        .ob-header { font-size: 11px; font-weight: 600; color: var(--text-3); margin-bottom: 8px; }
        .ob-level {
            display: flex; justify-content: space-between;
            padding: 6px 10px; font-family: 'SF Mono', monospace;
            font-size: 13px; border-radius: 4px; margin-bottom: 4px;
        }
        .ob-level.bid { background: var(--green-dim); color: var(--green); }
        .ob-level.ask { background: var(--red-dim); color: var(--red); }
        .ob-level .qty { color: var(--text-3); }
        .ob-stats { margin-top: 12px; font-size: 13px; color: var(--text-2); }
        .rec-card {
            background: var(--bg-2); border: 1px solid var(--green-dim);
            border-radius: 8px; padding: 16px;
        }
        .rec-action { font-size: 18px; font-weight: 600; color: var(--green); margin-bottom: 8px; }
        .rec-stats { font-size: 13px; color: var(--text-2); margin-bottom: 8px; }
        .rec-reasoning { font-size: 12px; color: var(--text-3); }

        .empty { text-align: center; padding: 48px 20px; color: var(--text-3); }
        .loading {
            display: flex; align-items: center; justify-content: center;
            padding: 32px; color: var(--text-3);
        }
        .spinner {
            width: 18px; height: 18px; border: 2px solid var(--border);
            border-top-color: var(--text-2); border-radius: 50%;
            animation: spin 0.8s linear infinite; margin-right: 10px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        @media (max-width: 768px) {
            .app { padding: 12px; }
            .risk-bar { gap: 12px; padding: 10px 14px; }
            .rb-value { font-size: 14px; }
            .position-grid { grid-template-columns: repeat(2, 1fr); }
        }
    </style>
</head>
<body>
    <div class="app">
        <!-- Risk Bar -->
        <div class="risk-bar" id="risk-bar">
            <div class="rb-item">
                <span class="rb-label">Balance</span>
                <span class="rb-value" id="rb-balance">$0</span>
            </div>
            <div class="rb-item rb-gauge">
                <span class="rb-label">Daily P&amp;L</span>
                <span class="rb-value" id="rb-daily-pnl" style="font-size:14px;">$0</span>
                <div class="gauge-track"><div class="gauge-fill ok" id="rb-pnl-gauge" style="width:0%"></div></div>
            </div>
            <div class="rb-item rb-gauge">
                <span class="rb-label">Exposure</span>
                <span class="rb-value" id="rb-exposure" style="font-size:14px;">0%</span>
                <div class="gauge-track"><div class="gauge-fill ok" id="rb-exposure-gauge" style="width:0%"></div></div>
            </div>
            <div class="rb-item">
                <span class="rb-label">Status</span>
                <span id="rb-status" style="font-size:13px;font-weight:500;">
                    <span class="status-dot stopped"></span> Stopped
                </span>
            </div>
            <div class="rb-item">
                <span class="rb-label">Errors</span>
                <span class="error-count" id="rb-error-count" onclick="toggleErrorPanel()">0</span>
            </div>
            <span class="mode-badge live" id="rb-mode">LIVE</span>
            <span class="data-age live" id="data-age">LIVE</span>
            <button class="kill-btn" onclick="killSwitch()">KILL</button>
        </div>

        <!-- Error Panel -->
        <div class="error-panel" id="error-panel">
            <div style="font-size:12px;font-weight:600;color:var(--text-2);margin-bottom:8px;">Error Breakdown</div>
            <div class="error-bar-container" id="error-bar"></div>
            <div class="error-threshold" id="error-thresholds"></div>
            <div style="margin-top:12px;font-size:12px;font-weight:600;color:var(--text-2);margin-bottom:6px;">Recent Errors</div>
            <ul class="error-list" id="error-list"></ul>
        </div>

        <!-- Tabs -->
        <div class="tabs">
            <button class="tab active" data-tab="opportunities">Opportunities</button>
            <button class="tab" data-tab="positions">Positions &amp; Orders</button>
            <button class="tab" data-tab="settings">Settings</button>
        </div>

        <!-- OPPORTUNITIES TAB -->
        <section class="section active" id="section-opportunities">
            <div class="table-container">
                <table>
                    <thead><tr>
                        <th>Asset</th><th>Strike / Range</th><th>Expiry</th>
                        <th class="text-right">Market</th><th class="text-right">Model</th>
                        <th class="text-right">Edge</th><th class="text-right">Action</th>
                        <th class="text-right">Vol</th>
                    </tr></thead>
                    <tbody id="opportunities-table">
                        <tr><td colspan="8" class="loading"><div class="spinner"></div> Loading...</td></tr>
                    </tbody>
                </table>
            </div>
            <div style="font-size:14px;font-weight:600;color:var(--text-2);margin-bottom:12px;">All Markets by Asset</div>
            <div id="asset-accordions"></div>
        </section>

        <!-- POSITIONS & ORDERS TAB -->
        <section class="section" id="section-positions">
            <div class="controls-bar">
                <button class="start-btn" id="start-trading-btn" onclick="startAutoTrading()">Start Trading</button>
                <button class="stop-btn" id="stop-trading-btn" onclick="stopAutoTrading()" style="display:none">Stop</button>
                <button class="cancel-btn" onclick="cancelAllOrders()">Cancel All</button>
                <button id="dry-run-toggle" onclick="toggleDryRun()" style="padding:8px 12px;border-radius:6px;border:none;cursor:pointer;font-size:13px;font-weight:500;">DRY RUN</button>
                <button id="reset-halt-btn" onclick="resetHalt()" style="display:none;padding:8px 12px;border-radius:6px;border:none;cursor:pointer;background:var(--red);color:white;font-size:13px;">Reset Halt</button>
            </div>
            <div style="font-size:14px;font-weight:600;color:var(--text-2);margin-bottom:12px;">Open Positions</div>
            <div id="positions-container"><div class="empty"><p>No open positions</p></div></div>

            <div style="font-size:14px;font-weight:600;color:var(--text-2);margin:20px 0 12px;">Active Orders</div>
            <div class="table-container">
                <table>
                    <thead><tr>
                        <th>Market</th><th>Side</th>
                        <th class="text-right">Price</th><th class="text-right">Size</th>
                        <th class="text-right">Fair Value</th><th class="text-right">Age</th>
                    </tr></thead>
                    <tbody id="active-orders-table">
                        <tr><td colspan="6" class="text-muted" style="text-align:center">No active orders</td></tr>
                    </tbody>
                </table>
            </div>

            <div style="font-size:14px;font-weight:600;color:var(--text-2);margin:20px 0 12px;">Fill Statistics</div>
            <div class="fill-stats-panel">
                <div class="fill-stats-grid">
                    <div class="fill-stat"><div class="label">Total Orders</div><div class="value" id="fill-total">0</div></div>
                    <div class="fill-stat"><div class="label">Fill Rate</div><div class="value" id="fill-rate">--</div></div>
                    <div class="fill-stat"><div class="label">Avg Fill Time</div><div class="value" id="fill-time">--</div></div>
                    <div class="fill-stat"><div class="label">Adverse Selection</div><div class="value" id="adverse-selection">--</div></div>
                </div>
                <div id="fill-rates-by-spread" style="margin-top:12px;font-size:12px;color:var(--text-2);"></div>
            </div>
        </section>

        <!-- SETTINGS TAB -->
        <section class="section" id="section-settings">
            <div class="config-panel">
                <h4>Trading Config</h4>
                <div style="font-size:11px;color:var(--text-3);margin-bottom:12px;">Risk management</div>
                <div class="config-grid">
                    <div class="config-item"><label>Daily Loss Limit</label><input type="number" id="cfg-daily-loss" value="5" step="0.5" min="1" max="20"> %</div>
                    <div class="config-item"><label>Max Drawdown</label><input type="number" id="cfg-max-drawdown" value="15" step="1" min="5" max="50"> %</div>
                    <div class="config-item"><label>Max Exposure</label><input type="number" id="cfg-max-exposure" value="80" step="5" min="10" max="100"> %</div>
                </div>
                <div style="font-size:11px;color:var(--text-3);margin:12px 0 8px;">Strategy-specific</div>
                <div class="config-grid">
                    <div class="config-item"><label>Fin Min Edge</label><input type="number" id="cfg-fin-min-edge" value="2" step="0.5" min="0.5" max="20"> %</div>
                    <div class="config-item"><label>Fin Position Size</label><input type="number" id="cfg-fin-position-size" value="10" step="5" min="1" max="100"></div>
                    <div class="config-item"><label>Max Orders</label><input type="number" id="cfg-max-orders" value="20" step="5" min="5" max="200"></div>
                </div>
                <button class="save-config-btn" onclick="saveConfig()">Save Config</button>
            </div>
        </section>
    </div>

    <script>
    // ---- STATE ----
    let lastDataTime = Date.now();
    let cachedConfig = null;
    let dataIntervalId = null;
    let statusIntervalId = null;
    let freshnessIntervalId = null;

    const ASSET_MAP = [
        ['nasdaq', 'above', 'NDX', 'NDX Above'],
        ['nasdaq', 'range', 'NDX', 'NDX Range'],
        ['spx', 'above', 'SPX', 'SPX Above'],
        ['spx', 'range', 'SPX', 'SPX Range'],
        ['treasury10y', 'markets', '10Y', '10Y Treasury'],
        ['usdjpy', 'markets', 'JPY', 'USD/JPY'],
        ['eurusd', 'markets', 'EUR', 'EUR/USD'],
        ['wti', 'markets', 'WTI', 'WTI Oil'],
        ['bitcoin', 'markets', 'BTC', 'Bitcoin'],
        ['ethereum', 'markets', 'ETH', 'Ethereum'],
        ['solana', 'markets', 'SOL', 'Solana'],
        ['dogecoin', 'markets', 'DOGE', 'Dogecoin'],
        ['xrp', 'markets', 'XRP', 'XRP'],
    ];

    const ERROR_COLORS = {
        'network':'#f97316','api_rejection':'#ef4444','rate_limit':'#eab308',
        'order_failure':'#a855f7','data_quality':'#3b82f6','unknown':'#71717a',
    };
    const ERROR_THRESHOLDS = {'network':10,'rate_limit':5,'order_failure':30};

    // ---- UTILS ----
    function esc(s) {
        if (s == null) return '';
        const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML;
    }
    function fmt(n, decimals=2) {
        if (n == null) return '-';
        return n.toLocaleString('en-US',{minimumFractionDigits:decimals,maximumFractionDigits:decimals});
    }
    function pct(n) { return n == null ? '-' : (n*100).toFixed(1)+'%'; }
    function edgeBadge(edge, side) {
        if (edge == null || edge < 0.01) return '<span class="text-muted">-</span>';
        return `<span class="edge positive">${side} +${(edge*100).toFixed(1)}%</span>`;
    }
    function ageClass(s) {
        if (s > 120) return 'age-expired';
        if (s > 60) return 'age-critical';
        if (s > 30) return 'age-stale';
        return 'age-normal';
    }
    function formatExpiry(ct) {
        if (!ct) return '';
        const d = new Date(ct) - new Date();
        if (d <= 0) return 'Exp';
        const h = Math.floor(d/3600000), m = Math.floor((d%3600000)/60000);
        if (h < 24) return h > 0 ? h+'h '+m+'m' : m+'m';
        return new Date(ct).toLocaleDateString('en-US',{month:'short',day:'numeric'});
    }

    function computeAction(m) {
        if (m.recommendation) {
            const r = m.recommendation;
            return `<span class="text-green">${r.side} ${r.size}x @ ${r.price}c</span>`;
        }
        if (m.mm_suggestion) {
            const mm = m.mm_suggestion;
            return `<span class="text-blue">MM: ${mm.side} @ ${mm.price}c</span>`;
        }
        if (m.best_edge && m.best_edge >= 0.02) {
            const fy = Math.round(m.fair_value*100), fn = 100-fy;
            if (m.best_side==='YES' && fy > 2) {
                const ya = m.yes_ask||0;
                const bp = ya>0&&fy>ya ? Math.max(2,ya-1) : Math.max(2,Math.min(fy-1,(m.yes_bid||0)+1));
                return `<span class="text-green">Bid YES @ ${bp}c</span>`;
            }
            if (m.best_side==='NO' && fn > 2) {
                const na = m.yes_bid?(100-m.yes_bid):0, nb = m.yes_ask?(100-m.yes_ask):0;
                const bp = na>0&&fn>na ? Math.max(2,na-1) : Math.max(2,Math.min(fn-1,(nb||0)+1));
                return `<span class="text-green">Bid NO @ ${bp}c</span>`;
            }
        }
        return `${m.yes_bid||'-'}c / ${m.yes_ask||'-'}c`;
    }

    // ---- TABS ----
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById('section-'+tab.dataset.tab).classList.add('active');
        });
    });

    // ---- RISK BAR ----
    function updateRiskBar(d) {
        document.getElementById('rb-balance').textContent = '$'+fmt(d.balance||0);

        const pnl = d.daily_pnl||0, pp = d.daily_pnl_pct||0;
        const pe = document.getElementById('rb-daily-pnl');
        pe.textContent = `$${fmt(pnl)} (${pp>=0?'+':''}${pp.toFixed(1)}%)`;
        pe.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';

        const sb = d.starting_balance||d.balance||1;
        const mlp = cachedConfig ? cachedConfig.max_daily_loss_pct : 0.05;
        const ml = sb * mlp;
        const pr = ml > 0 ? Math.min(Math.abs(pnl)/ml,1) : 0;
        const pg = document.getElementById('rb-pnl-gauge');
        pg.style.width = (pr*100)+'%';
        pg.className = 'gauge-fill '+(pnl>=0?'ok':(pr>0.8?'danger':pr>0.5?'warn':'ok'));

        const cp = d.capital_pct_used||0;
        const me = cachedConfig ? cachedConfig.max_total_exposure : 0.8;
        const er = me > 0 ? Math.min((cp/100)/me,1) : 0;
        document.getElementById('rb-exposure').textContent = cp.toFixed(1)+'%';
        const eg = document.getElementById('rb-exposure-gauge');
        eg.style.width = (er*100)+'%';
        eg.className = 'gauge-fill '+(er>0.8?'danger':er>0.5?'warn':'ok');

        const se = document.getElementById('rb-status');
        if (d.halted) {
            se.innerHTML = `<span class="status-dot halted"></span> HALTED: ${esc(d.halt_reason||'Unknown')}`;
            se.style.color = 'var(--red)';
        } else if (d.enabled) {
            se.innerHTML = `<span class="status-dot running"></span> Running (${d.active_orders||0} orders)`;
            se.style.color = '';
        } else {
            se.innerHTML = `<span class="status-dot stopped"></span> Stopped`;
            se.style.color = '';
        }

        const mb = document.getElementById('rb-mode');
        if (d.dry_run) {
            mb.textContent = 'DRY RUN'; mb.className = 'mode-badge dry-run';
            document.body.classList.add('dry-run');
        } else {
            mb.textContent = 'LIVE'; mb.className = 'mode-badge live';
            document.body.classList.remove('dry-run');
        }

        const errs = d.errors||{}, ec = errs.total_in_window||0;
        const ee = document.getElementById('rb-error-count');
        ee.textContent = ec;
        ee.className = ec > 0 ? 'error-count has-errors' : 'error-count';
        if (ec === 0) document.getElementById('error-panel').classList.remove('open');
        updateErrorPanel(errs);
    }

    // ---- ERROR PANEL ----
    function toggleErrorPanel() { document.getElementById('error-panel').classList.toggle('open'); }
    function updateErrorPanel(errors) {
        const bt = errors.by_type||{}, rc = errors.recent||[], tot = errors.total_in_window||0;
        const bar = document.getElementById('error-bar');
        bar.innerHTML = tot > 0 ? Object.entries(bt).map(([t,c]) => {
            const p = (c/tot)*100, cl = ERROR_COLORS[t]||ERROR_COLORS['unknown'];
            return `<div class="error-bar-segment" style="width:${p}%;background:${cl};" title="${t}: ${c}"></div>`;
        }).join('') : '';

        document.getElementById('error-thresholds').innerHTML = Object.entries(bt).map(([t,c]) => {
            const mx = ERROR_THRESHOLDS[t];
            if (!mx) return `${t}: ${c}`;
            const cl = c>=mx*0.8?'var(--red)':c>=mx*0.5?'var(--orange)':'var(--text-3)';
            return `<span style="color:${cl}">${t}: ${c}/${mx}</span>`;
        }).join(' &middot; ');

        const le = document.getElementById('error-list');
        le.innerHTML = rc.length > 0 ? rc.map(e => {
            const tm = new Date(e.time).toLocaleTimeString();
            const cl = ERROR_COLORS[e.type]||ERROR_COLORS['unknown'];
            return `<li><span style="color:var(--text-3);min-width:65px;">${tm}</span>` +
                `<span class="error-type-badge" style="background:${cl}22;color:${cl};">${e.type}</span>` +
                `<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(e.message)}</span></li>`;
        }).join('') : '<li style="color:var(--text-3);">No recent errors</li>';
    }

    // ---- KILL SWITCH ----
    async function killSwitch() {
        if (!confirm('KILL SWITCH: Stop trading AND cancel ALL resting orders?')) return;
        try {
            await fetch('/api/auto-trader/stop',{method:'POST'});
            const r = await fetch('/api/auto-trader/cancel-all',{method:'POST'});
            const d = await r.json();
            alert('Stopped. Cancelled '+d.cancelled+' orders.');
            loadAutoTraderStatus();
        } catch(e) { alert('Kill switch error: '+e); }
    }

    // ---- POSITIONS ----
    function renderPositions(positions) {
        const c = document.getElementById('positions-container');
        if (!positions || !positions.length) {
            c.innerHTML = '<div class="empty"><p>No open positions</p></div>'; return;
        }
        c.innerHTML = positions.map(p => `
            <div class="position-card">
                <div class="position-header">
                    <div><div class="position-title">${esc(p.title)}</div>
                    <div class="position-ticker">${esc(p.ticker)}</div></div>
                    <div class="position-side ${p.side.toLowerCase()}">${p.side} x ${p.quantity}</div>
                </div>
                <div class="position-grid">
                    <div class="position-metric"><div class="position-metric-label">Entry</div>
                        <div class="position-metric-value">${pct(p.avg_price)}</div></div>
                    <div class="position-metric"><div class="position-metric-label">Market</div>
                        <div class="position-metric-value">${pct(p.current_mid)}</div></div>
                    <div class="position-metric"><div class="position-metric-label">Model</div>
                        <div class="position-metric-value">${pct(p.fair_value)}</div></div>
                    <div class="position-metric"><div class="position-metric-label">P&L</div>
                        <div class="position-metric-value ${p.unrealized_pnl>=0?'text-green':'text-red'}">$${fmt(p.unrealized_pnl)}</div></div>
                </div>
            </div>`).join('');
    }

    // ---- OPPORTUNITIES ----
    function renderOpportunities(all) {
        const tb = document.getElementById('opportunities-table');
        const f = all.filter(m => m.best_edge >= 0.01).sort((a,b) => (b.best_edge||0)-(a.best_edge||0));
        if (!f.length) {
            tb.innerHTML = '<tr><td colspan="8" class="text-muted" style="text-align:center;padding:32px;">No opportunities with edge &ge; 1%</td></tr>';
            return;
        }
        tb.innerHTML = f.slice(0,50).map(m => {
            const st = m.title&&m.title.length>40 ? m.title.slice(0,37)+'...' : (m.title||m.ticker);
            return `<tr class="clickable" onclick="showOrderbook('${m.ticker}')" title="${esc(m.title||m.ticker)}">
                <td><span class="asset-badge">${m.asset}</span></td>
                <td><span class="mono">${esc(m.range)}</span>
                    <div style="font-size:10px;color:var(--text-3);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(st)}</div></td>
                <td style="font-size:12px;color:var(--text-2);">${formatExpiry(m.close_time)}</td>
                <td class="text-right mono">${pct(m.market_price)}</td>
                <td class="text-right mono">${pct(m.fair_value)}</td>
                <td class="text-right">${edgeBadge(m.best_edge,m.best_side)}</td>
                <td class="text-right mono" style="font-size:12px;">${computeAction(m)}</td>
                <td class="text-right text-muted">${m.volume||0}</td>
            </tr>`;
        }).join('');
    }

    // ---- ACCORDIONS ----
    const SECTIONS = [
        {k:'nasdaq',s:'above',l:'NDX Above',pf:'futures'},
        {k:'nasdaq',s:'range',l:'NDX Range',pf:'futures'},
        {k:'spx',s:'above',l:'SPX Above',pf:'futures'},
        {k:'spx',s:'range',l:'SPX Range',pf:'futures'},
        {k:'treasury10y',s:'markets',l:'10Y Treasury',pf:'quote'},
        {k:'usdjpy',s:'markets',l:'USD/JPY',pf:'quote'},
        {k:'eurusd',s:'markets',l:'EUR/USD',pf:'quote'},
        {k:'wti',s:'markets',l:'WTI Oil',pf:'quote'},
        {k:'bitcoin',s:'markets',l:'Bitcoin',pf:'quote'},
        {k:'ethereum',s:'markets',l:'Ethereum',pf:'quote'},
        {k:'solana',s:'markets',l:'Solana',pf:'quote'},
        {k:'dogecoin',s:'markets',l:'Dogecoin',pf:'quote'},
        {k:'xrp',s:'markets',l:'XRP',pf:'quote'},
    ];

    function renderAccordions(data) {
        const c = document.getElementById('asset-accordions');
        c.innerHTML = SECTIONS.map((s,i) => {
            const d = data[s.k]; if (!d) return '';
            const mkts = d[s.s]||[];
            const ec = mkts.filter(m => m.best_edge>=0.01).length;
            const q = d[s.pf];
            const price = q?.price||q?.yield;
            const ch = q?.change_pct||q?.change||0;
            let ps = price!=null ? (price>1000?fmt(price,2):price.toFixed(price<10?4:3)) : '';
            return `<details class="accordion-section" id="acc-${i}">
                <summary><span>${s.l}</span>
                    ${ps ? `<span class="futures-inline"><span class="fp">${ps}</span><span class="fc ${ch>=0?'text-green':'text-red'}">${ch>=0?'+':''}${typeof ch==='number'?ch.toFixed(2):ch}%</span></span>` : ''}
                    <span style="margin-left:auto;font-size:12px;color:var(--text-3);">${mkts.length} mkts${ec>0?', '+ec+' edge':''}</span>
                </summary>
                <div class="accordion-content"><table>
                    <thead><tr><th>Strike/Range</th><th class="text-right">Market</th><th class="text-right">Model</th><th class="text-right">Edge</th><th class="text-right">Action</th></tr></thead>
                    <tbody id="acc-tb-${i}"></tbody>
                </table></div>
            </details>`;
        }).join('');

        SECTIONS.forEach((s,i) => {
            const d = data[s.k]; if (!d) return;
            const tb = document.getElementById('acc-tb-'+i);
            if (tb) renderMarketsInTable(d[s.s]||[],tb);
        });
    }

    function renderMarketsInTable(mkts,tb) {
        if (!mkts||!mkts.length) { tb.innerHTML='<tr><td colspan="5" class="text-muted" style="text-align:center">No markets</td></tr>'; return; }
        mkts.sort((a,b) => Math.abs(a.distance)-Math.abs(b.distance));
        tb.innerHTML = mkts.slice(0,25).map(m => {
            const he = m.best_edge&&m.best_edge>=0.02;
            const st = m.title&&m.title.length>50?m.title.slice(0,47)+'...':(m.title||m.ticker);
            const ex = formatExpiry(m.close_time);
            return `<tr class="${he?'clickable':''}" ${he?`onclick="showOrderbook('${m.ticker}')"`:''} title="${esc(m.title||m.ticker)}">
                <td><span class="mono">${esc(m.range)}</span>
                    <div style="font-size:10px;color:var(--text-3);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(st)}${ex?' &middot; '+ex:''}</div></td>
                <td class="text-right mono">${pct(m.market_price)}</td>
                <td class="text-right mono">${pct(m.fair_value)}</td>
                <td class="text-right">${edgeBadge(m.best_edge,m.best_side)}</td>
                <td class="text-right mono" style="font-size:12px;">${computeAction(m)}</td>
            </tr>`;
        }).join('');
    }

    // ---- ORDERBOOK MODAL ----
    async function showOrderbook(ticker) {
        try {
            const r = await fetch('/api/orderbook/'+ticker);
            const d = await r.json();
            if (d.error) { console.error(d.error); return; }
            const mt = d.market?.title||ticker;
            const ct = d.market?.close_time ? new Date(d.market.close_time).toLocaleString() : '';
            let h = `<div class="modal-overlay" onclick="closeModal()">
                <div class="modal" onclick="event.stopPropagation()">
                <div class="modal-header"><h3 style="font-size:14px;line-height:1.3;">${mt}</h3>
                    <button onclick="closeModal()" class="modal-close">&times;</button></div>
                ${ct?`<div style="padding:0 16px;font-size:11px;color:var(--text-3);">Expires: ${ct}</div>`:''}
                <div class="modal-body"><div class="ob-section"><h4>Orderbook</h4>
                <div class="ob-grid"><div><div class="ob-header">BIDS (YES)</div>
                ${d.orderbook.yes_bids.map(l=>`<div class="ob-level bid">${l.price}c <span class="qty">${l.qty}</span></div>`).join('')}
                </div><div><div class="ob-header">ASKS (YES)</div>
                ${d.orderbook.yes_asks.map(l=>`<div class="ob-level ask">${l.price}c <span class="qty">${l.qty}</span></div>`).join('')}
                </div></div>
                <div class="ob-stats">Spread: ${d.orderbook.spread}c | Mid: ${fmt(d.orderbook.mid,1)}c</div></div>`;
            if (d.recommendation) {
                const rc = d.recommendation;
                h += `<div class="rec-section"><h4>Recommendation</h4><div class="rec-card">
                    <div class="rec-action">Buy ${rc.size} ${rc.side} @ ${rc.price}c</div>
                    <div class="rec-stats">Edge: ${(rc.edge*100).toFixed(1)}% | Fill: ${(rc.fill_prob*100).toFixed(0)}% | EV: $${rc.expected_value.toFixed(2)}</div>
                    <div class="rec-reasoning">${rc.reasoning}</div></div></div>`;
            }
            h += '</div></div></div>';
            document.body.insertAdjacentHTML('beforeend', h);
        } catch(e) { console.error('Failed to load orderbook:',e); }
    }
    function closeModal() { const m=document.querySelector('.modal-overlay'); if(m)m.remove(); }

    // ---- AUTO-TRADER ----
    async function loadAutoTraderStatus() {
        try {
            const r = await fetch('/api/auto-trader/status');
            const d = await r.json();
            updateRiskBar(d);

            const startBtn=document.getElementById('start-trading-btn');
            const stopBtn=document.getElementById('stop-trading-btn');
            const resetBtn=document.getElementById('reset-halt-btn');
            const dryBtn=document.getElementById('dry-run-toggle');

            if (d.dry_run) {
                dryBtn.textContent='GO LIVE'; dryBtn.style.background='var(--green)'; dryBtn.style.color='white';
            } else {
                dryBtn.textContent='DRY RUN'; dryBtn.style.background='var(--bg-3)'; dryBtn.style.color='var(--text-1)';
            }

            if (d.halted) {
                startBtn.style.display='none'; stopBtn.style.display='none'; resetBtn.style.display='inline-block';
            } else if (d.enabled) {
                startBtn.style.display='none'; stopBtn.style.display='block'; resetBtn.style.display='none';
            } else {
                startBtn.style.display='block'; stopBtn.style.display='none'; resetBtn.style.display='none';
            }

            // Active orders with age colors
            const tb = document.getElementById('active-orders-table');
            if (!d.orders||!d.orders.length) {
                tb.innerHTML='<tr><td colspan="6" class="text-muted" style="text-align:center">No active orders</td></tr>';
            } else {
                tb.innerHTML = d.orders.map(o => {
                    const st = o.title&&o.title.length>50?o.title.slice(0,47)+'...':(o.title||o.ticker);
                    const ag = Math.round(o.age_seconds);
                    return `<tr title="${esc(o.title||o.ticker)}">
                        <td><div class="mono" style="font-size:10px;color:var(--text-3);">${esc(o.ticker.substring(0,25))}...</div>
                            <div style="font-size:11px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(st)}</div></td>
                        <td><span class="edge positive">${o.side.toUpperCase()}</span></td>
                        <td class="text-right mono">${o.price}c</td>
                        <td class="text-right">${o.size}</td>
                        <td class="text-right">${(o.fair_value*100).toFixed(1)}%</td>
                        <td class="text-right ${ageClass(ag)}">${ag}s</td>
                    </tr>`;
                }).join('');
            }

            if (d.fill_stats) {
                const fs = d.fill_stats;
                document.getElementById('fill-total').textContent = fs.total_orders||0;
                document.getElementById('fill-rate').textContent = fs.total_orders>0?(fs.fill_rate*100).toFixed(1)+'%':'--';
                document.getElementById('fill-time').textContent = fs.avg_time_to_fill>0?Math.round(fs.avg_time_to_fill)+'s':'--';
                document.getElementById('adverse-selection').textContent = fs.total_orders>0?(fs.avg_adverse_selection*100).toFixed(2)+'%':'--';
                const sr = fs.fill_rate_by_spread||{};
                const se = Object.entries(sr).sort((a,b)=>parseInt(a[0])-parseInt(b[0]));
                const el = document.getElementById('fill-rates-by-spread');
                if (se.length>0&&fs.total_orders>=10) {
                    el.innerHTML = '<span style="color:var(--text-3);">Learned rates:</span> '+
                        se.map(([s,r])=>`Spread &le;${s}c: <strong>${(r*100).toFixed(0)}%</strong>`).join(' | ');
                } else {
                    el.innerHTML = '<span style="color:var(--text-3);">Need 10+ orders for learned rates</span>';
                }
            }
        } catch(e) { console.error('Failed to load auto-trader status:',e); }
    }

    async function startAutoTrading() {
        try { await fetch('/api/auto-trader/start',{method:'POST'}); loadAutoTraderStatus(); }
        catch(e) { alert('Failed to start: '+e); }
    }
    async function stopAutoTrading() {
        try { await fetch('/api/auto-trader/stop',{method:'POST'}); loadAutoTraderStatus(); }
        catch(e) { alert('Failed to stop: '+e); }
    }
    async function cancelAllOrders() {
        try {
            const r = await fetch('/api/auto-trader/cancel-all',{method:'POST'});
            const d = await r.json(); alert('Cancelled '+d.cancelled+' orders'); loadAutoTraderStatus();
        } catch(e) { alert('Failed to cancel: '+e); }
    }
    async function toggleDryRun() {
        try {
            const sr = await fetch('/api/auto-trader/status'); const st = await sr.json();
            if (st.dry_run && !confirm('You are switching to LIVE mode. Real orders will be placed.')) return;
            const r = await fetch('/api/auto-trader/dry-run',{
                method:'POST',headers:{'Content-Type':'application/json'},
                body:JSON.stringify({enabled:!st.dry_run})
            });
            const d = await r.json(); alert(d.message); loadAutoTraderStatus();
        } catch(e) { alert('Failed to toggle mode: '+e); }
    }
    async function resetHalt() {
        try {
            const r = await fetch('/api/auto-trader/reset-halt',{method:'POST'});
            const d = await r.json();
            if (d.error) { alert('Failed: '+d.error); return; }
            loadAutoTraderStatus();
        } catch(e) { alert('Failed to reset: '+e); }
    }

    // ---- CONFIG ----
    async function saveConfig() {
        try {
            const cfg = {
                max_daily_loss_pct: parseFloat(document.getElementById('cfg-daily-loss').value)/100,
                max_drawdown_pct: parseFloat(document.getElementById('cfg-max-drawdown').value)/100,
                max_total_exposure: parseFloat(document.getElementById('cfg-max-exposure').value)/100,
                financial_min_edge: parseFloat(document.getElementById('cfg-fin-min-edge').value)/100,
                financial_position_size: parseInt(document.getElementById('cfg-fin-position-size').value),
                max_total_orders: parseInt(document.getElementById('cfg-max-orders').value),
            };
            const r = await fetch('/api/trading-config',{
                method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)
            });
            const d = await r.json();
            if (d.error) alert('Error: '+d.error);
            else { alert('Config saved'); cachedConfig = cfg; }
        } catch(e) { alert('Failed to save: '+e); }
    }
    async function loadConfig() {
        try {
            const r = await fetch('/api/trading-config');
            cachedConfig = await r.json();
            document.getElementById('cfg-daily-loss').value = (cachedConfig.max_daily_loss_pct*100).toFixed(1);
            document.getElementById('cfg-max-drawdown').value = (cachedConfig.max_drawdown_pct*100).toFixed(0);
            document.getElementById('cfg-max-exposure').value = (cachedConfig.max_total_exposure*100).toFixed(0);
            document.getElementById('cfg-fin-min-edge').value = (cachedConfig.financial_min_edge*100).toFixed(1);
            document.getElementById('cfg-fin-position-size').value = cachedConfig.financial_position_size;
            document.getElementById('cfg-max-orders').value = cachedConfig.max_total_orders||20;
        } catch(e) {}
    }

    // ---- MAIN DATA LOAD ----
    async function loadData() {
        try {
            const r = await fetch('/api/data');
            const data = await r.json();
            lastDataTime = Date.now();

            const allMarkets = [];
            for (const [ak,sk,sl] of ASSET_MAP) {
                const d = data[ak]; if (!d) continue;
                for (const m of (d[sk]||[])) allMarkets.push({...m, asset: sl});
            }
            renderOpportunities(allMarkets);
            renderPositions(data.positions);
            renderAccordions(data);
        } catch(e) { console.error('Failed to load data:',e); }
    }

    // ---- FRESHNESS ----
    function updateFreshness() {
        const age = Math.floor((Date.now()-lastDataTime)/1000);
        const el = document.getElementById('data-age');
        if (el) {
            if (age < 3) { el.textContent='LIVE'; el.className='data-age live'; }
            else { el.textContent=age+'s ago'; el.className='data-age stale'; }
        }
    }

    // ---- VISIBILITY API ----
    function startPolling() {
        if (!dataIntervalId) dataIntervalId = setInterval(loadData, 1000);
        if (!statusIntervalId) statusIntervalId = setInterval(loadAutoTraderStatus, 2000);
        if (!freshnessIntervalId) freshnessIntervalId = setInterval(updateFreshness, 500);
    }
    function stopPolling() {
        if (dataIntervalId) { clearInterval(dataIntervalId); dataIntervalId=null; }
        if (statusIntervalId) { clearInterval(statusIntervalId); statusIntervalId=null; }
        if (freshnessIntervalId) { clearInterval(freshnessIntervalId); freshnessIntervalId=null; }
    }
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) stopPolling();
        else { loadData(); loadAutoTraderStatus(); startPolling(); }
    });

    // ---- INIT ----
    loadData();
    loadAutoTraderStatus();
    loadConfig();
    startPolling();
    </script>
</body>
</html>
'''


if __name__ == '__main__':
    import os
    app = create_app()
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=5050, debug=debug_mode, use_reloader=debug_mode)
