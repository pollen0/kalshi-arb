"""
Kalshi WebSocket Client for Real-Time Orderbook Data

Provides streaming orderbook updates instead of polling.
Based on: https://docs.kalshi.com/websockets/orderbook-updates
"""

import asyncio
import itertools
import json
import time
import base64
import threading
from datetime import datetime, timezone
from typing import Optional, Callable
from dataclasses import dataclass, field
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("[WS] websockets library not installed. Run: pip install websockets")


@dataclass
class OrderbookLevel:
    """Single price level"""
    price: int  # cents
    quantity: int


@dataclass
class LiveOrderbook:
    """Real-time orderbook maintained from WebSocket updates"""
    ticker: str
    yes_bids: dict[int, int] = field(default_factory=dict)  # price -> quantity
    yes_asks: dict[int, int] = field(default_factory=dict)
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def best_bid(self) -> Optional[int]:
        return max(self.yes_bids.keys()) if self.yes_bids else None

    @property
    def best_ask(self) -> Optional[int]:
        return min(self.yes_asks.keys()) if self.yes_asks else None

    @property
    def spread(self) -> Optional[int]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    def get_bids_list(self) -> list[OrderbookLevel]:
        """Get bids sorted by price descending"""
        return [OrderbookLevel(p, q) for p, q in
                sorted(self.yes_bids.items(), reverse=True) if q > 0]

    def get_asks_list(self) -> list[OrderbookLevel]:
        """Get asks sorted by price ascending"""
        return [OrderbookLevel(p, q) for p, q in
                sorted(self.yes_asks.items()) if q > 0]

    def depth_at_price(self, price: int, side: str) -> int:
        """Get total quantity at or better than price"""
        if side == "bid":
            return sum(q for p, q in self.yes_bids.items() if p >= price)
        else:
            return sum(q for p, q in self.yes_asks.items() if p <= price)


