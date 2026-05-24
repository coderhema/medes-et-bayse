"""Bayse Markets tool for PyFlue agent.

Wraps the existing BayseClient as a callable tool the agent can use
to scan markets, get quotes, and place orders.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from loguru import logger

# Re-use the existing BayseClient from the bot package
from bot.bayse_client import BayseClient


def _build_client() -> BayseClient:
    public_key = os.getenv("BAYSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("BAYSE_SECRET_KEY", "").strip()
    base_url = os.getenv("BAYSE_API_URL", "https://relay.bayse.markets").strip()
    if not public_key or not secret_key:
        raise RuntimeError("BAYSE_PUBLIC_KEY and BAYSE_SECRET_KEY must be set")
    return BayseClient(public_key=public_key, secret_key=secret_key, base_url=base_url)


_client: Optional[BayseClient] = None


def get_client() -> BayseClient:
    global _client
    if _client is None:
        _client = _build_client()
    return _client


async def scan_markets(page: int = 1, size: int = 20) -> list[dict[str, Any]]:
    """Fetch open prediction markets from Bayse.

    Returns a list of market events with prices, titles, and IDs.
    """
    try:
        client = get_client()
        events = client.get_open_events(page=page, size=size)
        results = []
        for event in events:
            results.append({
                "event_id": event.get("id") or event.get("eventId", ""),
                "title": event.get("title") or event.get("name", "Untitled"),
                "yes_price": event.get("yesPrice") or event.get("yes_price"),
                "no_price": event.get("noPrice") or event.get("no_price"),
                "volume": event.get("volume"),
                "status": event.get("status"),
            })
        logger.info(f"Scanned {len(results)} Bayse markets")
        return results
    except Exception as exc:
        logger.error(f"Market scan failed: {exc}")
        return [{"error": str(exc)}]


async def get_portfolio() -> dict[str, Any]:
    """Get current portfolio/balance from Bayse."""
    try:
        client = get_client()
        portfolio = client.get_portfolio()
        balance = client.get_balance()
        return {
            "portfolio": portfolio,
            "balance": balance,
        }
    except Exception as exc:
        logger.error(f"Portfolio fetch failed: {exc}")
        return {"error": str(exc)}


async def place_trade(
    event_id: str,
    market_id: str,
    side: str,
    amount: float,
    outcome: str = "YES",
    price: float = 0.0,
    currency: str = "USD",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Place a trade on Bayse Markets.

    Args:
        event_id: The event ID.
        market_id: The market ID within the event.
        side: 'buy' or 'sell'.
        amount: Trade amount in the specified currency.
        outcome: 'YES' or 'NO'.
        price: Limit price (0 for market order).
        currency: Trade currency (default USD).
        dry_run: If True, simulates without executing.

    Returns:
        Trade result dict.
    """
    if dry_run:
        logger.info(f"[DRY RUN] Trade: {side} {outcome} on {market_id} for ${amount}")
        return {
            "dry_run": True,
            "side": side,
            "outcome": outcome,
            "amount": amount,
            "price": price,
            "currency": currency,
            "market_id": market_id,
            "event_id": event_id,
            "status": "simulated",
        }
    try:
        client = get_client()
        result = client.place_order(
            event_id=event_id,
            market_id=market_id,
            side=side,
            outcome=outcome,
            amount=amount,
            currency=currency,
            order_type="LIMIT" if price else "MARKET",
            price=price if price else None,
        )
        logger.info(f"Trade executed: {side} {outcome} on {market_id}")
        return result
    except Exception as exc:
        logger.error(f"Trade failed: {exc}")
        return {"error": str(exc), "status": "failed"}
