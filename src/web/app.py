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
                usdjpy_quote = state["futures_quotes"].get("usdjpy")
                if usdjpy_quote and usdjpy_quote.price:
                    usdjpy_price = usdjpy_quote.price

                    usdjpy_markets = []
                    jpy_markets = state["client"].get_markets_by_series_prefix("KXUSDJPY", status="open")
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
                    eur_markets = state["client"].get_markets_by_series_prefix("KXEURUSD", status="open")
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
                            if wti_price and m.lower_bound is not None:
                                if m.upper_bound is not None and m.upper_bound != float('inf'):
                                    fv_result = fv_model.calculate(
                                        current_price=wti_price,
                                        lower_bound=m.lower_bound,
                                        upper_bound=m.upper_bound,
                                        hours_to_expiry=hours,
                                        index="wti",
                                        close_time=m.close_time,
                                    )
                                elif m.upper_bound is None or m.upper_bound == float('inf'):
                                    fv_result = fv_model.calculate(
                                        current_price=wti_price,
                                        lower_bound=m.lower_bound,
                                        upper_bound=float('inf'),
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
                                if coin_price and m.lower_bound is not None:
                                    if m.upper_bound is not None and m.upper_bound != float('inf'):
                                        fv_result = fv_model.calculate(
                                            current_price=coin_price,
                                            lower_bound=m.lower_bound,
                                            upper_bound=m.upper_bound,
                                            hours_to_expiry=hours,
                                            index=coin_key,
                                            close_time=m.close_time,
                                        )
                                    elif m.upper_bound is None or m.upper_bound == float('inf'):
                                        fv_result = fv_model.calculate(
                                            current_price=coin_price,
                                            lower_bound=m.lower_bound,
                                            upper_bound=float('inf'),
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
                    # Only trade if we have fresh futures data (sanity check)
                    futures_ok = False
                    nq = state["futures_quotes"].get("nasdaq")
                    es = state["futures_quotes"].get("spx")
                    t10y = state["futures_quotes"].get("treasury10y")
                    usdjpy = state["futures_quotes"].get("usdjpy")
                    eurusd = state["futures_quotes"].get("eurusd")
                    wti = state["futures_quotes"].get("wti")
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
                    if wti and wti.price > 20:  # WTI should be ~$60+
                        futures_ok = True
                    btc = state["futures_quotes"].get("bitcoin")
                    if btc and btc.price > 1000:  # BTC should be ~$90000+
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
            <button class="tab" data-tab="wti">WTI Oil</button>
            <button class="tab" data-tab="bitcoin">BTC</button>
            <button class="tab" data-tab="ethereum">ETH</button>
            <button class="tab" data-tab="solana">SOL</button>
            <button class="tab" data-tab="dogecoin">DOGE</button>
            <button class="tab" data-tab="xrp">XRP</button>
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
                <div style="font-size: 11px; color: var(--text-3); margin-bottom: 12px;">Risk management</div>
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
                        <label>Max Orders</label>
                        <input type="number" id="cfg-max-orders" value="20" step="5" min="5" max="200">
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

        <!-- WTI Crude Oil Section -->
        <section class="section" id="section-wti">
            <div class="futures-badge">
                <span style="color: var(--text-3)">WTI Crude:</span>
                <span class="price" id="wti-price">-</span>
                <span class="change" id="wti-change">-</span>
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
                    <tbody id="wti-table">
                        <tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </section>

        <!-- Bitcoin Section -->
        <section class="section" id="section-bitcoin">
            <div class="futures-badge">
                <span style="color: var(--text-3)">Bitcoin:</span>
                <span class="price" id="bitcoin-price">-</span>
                <span class="change" id="bitcoin-change">-</span>
            </div>
            <div class="table-container">
                <table>
                    <thead><tr><th>Price Range</th><th class="text-right">Market (YES)</th><th class="text-right">Model (YES)</th><th class="text-right">Best Trade</th><th class="text-right">Action</th></tr></thead>
                    <tbody id="bitcoin-table"><tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr></tbody>
                </table>
            </div>
        </section>

        <!-- Ethereum Section -->
        <section class="section" id="section-ethereum">
            <div class="futures-badge">
                <span style="color: var(--text-3)">Ethereum:</span>
                <span class="price" id="ethereum-price">-</span>
                <span class="change" id="ethereum-change">-</span>
            </div>
            <div class="table-container">
                <table>
                    <thead><tr><th>Price Range</th><th class="text-right">Market (YES)</th><th class="text-right">Model (YES)</th><th class="text-right">Best Trade</th><th class="text-right">Action</th></tr></thead>
                    <tbody id="ethereum-table"><tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr></tbody>
                </table>
            </div>
        </section>

        <!-- Solana Section -->
        <section class="section" id="section-solana">
            <div class="futures-badge">
                <span style="color: var(--text-3)">Solana:</span>
                <span class="price" id="solana-price">-</span>
                <span class="change" id="solana-change">-</span>
            </div>
            <div class="table-container">
                <table>
                    <thead><tr><th>Price Range</th><th class="text-right">Market (YES)</th><th class="text-right">Model (YES)</th><th class="text-right">Best Trade</th><th class="text-right">Action</th></tr></thead>
                    <tbody id="solana-table"><tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr></tbody>
                </table>
            </div>
        </section>

        <!-- Dogecoin Section -->
        <section class="section" id="section-dogecoin">
            <div class="futures-badge">
                <span style="color: var(--text-3)">Dogecoin:</span>
                <span class="price" id="dogecoin-price">-</span>
                <span class="change" id="dogecoin-change">-</span>
            </div>
            <div class="table-container">
                <table>
                    <thead><tr><th>Price Range</th><th class="text-right">Market (YES)</th><th class="text-right">Model (YES)</th><th class="text-right">Best Trade</th><th class="text-right">Action</th></tr></thead>
                    <tbody id="dogecoin-table"><tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr></tbody>
                </table>
            </div>
        </section>

        <!-- XRP Section -->
        <section class="section" id="section-xrp">
            <div class="futures-badge">
                <span style="color: var(--text-3)">XRP:</span>
                <span class="price" id="xrp-price">-</span>
                <span class="change" id="xrp-change">-</span>
            </div>
            <div class="table-container">
                <table>
                    <thead><tr><th>Price Range</th><th class="text-right">Market (YES)</th><th class="text-right">Model (YES)</th><th class="text-right">Best Trade</th><th class="text-right">Action</th></tr></thead>
                    <tbody id="xrp-table"><tr><td colspan="5" class="loading"><div class="spinner"></div> Loading...</td></tr></tbody>
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
                    max_total_orders: parseInt(document.getElementById('cfg-max-orders').value),
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
                document.getElementById('cfg-max-orders').value = data.max_total_orders || 20;
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

                // Render WTI Crude Oil
                if (data.wti?.quote?.price) {
                    const priceEl = document.getElementById('wti-price');
                    priceEl.textContent = '$' + data.wti.quote.price.toFixed(2);
                    const change = data.wti.quote.change || 0;
                    const changeEl = document.getElementById('wti-change');
                    changeEl.textContent = (change >= 0 ? '+' : '') + change.toFixed(2);
                    changeEl.className = 'change ' + (change >= 0 ? 'text-green' : 'text-red');
                }
                renderMarkets(data.wti?.markets, 'wti-table', data.wti?.quote?.price);

                // Render Crypto coins
                const cryptoCoins = [
                    {key: 'bitcoin', decimals: 2},
                    {key: 'ethereum', decimals: 2},
                    {key: 'solana', decimals: 2},
                    {key: 'dogecoin', decimals: 4},
                    {key: 'xrp', decimals: 4},
                ];
                for (const coin of cryptoCoins) {
                    const d = data[coin.key];
                    if (d?.quote?.price) {
                        const priceEl = document.getElementById(coin.key + '-price');
                        priceEl.textContent = '$' + d.quote.price.toLocaleString('en-US', {minimumFractionDigits: coin.decimals, maximumFractionDigits: coin.decimals});
                        const change = d.quote.change || 0;
                        const changeEl = document.getElementById(coin.key + '-change');
                        changeEl.textContent = (change >= 0 ? '+' : '') + change.toFixed(coin.decimals);
                        changeEl.className = 'change ' + (change >= 0 ? 'text-green' : 'text-red');
                    }
                    renderMarkets(d?.markets, coin.key + '-table', d?.quote?.price);
                }

            } catch (err) {
                console.error('Failed to load data:', err);
            }
        }

        // Initial load and fast refresh
        loadData();
        loadTradingPlan();
        loadAutoTraderStatus();
        loadConfig();
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

    </script>
</body>
</html>
'''


if __name__ == '__main__':
    import os
    app = create_app()
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=5050, debug=debug_mode, use_reloader=debug_mode)
