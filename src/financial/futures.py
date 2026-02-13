"""
Market data client using InsightSentry (real-time) via RapidAPI
+ Binance (real-time crypto) via public API

IMPORTANT: Kalshi markets settle to CASH INDICES, not futures!
- NASDAQ-100 markets settle to NDX (InsightSentry: NASDAQ:NDX)
- S&P 500 markets settle to SPX (InsightSentry: CBOE:SPX)
- Crypto markets settle to CF Benchmarks Real-Time Index

Primary: InsightSentry REST API — all symbols in ONE batch call, 0s delay
         Binance REST API — crypto prices (free, no auth)
Fallback: Yahoo Finance for historical volatility calculation
"""

import json
import math
import os
import threading
import time
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from ..core.models import FuturesQuote

# US Eastern timezone for market hours detection
ET = ZoneInfo("America/New_York")

INSIGHTSENTRY_BASE = "https://insightsentry.p.rapidapi.com"
BINANCE_BASE = "https://api.binance.com"


class FuturesClient:
    """Fetch real-time market data via InsightSentry (RapidAPI)"""

    # InsightSentry symbol mapping — all available in one batch call
    IS_TICKERS = {
        "nasdaq": "NASDAQ:NDX",      # NASDAQ-100 Cash Index (SETTLEMENT)
        "spx": "CBOE:SPX",           # S&P 500 Cash Index (SETTLEMENT)
        "vix": "CBOE:VIX",           # CBOE Volatility Index
        "treasury10y": "IS:TNX",     # 10-Year Treasury Yield
        "usdjpy": "FX:USDJPY",       # USD/JPY
        "eurusd": "FX:EURUSD",       # EUR/USD
        "wti": "NYMEX:CL1!",        # WTI Crude Oil Front Month
    }

    # Binance symbols for crypto (free public API, no auth needed)
    BINANCE_TICKERS = {
        "bitcoin": "BTCUSDT",
        "ethereum": "ETHUSDT",
        "solana": "SOLUSDT",
        "dogecoin": "DOGEUSDT",
        "xrp": "XRPUSDT",
    }

    # InsightSentry futures symbols (for overnight basis adjustment)
    IS_FUTURES = {
        "nasdaq": "CME_MINI:NQ1!",   # E-mini NASDAQ-100 Futures
        "spx": "CME_MINI:ES1!",      # E-mini S&P 500 Futures
    }

    # Yahoo Finance symbols for historical volatility only
    HIST_SYMBOLS = {
        "nasdaq": "^NDX",
        "spx": "^GSPC",
        "treasury10y": "^TNX",
        "usdjpy": "USDJPY=X",
        "eurusd": "EURUSD=X",
        "wti": "CL=F",
        "bitcoin": "BTC-USD",
        "ethereum": "ETH-USD",
        "solana": "SOL-USD",
        "dogecoin": "DOGE-USD",
        "xrp": "XRP-USD",
    }

    # Legacy aliases (backward compatibility)
    FUTURES_SYMBOLS = {"nasdaq": "NQ=F", "spx": "ES=F"}
    INDEX_SYMBOLS = IS_TICKERS
    SYMBOLS = IS_TICKERS
    POLYGON_TICKERS = IS_TICKERS
    TD_TICKERS = IS_TICKERS

    _vol_cache_ttl = 3600  # 1 hour

    def __init__(self, cache_ttl: float = 5.0, polygon_api_key: str = None):
        """
        Initialize market data client.

        Args:
            cache_ttl: How long to cache quotes (default 5s)
            polygon_api_key: Ignored (legacy param). Uses INSIGHTSENTRY_API_KEY.
        """
        self._vol_cache = {}
        self._cache = {}
        self._cache_ttl = cache_ttl
        self._cache_lock = threading.Lock()

        # Resolve InsightSentry API key: env var > credentials.py
        self._api_key = os.environ.get("INSIGHTSENTRY_API_KEY", "")
        if not self._api_key:
            try:
                import credentials
                self._api_key = getattr(credentials, "INSIGHTSENTRY_API_KEY", "")
            except ImportError:
                pass

        if not self._api_key:
            print("[MARKET DATA] WARNING: No InsightSentry API key found!")
            print("[MARKET DATA] Set INSIGHTSENTRY_API_KEY env var or add to credentials.py")

        # Rate limiting
        self._last_request_time = 0
        self._min_request_interval = 1.0  # 1 req/sec
        self._rate_limit_lock = threading.Lock()

        # Exponential backoff
        self._backoff_until = 0
        self._backoff_duration = 10
        self._max_backoff = 300
        self._base_backoff = 10

        # Last known basis (futures - spot) for overnight adjustments
        self._last_basis: dict[str, float] = {}
        self._last_basis_time: dict[str, datetime] = {}

        # HTTP session for connection pooling
        self._session = requests.Session()
        self._session.headers.update({
            "x-rapidapi-host": "insightsentry.p.rapidapi.com",
            "x-rapidapi-key": self._api_key,
        })

        key_display = "configured" if self._api_key else "not configured"
        print(f"[MARKET DATA] InsightSentry initialized (key={key_display})")

    # ========================================================================
    # InsightSentry API
    # ========================================================================

    def _is_get(self, path: str, params: dict = None) -> Optional[dict]:
        """Make GET request to InsightSentry via RapidAPI."""
        if not self._api_key:
            return None
        if self._is_rate_limited():
            return None

        with self._rate_limit_lock:
            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < self._min_request_interval:
                time.sleep(self._min_request_interval - elapsed)
            self._last_request_time = time.time()

        try:
            resp = self._session.get(
                f"{INSIGHTSENTRY_BASE}{path}",
                params=params or {},
                timeout=8,
            )
            if resp.status_code == 429:
                self._trigger_backoff()
                return None
            if resp.status_code == 403:
                print(f"[INSIGHTSENTRY] 403 Forbidden — check your RapidAPI subscription")
                return None
            resp.raise_for_status()
            self._reset_backoff()
            return resp.json()
        except requests.exceptions.Timeout:
            print("[INSIGHTSENTRY] Request timeout")
            return None
        except requests.exceptions.RequestException as e:
            print(f"[INSIGHTSENTRY] Error: {e}")
            return None

    def _fetch_batch_quotes(self, codes: list[str]) -> dict[str, dict]:
        """
        Fetch multiple symbols in ONE API call.
        Returns {InsightSentry_code: quote_data_dict}.
        """
        if not codes:
            return {}

        codes_str = ",".join(codes)
        data = self._is_get("/v3/symbols/quotes", {"codes": codes_str})

        if not data or "data" not in data:
            return {}

        result = {}
        for item in data["data"]:
            code = item.get("code", "")
            if code:
                result[code] = item
        return result

    def _fetch_all_realtime(self) -> dict[str, FuturesQuote]:
        """Fetch all index + forex quotes in a single API call."""
        # Always fetch both cash indices AND futures so we can compute basis
        all_codes = list(self.IS_TICKERS.values()) + list(self.IS_FUTURES.values())

        raw = self._fetch_batch_quotes(all_codes)
        if not raw:
            return {}

        quotes = {}
        futures_prices = {}  # internal_key -> price (for basis computation)
        cash_prices = {}     # internal_key -> price

        # Reverse map: IS code -> our internal key
        reverse_index = {v: k for k, v in self.IS_TICKERS.items()}
        reverse_futures = {v: k for k, v in self.IS_FUTURES.items()}

        for code, item in raw.items():
            price = item.get("last_price")
            if not price or price <= 0:
                continue

            quote = FuturesQuote(
                symbol=code,
                price=float(price),
                change=float(item.get("change", 0) or 0),
                change_pct=float(item.get("change_percent", 0) or 0),
            )

            # Map to our internal key
            if code in reverse_index:
                key = reverse_index[code]
                quotes[key] = quote
                cash_prices[key] = float(price)
                self._set_cached(code, quote)
            elif code in reverse_futures:
                key = reverse_futures[code]
                futures_prices[key] = float(price)
                self._set_cached(code, quote)

        # Always update basis when we have both cash and futures in the same batch.
        # This ensures the basis is populated even on first run, so off-hours
        # futures-adjusted prices correctly account for the carry premium.
        for key in ("nasdaq", "spx"):
            if key in cash_prices and key in futures_prices:
                new_basis = futures_prices[key] - cash_prices[key]
                self._last_basis[key] = new_basis
                self._last_basis_time[key] = datetime.now(timezone.utc)

        # Also fetch crypto from Binance and merge
        crypto_quotes = self._fetch_binance_prices()
        quotes.update(crypto_quotes)

        return quotes

    def _fetch_single_quote(self, index: str) -> Optional[FuturesQuote]:
        """Fetch a single index quote from InsightSentry."""
        is_code = self.IS_TICKERS.get(index)
        if not is_code:
            return None

        # Check cache first
        cached = self._get_cached(is_code)
        if cached:
            return cached

        raw = self._fetch_batch_quotes([is_code])
        item = raw.get(is_code)
        if not item:
            return None

        price = item.get("last_price")
        if not price or price <= 0:
            return None

        quote = FuturesQuote(
            symbol=is_code,
            price=float(price),
            change=float(item.get("change", 0) or 0),
            change_pct=float(item.get("change_percent", 0) or 0),
        )
        self._set_cached(is_code, quote)
        return quote

    def _get_futures_quote_is(self, index: str) -> Optional[FuturesQuote]:
        """Get futures quote from InsightSentry (for overnight basis)."""
        is_code = self.IS_FUTURES.get(index)
        if not is_code:
            return None

        cached = self._get_cached(is_code)
        if cached:
            return cached

        raw = self._fetch_batch_quotes([is_code])
        item = raw.get(is_code)
        if not item:
            return None

        price = item.get("last_price")
        if not price or price <= 0:
            return None

        quote = FuturesQuote(
            symbol=is_code,
            price=float(price),
            change=float(item.get("change", 0) or 0),
            change_pct=float(item.get("change_percent", 0) or 0),
        )
        self._set_cached(is_code, quote)
        return quote

    # ========================================================================
    # Public interface (unchanged from original)
    # ========================================================================

    @staticmethod
    def is_equity_market_open() -> bool:
        """Check if US equity cash markets are currently open (9:30 AM - 4:00 PM ET)."""
        now_et = datetime.now(ET)
        if now_et.weekday() >= 5:
            return False
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now_et <= market_close

    def get_settlement_price(self, index: str) -> Optional[FuturesQuote]:
        """
        Get the best estimate of where the cash index is RIGHT NOW.

        During market hours: returns the cash index price directly.
        Outside market hours: uses futures price adjusted by last known basis.
        For non-equity assets (treasury, forex, VIX): always uses direct quote.
        """
        index_lower = index.lower()

        if index_lower not in ("nasdaq", "spx"):
            return self.get_quote(index)

        if self.is_equity_market_open():
            cash_quote = self.get_quote(index)
            if cash_quote and cash_quote.price:
                self._update_basis(index_lower, cash_quote.price)
            return cash_quote
        else:
            return self._get_futures_adjusted_quote(index_lower)

    def _update_basis(self, index: str, cash_price: float):
        """Record the current futures-spot basis while both are live."""
        futures_quote = self._get_futures_quote_is(index)
        if futures_quote and futures_quote.price:
            self._last_basis[index] = futures_quote.price - cash_price
            self._last_basis_time[index] = datetime.now(timezone.utc)

    def _get_futures_adjusted_quote(self, index: str) -> Optional[FuturesQuote]:
        """Estimate cash index price from futures during off-hours."""
        futures_quote = self._get_futures_quote_is(index)
        if not futures_quote or not futures_quote.price:
            return self.get_quote(index)

        basis = self._last_basis.get(index, 0)
        basis_age_hours = 0
        if index in self._last_basis_time:
            basis_age_hours = (datetime.now(timezone.utc) - self._last_basis_time[index]).total_seconds() / 3600

        if basis_age_hours > 18:
            basis = 0

        estimated_cash = futures_quote.price - basis

        print(f"[MARKET DATA] {index} off-hours: futures={futures_quote.price:.2f}, "
              f"basis={basis:+.1f}, estimated_cash={estimated_cash:.2f}")

        return FuturesQuote(
            symbol=futures_quote.symbol,
            price=estimated_cash,
            change=futures_quote.change,
            change_pct=futures_quote.change_pct,
        )

    def get_quote(self, index: str) -> Optional[FuturesQuote]:
        """
        Get real-time quote for an index.

        For Kalshi fair value, prefer get_settlement_price() which handles
        overnight sessions using futures-adjusted prices.
        """
        index_lower = index.lower()

        # Route crypto to Binance
        if index_lower in self.BINANCE_TICKERS:
            quote = self._fetch_single_binance_quote(index_lower)
            if quote:
                return quote
            # Fallback to yfinance for crypto
            yf_symbol = self.HIST_SYMBOLS.get(index_lower)
            if yf_symbol:
                return self._fetch_quote_yfinance(yf_symbol, index_lower)
            return None

        is_code = self.IS_TICKERS.get(index_lower)

        # Check cache
        if is_code:
            cached = self._get_cached(is_code)
            if cached:
                return cached

        # Fetch from InsightSentry
        quote = self._fetch_single_quote(index_lower)
        if quote:
            return quote

        # Fallback to yfinance
        yf_symbol = self.HIST_SYMBOLS.get(index_lower)
        if yf_symbol:
            print(f"[MARKET DATA] InsightSentry unavailable for {index_lower}, falling back to Yahoo Finance")
            return self._fetch_quote_yfinance(yf_symbol, index_lower)
        return None

    def get_historical_volatility(self, index: str, days: int = 30) -> Optional[float]:
        """
        Calculate historical volatility (annualized) from daily returns.
        Uses Yahoo Finance for historical bars.
        """
        index_lower = index.lower()
        symbol = self.HIST_SYMBOLS.get(index_lower)

        if not symbol:
            return None

        cache_key = f"{symbol}_{days}"
        if cache_key in self._vol_cache:
            vol, ts = self._vol_cache[cache_key]
            if (datetime.now(timezone.utc) - ts).total_seconds() < self._vol_cache_ttl:
                return vol

        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=f"{days}d")

            if len(hist) < 10:
                return None

            if index_lower == "treasury10y":
                changes = hist['Close'].diff().dropna()
                daily_vol_abs = changes.std()
                current_yield = hist['Close'].iloc[-1]
                if current_yield > 0:
                    daily_vol = daily_vol_abs / current_yield
                    annual_vol = daily_vol * math.sqrt(252)
                else:
                    annual_vol = 0.15
            else:
                returns = hist['Close'].pct_change().dropna()
                daily_vol = returns.std()
                annual_vol = daily_vol * math.sqrt(252)

            self._vol_cache[cache_key] = (annual_vol, datetime.now(timezone.utc))

            print(f"[VOLATILITY] {index}: {annual_vol:.4f} ({daily_vol*100:.3f}% daily) from {len(hist)} days")
            return annual_vol

        except Exception as e:
            print(f"[VOLATILITY] Error calculating {index}: {e}")
            return None

    def get_all_quotes(self) -> dict[str, FuturesQuote]:
        """Get quotes for most important indices (nasdaq, spx)."""
        quotes = {}
        for index in ["nasdaq", "spx"]:
            quote = self.get_quote(index)
            if quote:
                quotes[index] = quote
        return quotes

    def get_all_quotes_extended(self) -> dict[str, FuturesQuote]:
        """Get quotes for all tracked assets in a single API call."""
        return self._fetch_all_realtime()

    def get_all_settlement_prices(self) -> dict[str, FuturesQuote]:
        """
        Get best available price estimates for all indices.
        Single API call for all symbols. Uses futures-adjusted prices
        during off-hours for equities. Crypto is always live (24/7).
        """
        quotes = self._fetch_all_realtime()

        if not self.is_equity_market_open():
            # Replace equity index prices with futures-adjusted estimates
            for key in ("nasdaq", "spx"):
                adjusted = self._get_futures_adjusted_quote(key)
                if adjusted:
                    quotes[key] = adjusted
        else:
            # During market hours, update basis with fresh cash prices
            for key in ("nasdaq", "spx"):
                if key in quotes and quotes[key].price:
                    self._update_basis(key, quotes[key].price)

        return quotes

    def get_all_volatilities(self, days: int = 30) -> dict[str, float]:
        """Get historical volatilities for all tracked assets."""
        vols = {}
        for index in ["nasdaq", "spx", "treasury10y", "usdjpy", "eurusd", "wti",
                      "bitcoin", "ethereum", "solana", "dogecoin", "xrp"]:
            vol = self.get_historical_volatility(index, days)
            if vol:
                vols[index] = vol
        return vols

    def get_index_quote(self, index: str) -> Optional[FuturesQuote]:
        """Get cash index quote (what Kalshi settles to)."""
        return self.get_quote(index)

    def get_futures_quote(self, index: str) -> Optional[FuturesQuote]:
        """Get futures quote (for reference only, NOT settlement)."""
        return self._get_futures_quote_is(index.lower())

    def get_vix(self) -> Optional[float]:
        """Get current VIX level (implied volatility)."""
        quote = self.get_quote("vix")
        return quote.price if quote else None

    def get_basis(self, index: str) -> Optional[float]:
        """Get futures basis (futures price - spot index price)."""
        futures = self.get_futures_quote(index)
        spot = self.get_index_quote(index)
        if futures and spot and futures.price and spot.price:
            return futures.price - spot.price
        return None

    def get_basis_pct(self, index: str) -> Optional[float]:
        """Get futures basis as percentage of spot price."""
        futures = self.get_futures_quote(index)
        spot = self.get_index_quote(index)
        if futures and spot and futures.price and spot.price:
            return (futures.price - spot.price) / spot.price
        return None

    # ========================================================================
    # Binance API (crypto)
    # ========================================================================

    def _fetch_binance_prices(self) -> dict[str, FuturesQuote]:
        """Fetch all crypto prices from Binance in a single API call."""
        symbols = list(self.BINANCE_TICKERS.values())
        reverse_map = {v: k for k, v in self.BINANCE_TICKERS.items()}

        try:
            resp = requests.get(
                f"{BINANCE_BASE}/api/v3/ticker/24hr",
                params={"symbols": json.dumps(symbols)},
                timeout=8,
            )
            if resp.status_code != 200:
                print(f"[BINANCE] HTTP {resp.status_code}")
                return {}

            data = resp.json()
            quotes = {}
            for item in data:
                symbol = item.get("symbol", "")
                key = reverse_map.get(symbol)
                if not key:
                    continue

                price = float(item.get("lastPrice", 0))
                if price <= 0:
                    continue

                quote = FuturesQuote(
                    symbol=symbol,
                    price=price,
                    change=float(item.get("priceChange", 0)),
                    change_pct=float(item.get("priceChangePercent", 0)),
                )
                quotes[key] = quote
                self._set_cached(f"BINANCE:{symbol}", quote)

            return quotes

        except Exception as e:
            print(f"[BINANCE] Error: {e}")
            return {}

    def _fetch_single_binance_quote(self, index: str) -> Optional[FuturesQuote]:
        """Fetch a single crypto quote from Binance."""
        symbol = self.BINANCE_TICKERS.get(index)
        if not symbol:
            return None

        # Check cache
        cached = self._get_cached(f"BINANCE:{symbol}")
        if cached:
            return cached

        try:
            resp = requests.get(
                f"{BINANCE_BASE}/api/v3/ticker/24hr",
                params={"symbol": symbol},
                timeout=8,
            )
            if resp.status_code != 200:
                return None

            item = resp.json()
            price = float(item.get("lastPrice", 0))
            if price <= 0:
                return None

            quote = FuturesQuote(
                symbol=symbol,
                price=price,
                change=float(item.get("priceChange", 0)),
                change_pct=float(item.get("priceChangePercent", 0)),
            )
            self._set_cached(f"BINANCE:{symbol}", quote)
            return quote

        except Exception as e:
            print(f"[BINANCE] Error fetching {symbol}: {e}")
            return None

    # ========================================================================
    # Internal helpers
    # ========================================================================

    def _fetch_quote_yfinance(self, symbol: str, label: str = "") -> Optional[FuturesQuote]:
        """Fallback: fetch a quote from Yahoo Finance."""
        cached = self._get_cached(symbol)
        if cached:
            return cached

        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            fast = ticker.fast_info

            quote = FuturesQuote(
                symbol=symbol,
                price=fast.get('lastPrice') or info.get('regularMarketPrice', 0),
                change=info.get('regularMarketChange', 0),
                change_pct=info.get('regularMarketChangePercent', 0),
            )

            if quote.price and quote.price > 0:
                self._set_cached(symbol, quote)
                return quote
            return None

        except Exception as e:
            print(f"[YFINANCE] Error fetching {symbol}: {e}")
            return None

    def _is_rate_limited(self) -> bool:
        """Check if we're currently in a rate-limit backoff period."""
        return time.time() < self._backoff_until

    def _trigger_backoff(self):
        """Trigger exponential backoff due to rate limiting."""
        with self._rate_limit_lock:
            self._backoff_until = time.time() + self._backoff_duration
            print(f"[MARKET DATA] Rate limited, backing off for {self._backoff_duration}s")
            self._backoff_duration = min(self._backoff_duration * 2, self._max_backoff)

    def _reset_backoff(self):
        """Reset backoff on successful request."""
        with self._rate_limit_lock:
            self._backoff_duration = self._base_backoff

    def _get_cached(self, symbol: str, max_age: float = None) -> Optional[FuturesQuote]:
        """Get cached quote if available and not too old."""
        max_age = max_age or self._cache_ttl
        with self._cache_lock:
            if symbol in self._cache:
                quote, ts = self._cache[symbol]
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                if age < max_age:
                    return quote
                if self._is_rate_limited() and age < 300:
                    return quote
        return None

    def _set_cached(self, symbol: str, quote: FuturesQuote):
        """Store quote in cache."""
        with self._cache_lock:
            self._cache[symbol] = (quote, datetime.now(timezone.utc))

    # Legacy method for backward compatibility
    def _fetch_quote(self, symbol: str, label: str) -> Optional[FuturesQuote]:
        """Legacy fetch — routes to InsightSentry or yfinance."""
        reverse_map = {v: k for k, v in self.IS_TICKERS.items()}
        if symbol in reverse_map:
            return self._fetch_single_quote(reverse_map[symbol])
        reverse_futures = {v: k for k, v in self.IS_FUTURES.items()}
        if symbol in reverse_futures:
            return self._get_futures_quote_is(reverse_futures[symbol])
        return self._fetch_quote_yfinance(symbol, label)
