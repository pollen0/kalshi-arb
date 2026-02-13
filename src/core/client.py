"""
Kalshi API Client with RSA-PSS authentication
"""

import threading
import time
import json
import base64
import uuid
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

from .models import Market, Position, Orderbook, OrderbookLevel, Side, MarketType


class KalshiClient:
    """
    Kalshi API client with proper RSA-PSS authentication.

    Rate limits (Basic tier): 20 reads/sec, 10 writes/sec
    """

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, api_key: str, private_key_pem: str):
        self.api_key = api_key
        self.private_key = serialization.load_pem_private_key(
            private_key_pem.encode(),
            password=None,
            backend=default_backend()
        )
        self.session = requests.Session()
        self._last_request = 0
        self._min_read_interval = 0.05   # 50ms = 20 reads/sec
        self._min_write_interval = 0.10  # 100ms = 10 writes/sec
        self._last_read = 0
        self._last_write = 0
        self._rate_lock = threading.Lock()

        # Market discovery cache: {series_prefix: {"markets": [...], "fetched_at": datetime, "next_expiry": datetime}}
        self._series_cache = {}
        self._series_cache_lock = threading.Lock()

    def _rate_limit(self, is_write: bool = False):
        """Thread-safe rate limiting with separate read/write budgets.

        Kalshi limits: 20 reads/sec, 10 writes/sec.
        """
        with self._rate_lock:
            now = time.time()
            if is_write:
                elapsed = now - self._last_write
                if elapsed < self._min_write_interval:
                    time.sleep(self._min_write_interval - elapsed)
                self._last_write = time.time()
            else:
                elapsed = now - self._last_read
                if elapsed < self._min_read_interval:
                    time.sleep(self._min_read_interval - elapsed)
                self._last_read = time.time()

    def _sign(self, text: str) -> str:
        """Sign text using RSA-PSS with SHA256"""
        signature = self.private_key.sign(
            text.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')

    def _headers(self, method: str, path: str) -> dict:
        """Generate signed headers"""
        timestamp = str(int(time.time() * 1000))
        full_path = "/trade-api/v2" + path.split('?')[0]
        signature = self._sign(timestamp + method + full_path)

        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def _request(self, method: str, path: str, data: dict = None) -> dict:
        """Make authenticated request"""
        is_write = method in ("POST", "PUT", "DELETE")
        self._rate_limit(is_write=is_write)

        url = f"{self.BASE_URL}{path}"
        headers = self._headers(method, path)

        try:
            if method == "GET":
                resp = self.session.get(url, headers=headers, timeout=15)
            elif method == "POST":
                resp = self.session.post(url, headers=headers,
                                        data=json.dumps(data) if data else "", timeout=15)
            elif method == "DELETE":
                resp = self.session.delete(url, headers=headers, timeout=15)
            else:
                raise ValueError(f"Unknown method: {method}")

            if resp.status_code in [200, 201]:
                return resp.json()
            else:
                return {"error": True, "status": resp.status_code, "message": resp.text}

        except Exception as e:
            return {"error": True, "message": str(e)}

    # === Account Methods ===

    def get_balance(self) -> float:
        """Get account balance in dollars. Returns None on API error."""
        result = self._request("GET", "/portfolio/balance")
        if result.get("error"):
            return None
        return result.get("balance", 0) / 100

    def get_positions(self) -> Optional[list[Position]]:
        """Get all open positions with current market prices.
        Returns None on API error (not empty list) so callers can distinguish errors from no positions."""
        result = self._request("GET", "/portfolio/positions")
        if result.get("error"):
            print(f"[POSITIONS] Error fetching positions: {result}")
            return None

        positions = []
        market_positions = result.get("market_positions", [])

        for p in market_positions:
            pos = p.get("position", 0)
            if pos == 0:
                continue

            # Get current market prices if provided by API
            # Kalshi may provide these as part of the position response
            current_yes_bid = p.get("yes_bid")
            current_yes_ask = p.get("yes_ask")

            position = Position(
                ticker=p.get("ticker", ""),
                market_title=p.get("market_title", ""),
                side=Side.YES if pos > 0 else Side.NO,
                quantity=abs(pos),
                avg_price=p.get("average_price", 0) / 100,
                realized_pnl=p.get("realized_pnl", 0) / 100,
            )

            # Set current prices if available (convert from cents to probability)
            if current_yes_bid is not None:
                position.current_bid = current_yes_bid / 100
            if current_yes_ask is not None:
                position.current_ask = current_yes_ask / 100

            positions.append(position)
            print(f"[POSITIONS] Loaded: {position.ticker} - {position.side.value.upper()} x{position.quantity} @ {position.avg_price:.2f}")

        if positions:
            print(f"[POSITIONS] Total: {len(positions)} positions")

        return positions

    # === Market Methods ===

    def get_events(self, series_ticker: str = None, status: str = None, limit: int = 100) -> list:
        """Get events, optionally filtered by series"""
        params = [f"limit={limit}"]
        if status:
            params.append(f"status={status}")
        if series_ticker:
            params.append(f"series_ticker={series_ticker}")

        result = self._request("GET", f"/events?{'&'.join(params)}")
        return result.get("events", []) if isinstance(result, dict) else []

    def get_markets_by_series_prefix(
        self,
        series_prefix: str,
        status: str = None,
        time_patterns: list[str] = None,
        days_ahead: int = 2,
    ) -> list['Market']:
        """
        Get markets by series prefix (e.g., 'KXINXU', 'KXNASDAQ100U').

        Uses a cached 3-step discovery approach:
        1. Direct series_ticker query (single API call, most efficient)
        2. Events API with series_ticker filter (reliable fallback)
        3. Pattern-based event ticker generation (last resort)

        Results are cached per series_prefix. Cache auto-invalidates when:
        - The nearest market expires (triggers roll-forward to next slot)
        - 90 seconds have elapsed (periodic refresh)

        All queries run WITHOUT status filter. Results filtered client-side.

        Args:
            series_prefix: The series prefix (e.g., 'KXINXU', 'KXNASDAQ100U')
            status: Ignored (kept for caller compatibility). Filtering is done client-side.
            time_patterns: Optional list of time suffixes to check (e.g., ['H1200', 'H1600'])
            days_ahead: How many days ahead to check for events

        Returns:
            List of Market objects from all matching events
        """
        now = datetime.now(timezone.utc)

        # --- Check cache first ---
        with self._series_cache_lock:
            cached = self._series_cache.get(series_prefix)
            if cached:
                age = (now - cached["fetched_at"]).total_seconds()
                next_exp = cached["next_expiry"]

                # Determine if cache is still valid
                # Invalidate if: >90s old OR nearest market has expired (roll forward)
                needs_refresh = age > 90
                if next_exp and next_exp <= now:
                    needs_refresh = True  # A market just expired — roll forward

                if not needs_refresh:
                    # Return cached, but strip any that expired since caching
                    active = [m for m in cached["markets"] if m.close_time and m.close_time > now]
                    if active:
                        return active
                    # All cached markets expired — force refresh

        # --- Fetch fresh data ---
        markets = self._fetch_series_markets(series_prefix, time_patterns, days_ahead, now)

        # --- Update cache ---
        if markets:
            next_expiry = min(
                (m.close_time for m in markets if m.close_time),
                default=now + timedelta(hours=24),
            )
            with self._series_cache_lock:
                self._series_cache[series_prefix] = {
                    "markets": markets,
                    "fetched_at": now,
                    "next_expiry": next_expiry,
                }

        return markets

    def _fetch_series_markets(
        self,
        series_prefix: str,
        time_patterns: list[str],
        days_ahead: int,
        now: datetime,
    ) -> list['Market']:
        """Internal: fetch markets for a series prefix using 3-step discovery."""

        # Determine time patterns based on series prefix if not provided
        if time_patterns is None:
            if series_prefix in ["KXINXU", "KXINX", "KXNASDAQ100U", "KXNASDAQ100"]:
                time_patterns = ["H1000", "H1200", "H1600"]
            elif series_prefix in ["KXEURUSD", "KXUSDJPY"]:
                time_patterns = ["10"]
            elif series_prefix in ["KXTNOTED"]:
                time_patterns = [""]
            elif series_prefix in ["KXWTI", "KXWTIW"]:
                time_patterns = [""]
            elif series_prefix in ["KXBTCD", "KXETHD", "KXSOLD", "KXDOGED", "KXXRPD"]:
                time_patterns = [""]
            elif series_prefix in ["KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP",
                                   "KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXXRP15M"]:
                time_patterns = None  # High-frequency — rely on API discovery only
            else:
                time_patterns = ["H1600", ""]

        # --- Step 1: Direct series_ticker query (single API call) ---
        try:
            direct_markets = self.get_markets(series_ticker=series_prefix, status=None, limit=1000)
            if direct_markets:
                active = [m for m in direct_markets if m.close_time and m.close_time > now]
                if active:
                    print(f"[CLIENT] {series_prefix}: {len(active)} active markets via series_ticker")
                    return active
        except Exception as e:
            print(f"[CLIENT] Series query failed for {series_prefix}: {e}")

        # --- Step 2: Events API (N+1 calls, but reliable) ---
        try:
            events = self.get_events(series_ticker=series_prefix, limit=200)
            if events:
                all_markets = []
                for event in events:
                    event_ticker = event.get("event_ticker", "")
                    if not event_ticker:
                        continue
                    markets = self.get_markets(event_ticker=event_ticker, status=None)
                    if markets:
                        all_markets.extend(markets)

                active = [m for m in all_markets if m.close_time and m.close_time > now]
                if active:
                    print(f"[CLIENT] {series_prefix}: {len(active)} active markets via events API")
                    return active
        except Exception as e:
            print(f"[CLIENT] Events API failed for {series_prefix}: {e}")

        # --- Step 3: Pattern-based event ticker generation (last resort) ---
        if time_patterns is None:
            return []

        all_markets = []
        for day_offset in range(days_ahead + 1):
            date = now + timedelta(days=day_offset)
            date_str = date.strftime("%y%b%d").upper()

            for time_suffix in time_patterns:
                event_ticker = f"{series_prefix}-{date_str}{time_suffix}"
                markets = self.get_markets(event_ticker=event_ticker, status=None)
                if markets:
                    active = [m for m in markets if m.close_time and m.close_time > now]
                    if active:
                        all_markets.extend(active)
                        print(f"[CLIENT] Found {len(active)} markets for {event_ticker}")

        return all_markets

    def get_markets(self, event_ticker: str = None, series_ticker: str = None,
                   status: str = None, limit: int = 200) -> Optional[list['Market']]:
        """Get markets with parsed data.
        Returns None on API error (not empty list) so callers can preserve previous state."""
        params = [f"limit={limit}"]
        if event_ticker:
            params.append(f"event_ticker={event_ticker}")
        if series_ticker:
            params.append(f"series_ticker={series_ticker}")
        if status:
            params.append(f"status={status}")

        result = self._request("GET", f"/markets?{'&'.join(params)}")
        if result.get("error"):
            print(f"[MARKETS] Error fetching markets: {result.get('message', 'Unknown')}")
            return None
        raw_markets = result.get("markets", []) if isinstance(result, dict) else []

        markets = []
        for m in raw_markets:
            # Server-side status filter already applied via query param — no client-side re-check
            # (API accepts "open" as filter but response may use different status labels)

            # Parse close time
            close_time = None
            if m.get("close_time"):
                try:
                    close_time = datetime.fromisoformat(m["close_time"].replace('Z', '+00:00'))
                except:
                    pass

            # Detect market type
            market_type = self._detect_market_type(m)

            # Get bounds from floor_strike and cap_strike (API provides these directly)
            lower = m.get("floor_strike")
            upper = m.get("cap_strike")

            # Fallback to parsing from title if not provided
            if lower is None and upper is None:
                lower, upper = self._parse_bounds(m.get("title", ""))

            # Validate price fields — drop malformed entries
            yes_bid = m.get("yes_bid") or 0
            yes_ask = m.get("yes_ask") or 0
            no_bid = m.get("no_bid") or 0
            no_ask = m.get("no_ask") or 0

            if not (0 <= yes_bid <= 99 and 1 <= yes_ask <= 100 and yes_bid < yes_ask):
                # Allow zero-liquidity markets (bid=0, ask=100) but reject nonsense
                if not (yes_bid == 0 and yes_ask == 0):
                    ticker_str = m.get("ticker", "?")
                    print(f"[VALIDATE] Dropping malformed market {ticker_str}: "
                          f"yes_bid={yes_bid}, yes_ask={yes_ask}")
                    continue

            markets.append(Market(
                ticker=m.get("ticker", ""),
                event_ticker=m.get("event_ticker", ""),
                title=m.get("title", ""),
                subtitle=m.get("subtitle", ""),
                market_type=market_type,
                lower_bound=lower,
                upper_bound=upper,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                volume=m.get("volume", 0),
                close_time=close_time,
            ))

        return markets

    def get_orderbook(self, ticker: str) -> Optional[Orderbook]:
        """Get full orderbook for a market"""
        result = self._request("GET", f"/markets/{ticker}/orderbook")
        if result.get("error"):
            return None

        ob_data = result.get("orderbook") or {}

        # Parse YES bids (yes key can be None)
        yes_bids = []
        for level in ob_data.get("yes") or []:
            if len(level) >= 2:
                yes_bids.append(OrderbookLevel(price=level[0], quantity=level[1]))

        # Parse YES asks (from NO bids - no key can be None)
        yes_asks = []
        for level in ob_data.get("no") or []:
            if len(level) >= 2:
                yes_asks.append(OrderbookLevel(price=100 - level[0], quantity=level[1]))

        yes_bids.sort(key=lambda x: x.price, reverse=True)
        yes_asks.sort(key=lambda x: x.price)

        return Orderbook(ticker=ticker, yes_bids=yes_bids, yes_asks=yes_asks)

    def _detect_market_type(self, m: dict) -> MarketType:
        """Detect market type from market data"""
        title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()

        if "between" in title:
            return MarketType.FINANCIAL_RANGE
        elif "above" in title:
            return MarketType.FINANCIAL_ABOVE
        elif "below" in title:
            return MarketType.FINANCIAL_BELOW
        elif any(x in title for x in ["spread", "handicap", "pts", "+/-"]):
            return MarketType.SPORTS_SPREAD
        elif any(x in title for x in ["over", "under", "total", "o/u"]):
            return MarketType.SPORTS_TOTAL
        else:
            return MarketType.SPORTS_MONEYLINE

    def _parse_bounds(self, title: str) -> tuple:
        """Parse range bounds from title"""
        import re

        # "between X and Y"
        match = re.search(r'between\s*([\d,]+(?:\.\d+)?)\s*and\s*([\d,]+(?:\.\d+)?)', title, re.I)
        if match:
            return float(match.group(1).replace(',', '')), float(match.group(2).replace(',', ''))

        # "below X"
        match = re.search(r'below\s*([\d,]+(?:\.\d+)?)', title, re.I)
        if match:
            return 0, float(match.group(1).replace(',', ''))

        # "above X"
        match = re.search(r'above\s*([\d,]+(?:\.\d+)?)', title, re.I)
        if match:
            return float(match.group(1).replace(',', '')), float('inf')

        return None, None

    # === Trading Methods ===

    def place_order(self, ticker: str, side: str, action: str,
                   count: int, price: int, order_type: str = "limit") -> dict:
        """Place a limit order"""
        order_data = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
            "client_order_id": f"bot_{uuid.uuid4().hex[:12]}",
        }

        if side == "yes":
            order_data["yes_price"] = price
        else:
            order_data["no_price"] = price

        return self._request("POST", "/portfolio/orders", order_data)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order"""
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def cancel_all_orders(self) -> dict:
        """
        Cancel ALL resting orders on the account using parallel requests.

        Uses ThreadPoolExecutor to cancel multiple orders simultaneously,
        which is critical during emergency halts when speed matters.

        Returns:
            Dict with 'cancelled' count and 'failed' count
        """
        orders = self.get_orders(status="resting")
        order_ids = [o["order_id"] for o in orders if o.get("order_id")]

        if not order_ids:
            print("[CLIENT] No resting orders to cancel.")
            return {"cancelled": 0, "failed": 0, "total": 0}

        print(f"[CLIENT] Cancelling {len(order_ids)} resting orders in parallel...")

        cancelled = 0
        failed = 0

        def _cancel_one(order_id: str) -> tuple:
            """Cancel a single order, return (order_id, success)."""
            result = self.cancel_order(order_id)
            if result.get("error"):
                print(f"[CLIENT] Failed to cancel {order_id}: {result.get('message', 'Unknown')}")
                return (order_id, False)
            return (order_id, True)

        # Cap at 10 workers to stay within Kalshi's 10 writes/sec rate limit
        max_workers = min(10, len(order_ids))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_cancel_one, oid): oid for oid in order_ids}
            for future in as_completed(futures):
                try:
                    _, success = future.result()
                    if success:
                        cancelled += 1
                    else:
                        failed += 1
                except Exception as e:
                    failed += 1
                    print(f"[CLIENT] Exception cancelling {futures[future]}: {e}")

        print(f"[CLIENT] Cancelled {cancelled} orders, {failed} failed")
        return {"cancelled": cancelled, "failed": failed, "total": len(order_ids)}

    def get_orders(self, status: str = "resting", limit: int = 1000) -> list:
        """
        Get orders with pagination support.

        Args:
            status: Order status filter ('resting', 'filled', 'canceled', etc.)
            limit: Max orders per page (1-1000, default 1000)

        Returns:
            List of all orders matching the status (paginated through all pages)
        """
        all_orders = []
        cursor = None

        while True:
            # Build query params
            params = [f"status={status}", f"limit={limit}"]
            if cursor:
                params.append(f"cursor={cursor}")

            result = self._request("GET", f"/portfolio/orders?{'&'.join(params)}")

            if result.get("error"):
                print(f"[ORDERS] Error fetching orders: {result}")
                break

            orders = result.get("orders", [])
            all_orders.extend(orders)

            # Check for next page
            cursor = result.get("cursor")
            if not cursor or len(orders) < limit:
                # No more pages
                break

        return all_orders

    def get_order_status(self, order_id: str) -> Optional[dict]:
        """
        Get status of a specific order.

        Returns order details including status: 'resting', 'filled', 'canceled', etc.
        Returns None if order not found or error.
        """
        result = self._request("GET", f"/portfolio/orders/{order_id}")
        if result.get("error"):
            return None
        return result.get("order")

    def get_fills(self, ticker: str = None, limit: int = 100) -> list:
        """
        Get recent fills.

        Args:
            ticker: Optional ticker to filter fills
            limit: Max number of fills to return

        Returns:
            List of fill records
        """
        params = [f"limit={limit}"]
        if ticker:
            params.append(f"ticker={ticker}")

        result = self._request("GET", f"/portfolio/fills?{'&'.join(params)}")
        return result.get("fills", []) if not result.get("error") else []