class KalshiWebSocket:
    """
    WebSocket client for real-time Kalshi orderbook data.

    Usage:
        ws = KalshiWebSocket(api_key, private_key_pem)
        ws.subscribe(["TICKER1", "TICKER2"])
        ws.start()  # Runs in background thread

        # Get live orderbook
        ob = ws.get_orderbook("TICKER1")
        print(f"Best bid: {ob.best_bid}, Best ask: {ob.best_ask}")

        ws.stop()
    """

    # Production WebSocket URL
    WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    def __init__(self, api_key: str, private_key_pem: str):
        if not WEBSOCKETS_AVAILABLE:
            raise ImportError("websockets library required. Run: pip install websockets")

        self.api_key = api_key
        self.private_key = serialization.load_pem_private_key(
            private_key_pem.encode(),
            password=None,
            backend=default_backend()
        )

        # State
        self.orderbooks: dict[str, LiveOrderbook] = {}
        self._subscribed_tickers: set[str] = set()
        self._message_id = itertools.count(1)
        self._lock = threading.Lock()

        # Connection state
        self._ws = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reconnect_attempts: int = 0

        # Callbacks
        self._on_orderbook_update: Optional[Callable[[str, LiveOrderbook], None]] = None
        self._on_connection_change: Optional[Callable[[bool], None]] = None

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

    def _get_auth_headers(self) -> dict:
        """Generate authentication headers for WebSocket connection"""
        timestamp = str(int(time.time() * 1000))
        # Sign: timestamp + method + path
        message = timestamp + "GET" + "/trade-api/ws/v2"
        signature = self._sign(message)

        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def _next_message_id(self) -> int:
        return next(self._message_id)

    async def _connect(self):
        """Establish WebSocket connection"""
        headers = self._get_auth_headers()

        try:
            self._ws = await websockets.connect(
                self.WS_URL,
                extra_headers=headers,
                ping_interval=30,
                ping_timeout=10,
            )
            print(f"[WS] Connected to {self.WS_URL}")

            if self._on_connection_change:
                self._on_connection_change(True)

            # Reset backoff on successful connection
            self._reconnect_attempts = 0

            # Resubscribe to tickers
            if self._subscribed_tickers:
                await self._send_subscribe(list(self._subscribed_tickers))

        except Exception as e:
            print(f"[WS] Connection failed: {e}")
            if self._on_connection_change:
                self._on_connection_change(False)
            raise

    async def _send_subscribe(self, tickers: list[str]):
        """Send subscription message for orderbook updates"""
        if not self._ws:
            return

        message = {
            "id": self._next_message_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            }
        }

        await self._ws.send(json.dumps(message))
        print(f"[WS] Subscribed to {len(tickers)} markets")

    async def _send_unsubscribe(self, tickers: list[str]):
        """Send unsubscribe message"""
        if not self._ws:
            return

        message = {
            "id": self._next_message_id(),
            "cmd": "unsubscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            }
        }

        await self._ws.send(json.dumps(message))

    def _handle_snapshot(self, data: dict):
        """Handle orderbook snapshot message"""
        ticker = data.get("market_ticker")
        if not ticker:
            return

        with self._lock:
            ob = LiveOrderbook(ticker=ticker)

            # Parse YES bids (the 'yes' array)
            for level in data.get("yes", []) or []:
                if len(level) >= 2:
                    price, qty = level[0], level[1]
                    if qty > 0:
                        ob.yes_bids[price] = qty

            # Parse YES asks (derived from 'no' bids: ask_price = 100 - no_bid)
            for level in data.get("no", []) or []:
                if len(level) >= 2:
                    no_price, qty = level[0], level[1]
                    yes_ask_price = 100 - no_price
                    if qty > 0:
                        ob.yes_asks[yes_ask_price] = qty

            ob.last_update = datetime.now(timezone.utc)
            self.orderbooks[ticker] = ob

        if self._on_orderbook_update:
            self._on_orderbook_update(ticker, ob)

    def _handle_delta(self, data: dict):
        """Handle orderbook delta (incremental update)"""
        ticker = data.get("market_ticker")
        if not ticker:
            return

        with self._lock:
            if ticker not in self.orderbooks:
                # Need snapshot first
                return

            ob = self.orderbooks[ticker]
            price = data.get("price", 0)
            delta = data.get("delta", 0)
            side = data.get("side", "")

            if side == "yes":
                # Update YES bids
                current = ob.yes_bids.get(price, 0)
                new_qty = current + delta
                if new_qty <= 0:
                    ob.yes_bids.pop(price, None)
                else:
                    ob.yes_bids[price] = new_qty
            elif side == "no":
                # Update YES asks (NO bid at X = YES ask at 100-X)
                yes_ask_price = 100 - price
                current = ob.yes_asks.get(yes_ask_price, 0)
                new_qty = current + delta
                if new_qty <= 0:
                    ob.yes_asks.pop(yes_ask_price, None)
                else:
                    ob.yes_asks[yes_ask_price] = new_qty

            ob.last_update = datetime.now(timezone.utc)

        if self._on_orderbook_update:
            self._on_orderbook_update(ticker, ob)

    async def _message_loop(self):
        """Main message processing loop"""
        while self._running:
            try:
                if not self._ws:
                    await self._connect()

                message = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=60
                )

                data = json.loads(message)
                msg_type = data.get("type")

                if msg_type == "orderbook_snapshot":
                    self._handle_snapshot(data)
                elif msg_type == "orderbook_delta":
                    self._handle_delta(data)
                elif msg_type == "subscribed":
                    print(f"[WS] Subscription confirmed: {data.get('msg', {}).get('channel')}")
                elif msg_type == "error":
                    print(f"[WS] Error: {data.get('msg')}")

            except asyncio.TimeoutError:
                # No message received, send ping
                if self._ws:
                    try:
                        await self._ws.ping()
                    except Exception:
                        pass

            except websockets.exceptions.ConnectionClosed as e:
                print(f"[WS] Connection closed: {e}")
                if self._on_connection_change:
                    self._on_connection_change(False)
                self._ws = None
                if self._running:
                    backoff = min(60, 2 ** self._reconnect_attempts)
                    self._reconnect_attempts += 1
                    print(f"[WS] Reconnecting in {backoff}s (attempt {self._reconnect_attempts})")
                    await asyncio.sleep(backoff)

            except Exception as e:
                print(f"[WS] Error: {e}")
                if self._running:
                    backoff = min(60, 2 ** self._reconnect_attempts)
                    self._reconnect_attempts += 1
                    await asyncio.sleep(backoff)

    def _run_async(self):
        """Run the async event loop in a thread"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._message_loop())
        finally:
            self._loop.close()

    def subscribe(self, tickers: list[str]):
        """Subscribe to orderbook updates for given tickers"""
        with self._lock:
            new_tickers = set(tickers) - self._subscribed_tickers
            self._subscribed_tickers.update(tickers)

        # Initialize orderbooks
        for ticker in new_tickers:
            if ticker not in self.orderbooks:
                self.orderbooks[ticker] = LiveOrderbook(ticker=ticker)

        # If connected, send subscribe
        if self._ws and self._loop and new_tickers:
            asyncio.run_coroutine_threadsafe(
                self._send_subscribe(list(new_tickers)),
                self._loop
            )

    def unsubscribe(self, tickers: list[str]):
        """Unsubscribe from orderbook updates"""
        with self._lock:
            self._subscribed_tickers -= set(tickers)

        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_unsubscribe(tickers),
                self._loop
            )

    def start(self):
        """Start WebSocket connection in background thread"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_async, daemon=True)
        self._thread.start()
        print("[WS] Started background thread")

    def stop(self):
        """Stop WebSocket connection"""
        self._running = False
        if self._ws and self._loop:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        if self._thread:
            self._thread.join(timeout=5)
        print("[WS] Stopped")

    def get_orderbook(self, ticker: str) -> Optional[LiveOrderbook]:
        """Get current orderbook for a ticker"""
        with self._lock:
            return self.orderbooks.get(ticker)

    def get_all_orderbooks(self) -> dict[str, LiveOrderbook]:
        """Get all orderbooks"""
        with self._lock:
            return dict(self.orderbooks)

    def is_connected(self) -> bool:
        """Check if WebSocket is connected"""
        return self._ws is not None and self._running

    def on_orderbook_update(self, callback: Callable[[str, LiveOrderbook], None]):
        """Set callback for orderbook updates"""
        self._on_orderbook_update = callback

    def on_connection_change(self, callback: Callable[[bool], None]):
        """Set callback for connection state changes"""
        self._on_connection_change = callback


# Convenience function to create client
def create_websocket_client(api_key: str, private_key_pem: str) -> Optional[KalshiWebSocket]:
    """Create a WebSocket client if websockets is available"""
    if not WEBSOCKETS_AVAILABLE:
        print("[WS] WebSocket client unavailable - install websockets package")
        return None
    return KalshiWebSocket(api_key, private_key_pem)
