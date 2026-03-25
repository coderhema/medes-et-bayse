"""Bayse Markets REST API client.

Base URL: https://relay.bayse.markets
API docs: https://docs.bayse.markets/llms.txt

Endpoints used:
  GET  /v1/pm/events           - List open prediction market events
  GET  /v1/pm/events/{id}      - Get a specific event with its current odds
  GET  /v1/pm/portfolio        - Get the user's open positions
  POST /v1/pm/trade            - Place a trade (buy Yes or No shares)
  POST /v1/pm/sell             - Sell shares of an open position
  GET  /v1/wallet/balance      - Get wallet balance
  GET  /v1/user/profile        - Get authenticated user profile
"""

import httpx
from loguru import logger
from typing import Optional


class BayseClient:
    """Thin wrapper around the Bayse Markets REST API."""

    def __init__(self, api_key: str, base_url: str = "https://relay.bayse.markets"):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = httpx.get(url, headers=self.headers, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"GET {path} failed: {e.response.status_code} {e.response.text}")
            raise

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = httpx.post(url, headers=self.headers, json=body, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"POST {path} failed: {e.response.status_code} {e.response.text}")
            raise

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #

    def get_open_events(self, page: int = 1, size: int = 20) -> list[dict]:
        """Fetch open (live) prediction market events."""
        data = self._get("/v1/pm/events", params={"page": page, "size": size, "status": "open"})
        return data.get("data", data) if isinstance(data, dict) else data

    def get_event(self, event_id: str) -> dict:
        """Get a specific event by ID, including current share prices."""
        return self._get(f"/v1/pm/events/{event_id}")

    # ------------------------------------------------------------------ #
    # Trading
    # ------------------------------------------------------------------ #

    def place_trade(
        self,
        event_id: str,
        side: str,  # 'yes' or 'no'
        amount: float,  # USDC amount
    ) -> dict:
        """Buy shares on a prediction market outcome.

        Args:
            event_id: The market event ID.
            side: 'yes' or 'no'.
            amount: Amount in USDC to stake.

        Returns:
            Trade confirmation from the API.
        """
        body = {
            "eventId": event_id,
            "side": side.upper(),
            "amount": round(amount, 2),
        }
        logger.info(f"Placing trade: {body}")
        return self._post("/v1/pm/trade", body)

    def sell_position(
        self,
        event_id: str,
        side: str,
        shares: float,
    ) -> dict:
        """Sell shares of an open position."""
        body = {
            "eventId": event_id,
            "side": side.upper(),
            "shares": round(shares, 4),
        }
        return self._post("/v1/pm/sell", body)

    # ------------------------------------------------------------------ #
    # Portfolio & wallet
    # ------------------------------------------------------------------ #

    def get_portfolio(self) -> list[dict]:
        """Get all open positions."""
        data = self._get("/v1/pm/portfolio")
        return data.get("data", data) if isinstance(data, dict) else data

    def get_balance(self) -> dict:
        """Get wallet balance (USDC and local currency)."""
        return self._get("/v1/wallet/balance")

    def get_profile(self) -> dict:
        """Get the authenticated user profile."""
        return self._get("/v1/user/profile")
