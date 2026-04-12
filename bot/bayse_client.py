"""Bayse Markets REST API client.

Base URL: https://relay.bayse.markets
API docs: https://docs.bayse.markets

Authentication (HMAC-SHA256):
  Write requests include `X-Public-Key`, `X-Timestamp`, and `X-Signature`.
  Signature format: base64(HMAC-SHA256(secret_key, "{ts}.{METHOD}.{path}.{sha256(body)}"))

Docs-confirmed endpoints used here:
  GET  /v1/pm/events
  GET  /v1/pm/events/{eventId}
  GET  /v1/pm/portfolio
  GET  /v1/pm/trades
  GET  /v1/wallet/assets
  POST /v1/pm/events/{eventId}/markets/{marketId}/quote
  POST /v1/pm/events/{eventId}/markets/{marketId}/orders
  DELETE /v1/pm/orders/{orderId}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

import httpx
from loguru import logger


BASE_URL = "https://relay.bayse.markets"


def _sign(secret_key: str, method: str, path: str, body: str, timestamp: str) -> str:
    body_hash = hashlib.sha256(body.encode()).hexdigest() if body else ""
    message = f"{timestamp}.{method}.{path}.{body_hash}"
    sig = hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()


class BayseClient:
    """Thin wrapper around the Bayse Markets REST API."""

    def __init__(self, public_key: str, secret_key: str, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.public_key = public_key
        self.secret_key = secret_key

    def _headers(self, method: str, path: str, body: str = "", sign: bool = False) -> dict:
        headers = {
            "X-Public-Key": self.public_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if sign:
            timestamp = str(int(time.time()))
            sign_path = path.split("?")[0]
            signature = _sign(self.secret_key, method.upper(), sign_path, body, timestamp)
            headers["X-Timestamp"] = timestamp
            headers["X-Signature"] = signature
        return headers

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._headers("GET", path)
        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"GET {path} failed: {e.response.status_code} {e.response.text}")
            raise

    def _post(self, path: str, body: dict, *, sign: bool = True) -> dict:
        url = f"{self.base_url}{path}"
        body_str = json.dumps(body)
        headers = self._headers("POST", path, body_str, sign=sign)
        try:
            resp = httpx.post(url, headers=headers, content=body_str, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"POST {path} failed: {e.response.status_code} {e.response.text}")
            raise

    def _delete(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._headers("DELETE", path, sign=True)
        try:
            resp = httpx.delete(url, headers=headers, timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as e:
            logger.error(f"DELETE {path} failed: {e.response.status_code} {e.response.text}")
            raise

    def get_open_events(self, page: int = 1, size: int = 20) -> list[dict]:
        data = self._get("/v1/pm/events", params={"page": page, "size": size, "status": "open"})
        if isinstance(data, dict):
            return data.get("events", data.get("data", data))
        return data

    def get_events_by_series(self, series_slug: str) -> list[dict]:
        data = self._get("/v1/pm/events", params={"seriesSlug": series_slug, "status": "open"})
        if isinstance(data, dict):
            return data.get("events", data.get("data", data))
        return data

    def get_event(self, event_id: str) -> dict:
        return self._get(f"/v1/pm/events/{event_id}")

    def get_quote(
        self,
        event_id: str,
        market_id: str,
        side: str,
        outcome_id: str,
        amount: float,
        currency: str = "USD",
    ) -> dict:
        body = {
            "side": side.upper(),
            "outcomeId": outcome_id,
            "amount": round(float(amount), 2),
            "currency": currency,
        }
        return self._post(f"/v1/pm/events/{event_id}/markets/{market_id}/quote", body, sign=False)

    def get_market_ticker(self, market_id: str) -> dict:
        return self._get(f"/v1/pm/markets/{market_id}/ticker")

    def get_order_book(self, market_id: str) -> dict:
        return self._get(f"/v1/pm/markets/{market_id}/orderbook")

    def get_trades(
        self,
        market_id: Optional[str] = None,
        trade_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        params: dict[str, object] = {"limit": limit}
        if market_id:
            params["marketId"] = market_id
        if trade_id:
            params["id"] = trade_id
        data = self._get("/v1/pm/trades", params=params)
        if isinstance(data, dict):
            return data.get("trades", data.get("data", data))
        return data

    def place_order(
        self,
        event_id: str,
        market_id: str,
        side: str,
        outcome: str,
        amount: float,
        currency: str = "USD",
        order_type: str = "MARKET",
        price: Optional[float] = None,
        time_in_force: Optional[str] = None,
        post_only: Optional[bool] = None,
        max_slippage: Optional[float] = None,
        expires_at: Optional[str] = None,
    ) -> dict:
        body: dict[str, object] = {
            "outcome": outcome.upper(),
            "side": side.upper(),
            "amount": round(float(amount), 2),
            "currency": currency,
        }
        if price is not None:
            body["price"] = round(float(price), 4)
        if time_in_force is not None:
            body["timeInForce"] = time_in_force
        elif order_type.upper() == "LIMIT":
            body["timeInForce"] = "GTC"
        if post_only is not None:
            body["postOnly"] = post_only
        if max_slippage is not None:
            body["maxSlippage"] = max_slippage
        if expires_at is not None:
            body["expiresAt"] = expires_at

        logger.info(f"Placing order: {body}")
        return self._post(f"/v1/pm/events/{event_id}/markets/{market_id}/orders", body, sign=True)

    def place_post_only_limit_order(
        self,
        event_id: str,
        market_id: str,
        *,
        outcome: str,
        side: str,
        amount: float,
        price: float,
        currency: str = 'USD',
        expires_at: Optional[str] = None,
    ) -> dict:
        if price is None:
            raise ValueError('price is required for a post-only limit order')
        return self.place_order(
            event_id=event_id,
            market_id=market_id,
            outcome=outcome,
            side=side,
            amount=amount,
            currency=currency,
            order_type='LIMIT',
            price=price,
            time_in_force='GTC',
            post_only=True,
            expires_at=expires_at,
        )

    def cancel_order(self, order_id: str) -> dict:
        return self._delete(f"/v1/pm/orders/{order_id}")

    def get_orders(self, status: Optional[str] = None) -> list[dict]:
        params = {"status": status} if status else None
        data = self._get("/v1/pm/orders", params=params)
        if isinstance(data, dict):
            return data.get("data", data)
        return data

    def mint_shares(self, market_id: str, quantity: int) -> dict:
        return self._post(f"/v1/pm/markets/{market_id}/mint", {"marketId": market_id, "quantity": quantity}, sign=True)

    def burn_shares(self, market_id: str, quantity: int) -> dict:
        return self._post(f"/v1/pm/markets/{market_id}/burn", {"marketId": market_id, "quantity": quantity}, sign=True)

    def get_portfolio(self) -> dict:
        data = self._get("/v1/pm/portfolio")
        if isinstance(data, dict):
            return data.get("outcomeBalances", data.get("data", data))
        return data

    def get_balance(self) -> dict:
        data = self._get("/v1/wallet/assets")
        if isinstance(data, dict):
            return data.get("assets", data)
        return data

    def get_profile(self) -> dict:
        return self._get("/v1/user/profile")
