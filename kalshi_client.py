#!/usr/bin/env python3
"""
Kalshi Live Trading Client

Implements RSA-PSS authentication required by Kalshi API.
Based on official Kalshi starter code.
"""

import time
import json
import base64
import requests
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend


class KalshiLiveClient:
    """
    Live trading client for Kalshi with proper RSA-PSS authentication.

    WARNING: This trades real money!
    """

    # Production API
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, api_key: str, private_key_pem: str):
        self.api_key = api_key
        self.private_key = serialization.load_pem_private_key(
            private_key_pem.encode(),
            password=None,
            backend=default_backend()
        )
        self.session = requests.Session()

    def _sign_pss_text(self, text: str) -> str:
        """Sign text using RSA-PSS with SHA256"""
        message = text.encode('utf-8')
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH  # This is the key difference!
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')

    def _request_headers(self, method: str, path: str) -> dict:
        """
        Generate signed headers for Kalshi API request.

        Message format: timestamp + method + FULL path (including /trade-api/v2)
        """
        timestamp = str(int(time.time() * 1000))

        # Build full path for signature (must include /trade-api/v2)
        full_path = "/trade-api/v2" + path
        path_parts = full_path.split('?')
        msg_string = timestamp + method + path_parts[0]

        signature = self._sign_pss_text(msg_string)

        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    def _request(self, method: str, path: str, data: dict = None) -> dict:
        """Make authenticated request to Kalshi API"""
        url = f"{self.BASE_URL}{path}"
        headers = self._request_headers(method, path)

        try:
            if method == "GET":
                resp = self.session.get(url, headers=headers, timeout=15)
            elif method == "POST":
                body = json.dumps(data) if data else ""
                resp = self.session.post(url, headers=headers, data=body, timeout=15)
            elif method == "DELETE":
                resp = self.session.delete(url, headers=headers, timeout=15)
            else:
                raise ValueError(f"Unknown method: {method}")

            if resp.status_code in [200, 201]:
                return resp.json()
            else:
                return {
                    "error": True,
                    "status_code": resp.status_code,
                    "message": resp.text
                }
        except Exception as e:
            return {"error": True, "message": str(e)}

    # =========================================================================
    # ACCOUNT METHODS
    # =========================================================================

    def get_balance(self) -> dict:
        """Get account balance in cents"""
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> list:
        """Get all open positions"""
        result = self._request("GET", "/portfolio/positions")
        return result.get("market_positions", []) if not result.get("error") else []

    def get_fills(self, limit: int = 100) -> list:
        """Get recent order fills"""
        result = self._request("GET", f"/portfolio/fills?limit={limit}")
        return result.get("fills", []) if not result.get("error") else []

    # =========================================================================
    # TRADING METHODS
    # =========================================================================

    def place_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,
        price: int,  # Price in cents (1-99)
        order_type: str = "limit"
    ) -> dict:
        """
        Place a limit order.

        Args:
            ticker: Market ticker (e.g., "KXNFLGAME-25DEC22DALNYG-DAL")
            side: "yes" or "no"
            action: "buy" or "sell"
            count: Number of contracts
            price: Price in cents (1-99)
            order_type: "limit" or "market"

        Returns:
            Order response with order_id
        """
        order_data = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
            "client_order_id": f"live_{int(time.time()*1000)}",
        }

        # Set price based on side
        if side == "yes":
            order_data["yes_price"] = price
        else:
            order_data["no_price"] = price

        return self._request("POST", "/portfolio/orders", order_data)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order"""
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def get_orders(self, ticker: str = None, status: str = "resting") -> list:
        """Get open orders"""
        path = f"/portfolio/orders?status={status}"
        if ticker:
            path += f"&ticker={ticker}"
        result = self._request("GET", path)
        return result.get("orders", []) if not result.get("error") else []

    # =========================================================================
    # MARKET METHODS
    # =========================================================================

    def get_market(self, ticker: str) -> dict:
        """Get market details"""
        return self._request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker: str) -> dict:
        """Get market orderbook"""
        return self._request("GET", f"/markets/{ticker}/orderbook")


def test_connection():
    """Test the API connection"""
    try:
        from credentials import KALSHI_API_KEY, KALSHI_PRIVATE_KEY

        client = KalshiLiveClient(KALSHI_API_KEY, KALSHI_PRIVATE_KEY)

        print("Testing Kalshi API connection...")
        print("-" * 40)

        # Test balance
        balance = client.get_balance()
        if balance.get("error"):
            print(f"ERROR: {balance}")
            return False

        balance_cents = balance.get("balance", 0)
        print(f"Account Balance: ${balance_cents/100:.2f}")

        # Test positions
        positions = client.get_positions()
        print(f"Open Positions: {len(positions)}")

        for pos in positions[:5]:
            print(f"  - {pos.get('ticker')}: {pos.get('position')} contracts")

        print("-" * 40)
        print("Connection successful!")
        return True

    except ImportError:
        print("ERROR: credentials.py not found")
        return False
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    test_connection()
