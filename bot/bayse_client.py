"""Bayse Markets REST API client.

Base URL: https://relay.bayse.markets
API docs: https://docs.bayse.markets

Authentication (HMAC-SHA256):
  All requests include `X-Public-Key` header.
  POST and DELETE requests additionally require:
    - X-Timestamp: Unix timestamp (seconds)
    - X-Signature: base64(HMAC-SHA256(secret_key, "{ts}.{METHOD}.{path}.{sha256(body)}"))

This mirrors the auth scheme used by the bayse-markets-sdk TypeScript
package (npm install bayse-markets-sdk, by @Mudigram / @TheMudiaga).
The SDK wraps the same auth logic under its `BayseClient` constructor:
  new BayseClient({ publicKey: '...', secretKey: '...' })

So yes — you still need both an API key (publicKey) and a secret key.
The SDK does NOT bypass API keys; it handles the HMAC signing for you.

Endpoints used:
  GET  /v1/pm/events                     - List open prediction market events
  GET  /v1/pm/events/{id}                - Get a specific event with its current odds
  GET  /v1/pm/events?seriesSlug={slug}   - Get events by series slug
  GET  /v1/pm/portfolio                  - Get the user's open positions
  POST /v1/pm/orders/{eventId}/{mktId}   - Place an order (buy/sell shares)
  DELETE /v1/pm/orders/{orderId}         - Cancel an order
  POST /v1/pm/shares/mint                - Mint share pairs
  POST /v1/pm/shares/burn                - Burn matched share pairs
  GET  /v1/wallet/assets                 - Get wallet balances
  GET  /v1/pm/markets/{mktId}/orderbook  - Get order book for a market
  GET  /v1/pm/markets/{mktId}/ticker     - Get market ticker
"""

import hashlib
import hmac
import base64
import time
import json

import httpx
from loguru import logger
from typing import Optional


BASE_URL = "https://relay.bayse.markets"


def _sign(secret_key: str, method: str, path: str, body: str, timestamp: str) -> str:
    """Generate HMAC-SHA256 signature for authenticated requests.

    Signature covers: "{timestamp}.{METHOD}.{path}.{sha256(body)}"
    Returns base64-encoded signature.
    """
    body_hash = hashlib.sha256(body.encode()).hexdigest() if body else ""
    message = f"{timestamp}.{method}.{path}.{body_hash}"
    sig = hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()


class BayseClient:
    """Thin wrapper around the Bayse Markets REST API.

    Uses the same authentication scheme as the bayse-markets-sdk TypeScript
    package: public/secret key pair with HMAC-SHA256 request signing.
    """

    def __init__(
        self,
        public_key: str,
        secret_key: str,
        base_url: str = BASE_URL,
    ):
        self.base_url = base_url.rstrip("/")
        self.public_key = public_key
        self.secret_key = secret_key

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        """Build headers for a request. Adds signing headers for write ops."""
        headers = {
            "X-Public-Key": self.public_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if method.upper() not in ("GET", "HEAD"):
            timestamp = str(int(time.time()))
            # Strip query string from path before signing
            sign_path = path.split("?")[0]
            signature = _sign(self.secret_key, method.upper(), sign_path, body, timestamp)
            headers["X-Timestamp"] = timestamp
            headers["X-Signature"] = signature
        return headers

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._headers("GET", path)
        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"GET {path} failed: {e.response.status_code} {e.response.text}")
            raise

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        body_str = json.dumps(body)
        headers = self._headers("POST", path, body_str)
        try:
            resp = httpx.post(url, headers=headers, content=body_str, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"POST {path} failed: {e.response.status_code} {e.response.text}")
            raise

    def _delete(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._headers("DELETE", path)
        try:
            resp = httpx.delete(url, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as e:
            logger.error(f"DELETE {path} failed: {e.response.status_code} {e.response.text}")
            raise

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #

    def get_open_events(self, page: int = 1, size: int = 20) -> list[dict]:
        """Fetch open (live) prediction market events."""
        data = self._get("/v1/pm/events", params={"page": page, "size": size, "status": "open"})
        return data.get("events", data.get("data", data)) if isinstance(data, dict) else data

    def get_events_by_series(self, series_slug: str) -> list[dict]:
        """Fetch active events for a specific series slug (e.g. 'crypto-btc-15min')."""
        data = self._get("/v1/pm/events", params={"seriesSlug": series_slug, "status": "open"})
        return data.get("events", data.get("data", data)) if isinstance(data, dict) else data

    def get_event(self, event_id: str) -> dict:
        """Get a specific event by ID, including current share prices."""
        return self._get(f"/v1/pm/events/{event_id}")

    def get_market_ticker(self, market_id: str) -> dict:
        """Get ticker data for a market."""
        return self._get(f"/v1/pm/markets/{market_id}/ticker")

    def get_order_book(self, market_id: str) -> dict:
        """Get the order book for a market."""
        return self._get(f"/v1/pm/markets/{market_id}/orderbook")

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #

    def place_order(
        self,
        event_id: str,
        market_id: str,
        outcome_id: str,
        side: str,  # 'BUY' or 'SELL'
        price: float,
        amount: float,
        currency: str = "NGN",
    ) -> dict:
        """Place a limit order on a prediction market outcome.

        Args:
            event_id: The event ID.
            market_id: The market ID within the event.
            outcome_id: The specific outcome (YES/NO outcome ID).
            side: 'BUY' or 'SELL'.
            price: Probability price (0.0 - 1.0).
            amount: Number of shares.
            currency: Trading currency (default NGN).

        Returns:
            Order confirmation from the API.
        """
        body = {
            "outcomeId": outcome_id,
            "side": side.upper(),
            "price": round(price, 4),
            "amount": round(amount, 2),
            "currency": currency,
        }
        logger.info(f"Placing order: {body}")
        return self._post(f"/v1/pm/orders/{event_id}/{market_id}", body)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        return self._delete(f"/v1/pm/orders/{order_id}")

    def get_orders(self, status: Optional[str] = None) -> list[dict]:
        """Get the user's orders."""
        params = {"status": status} if status else None
        data = self._get("/v1/pm/orders", params=params)
        return data.get("data", data) if isinstance(data, dict) else data

    # ------------------------------------------------------------------ #
    # Share minting / burning (market-making capital efficiency)
    # ------------------------------------------------------------------ #

    def mint_shares(self, market_id: str, quantity: int) -> dict:
        """Mint new YES+NO share pairs to provide liquidity."""
        return self._post("/v1/pm/shares/mint", {"marketId": market_id, "quantity": quantity})

    def burn_shares(self, market_id: str, quantity: int) -> dict:
        """Burn matched YES+NO share pairs to reclaim capital."""
        return self._post("/v1/pm/shares/burn", {"marketId": market_id, "quantity": quantity})

    # ------------------------------------------------------------------ #
    # Portfolio & wallet
    # ------------------------------------------------------------------ #

    def get_portfolio(self) -> list[dict]:
        """Get all open positions."""
        data = self._get("/v1/pm/portfolio")
        return data.get("data", data) if isinstance(data, dict) else data

    def get_balance(self) -> dict:
        """Get wallet balances across all assets."""
        data = self._get("/v1/wallet/assets")
        return data.get("assets", data) if isinstance(data, dict) else data

    def get_profile(self) -> dict:
        """Get the authenticated user profile."""
        return self._get("/v1/user/profile")
