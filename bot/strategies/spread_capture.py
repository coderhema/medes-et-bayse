"""Spread-capture market-maker strategy for Bayse Markets.

Implements the approach described at:
https://www.bayse.markets/blog/build-a-spread-capture-market-maker-bot-on-bayse-markets

Workflow per refresh cycle:
1. Discover the active series market via the REST API.
2. Consume the current best bid/ask from the live orderbook feed (WebSocket or REST).
3. Compute mid-price and derive symmetric bid/ask quotes with inventory-aware skew.
4. Cancel and replace a resting order only when the target price has drifted by more
   than ``reprice_threshold`` — avoiding unnecessary API round-trips.
5. Mint YES shares before placing a SELL quote when wallet inventory is thin.
6. Burn matched YES/NO pairs after fills to recycle USD capital.
7. Stop quoting when the market is within ``pre_close_seconds`` of its scheduled close.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from bot.bayse_client import BayseClient


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_close_time(value: str) -> datetime:
    """Parse an ISO-8601 close-time string into an aware datetime."""
    cleaned = value.replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# SpreadCaptureEngine
# ---------------------------------------------------------------------------

class SpreadCaptureEngine:
    """Resting-quote engine that implements the spread-capture strategy.

    Parameters
    ----------
    client:
        Authenticated Bayse REST API client.
    bankroll:
        Total capital available for quoting (USD).
    half_spread:
        Half the target bid-ask spread (probability units). Default 0.02 → full
        spread of 4 cents on a $1 contract.
    order_size:
        Notional size of each resting order in USD.
    reprice_threshold:
        Minimum price move (probability units) required before cancelling and
        replacing a resting order. Avoids thrashing when the mid barely moves.
    pre_close_seconds:
        Stop quoting this many seconds before the scheduled market close.
    inventory_skew:
        Fraction of ``half_spread`` applied as a skew correction per unit of
        inventory ratio. Set to 0 to disable skewing.
    max_position_fraction:
        Maximum position as a fraction of ``bankroll``. Used to normalise the
        inventory ratio for skew computation.
    dry_run:
        When True, log intended actions but do not call the API.
    """

    def __init__(
        self,
        client: BayseClient,
        bankroll: float,
        *,
        half_spread: float = 0.02,
        order_size: float = 10.0,
        reprice_threshold: float = 0.005,
        pre_close_seconds: float = 300.0,
        inventory_skew: float = 0.60,
        max_position_fraction: float = 0.03,
        dry_run: bool = True,
    ) -> None:
        self.client = client
        self.bankroll = float(bankroll)
        self.half_spread = float(half_spread)
        self.order_size = float(order_size)
        self.reprice_threshold = float(reprice_threshold)
        self.pre_close_seconds = float(pre_close_seconds)
        self.inventory_skew = float(inventory_skew)
        self.max_position_fraction = float(max_position_fraction)
        self.dry_run = bool(dry_run)
        self._lock = threading.Lock()
        # {market_id: {side: {'order_id': str, 'price': float, 'amount': float}}}
        self._active_orders: dict[str, dict[str, dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover_series_market(self, series_slug: str) -> Optional[dict]:
        """Return the first open event for *series_slug*, or None."""
        if not series_slug:
            return None
        try:
            events = self.client.get_events_by_series(series_slug)
        except Exception as exc:
            logger.warning('[SC] Series discovery failed for {!r}: {}', series_slug, exc)
            return None
        if not isinstance(events, list):
            return None
        for event in events:
            if isinstance(event, dict) and str(event.get('status', '')).lower() in {'open', 'active', 'live'}:
                return event
        return None

    def get_mid_price(self, market_id: str) -> Optional[float]:
        """Fetch the current mid-price via the REST orderbook endpoint (REST fallback)."""
        try:
            ob = self.client.get_order_book(market_id)
        except Exception as exc:
            logger.debug('[SC] Orderbook fetch failed for {}: {}', market_id, exc)
            return None
        bid = _safe_float(ob.get('bestBid') or ob.get('bid'))
        ask = _safe_float(ob.get('bestAsk') or ob.get('ask'))
        mid = _safe_float(ob.get('midpoint') or ob.get('mid'))
        if bid is not None and ask is not None and ask >= bid:
            return (bid + ask) / 2.0
        return mid

    def refresh_quotes(
        self,
        event: dict,
        mid_price: Optional[float],
        *,
        inventory_units: float = 0.0,
        event_id: str = '',
        market_id: str = '',
        outcome_id: str = '',
        currency: str = 'USD',
    ) -> list[dict]:
        """Cancel stale resting orders and place fresh quotes around *mid_price*.

        Quotes are only repriced when the target price shifts by more than
        ``reprice_threshold``, avoiding unnecessary API round-trips.

        Returns a list of order results (one per side that was repriced or placed).
        An empty list means no action was taken this cycle.
        """
        if mid_price is None or not market_id:
            return []

        if self.should_stop_quoting(event):
            title = str(event.get('title') or event.get('name') or market_id).strip()
            logger.info('[SC] Pre-close guard triggered for "{}"; cancelling all resting quotes', title)
            self._cancel_all(market_id)
            return []

        # Inventory-aware skew
        max_units = max(1.0, self.bankroll * self.max_position_fraction) / max(mid_price, 0.01)
        inventory_ratio = _clamp(inventory_units / max_units, -1.0, 1.0) if max_units > 0 else 0.0
        skew = inventory_ratio * self.inventory_skew * self.half_spread

        bid_price = _clamp(mid_price - self.half_spread - skew, 0.01, 0.99)
        ask_price = _clamp(mid_price + self.half_spread - skew, 0.01, 0.99)
        if bid_price >= ask_price:
            ask_price = _clamp(bid_price + 0.01, 0.01, 0.99)

        results: list[dict] = []
        with self._lock:
            market_orders = self._active_orders.setdefault(market_id, {})

            if inventory_ratio >= 0.95:
                # Max-long: drop the BUY side and only maintain the SELL side
                self._cancel_side(market_id, 'BUY', market_orders)
                result = self._refresh_side(
                    'SELL', ask_price, market_orders,
                    event_id=event_id, market_id=market_id,
                    outcome_id=outcome_id, currency=currency,
                )
                if result is not None:
                    results.append(result)
            elif inventory_ratio <= -0.95:
                # Max-short: drop the SELL side and only maintain the BUY side
                self._cancel_side(market_id, 'SELL', market_orders)
                result = self._refresh_side(
                    'BUY', bid_price, market_orders,
                    event_id=event_id, market_id=market_id,
                    outcome_id=outcome_id, currency=currency,
                )
                if result is not None:
                    results.append(result)
            else:
                # Normal: maintain both sides
                # Mint YES shares before placing SELL quote if inventory is thin
                if not self.dry_run:
                    self._mint_if_needed(market_id, int(self.order_size), inventory_units, mid_price)
                for side, price in (('BUY', bid_price), ('SELL', ask_price)):
                    result = self._refresh_side(
                        side, price, market_orders,
                        event_id=event_id, market_id=market_id,
                        outcome_id=outcome_id, currency=currency,
                    )
                    if result is not None:
                        results.append(result)

        logger.debug(
            '[SC] {} | mid={:.4f} | bid={:.4f} | ask={:.4f} | inv_ratio={:.2f} | actions={}',
            market_id, mid_price, bid_price, ask_price, inventory_ratio, len(results),
        )
        return results

    def burn_pairs(self, market_id: str, quantity: int = 1) -> dict:
        """Burn *quantity* matched YES+NO pairs to recycle USD capital.

        Returns the API response dict, or ``{'dry_run': True, ...}`` in dry-run mode.
        """
        if self.dry_run:
            logger.info('[SC] [DRY RUN] burn_pairs market={} qty={}', market_id, quantity)
            return {'dry_run': True, 'market_id': market_id, 'quantity': quantity}
        try:
            result = self.client.burn_shares(market_id, quantity=quantity)
            logger.info('[SC] Burned {} pair(s) for market {}: {}', quantity, market_id, result)
            return result
        except Exception as exc:
            logger.warning('[SC] Burn failed for market {}: {}', market_id, exc)
            return {'error': str(exc), 'market_id': market_id}

    def cancel_market_quotes(self, market_id: str) -> None:
        """Cancel all resting quotes for *market_id* (e.g. on shutdown or close)."""
        with self._lock:
            self._cancel_all(market_id)

    def active_orders_snapshot(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Return a thread-safe copy of the active order state."""
        with self._lock:
            return {mid: dict(sides) for mid, sides in self._active_orders.items()}

    def should_stop_quoting(self, event: dict) -> bool:
        """Return True when the market is within ``pre_close_seconds`` of close."""
        for key in ('closesAt', 'endsAt', 'closeTime', 'close_time', 'closes_at', 'ends_at'):
            close_str = str(event.get(key) or '').strip()
            if not close_str:
                continue
            try:
                close_dt = _parse_close_time(close_str)
                remaining = (close_dt - datetime.now(timezone.utc)).total_seconds()
                return remaining <= self.pre_close_seconds
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_side(
        self,
        side: str,
        target_price: float,
        market_orders: dict[str, dict[str, Any]],
        *,
        event_id: str,
        market_id: str,
        outcome_id: str,
        currency: str,
    ) -> Optional[dict]:
        """Cancel stale order and place a fresh one only if the price has moved enough.

        Returns the new order dict, or None if no action was taken.
        """
        existing = market_orders.get(side)
        if existing is not None:
            old_price = float(existing.get('price', 0.0))
            if abs(target_price - old_price) <= self.reprice_threshold:
                # Quote still valid — keep the existing order
                return None
            self._cancel_side(market_id, side, market_orders)

        # Place a new resting post-only limit order
        if self.dry_run:
            order: dict[str, Any] = {
                'dry_run': True,
                'side': side,
                'price': round(target_price, 4),
                'amount': round(self.order_size, 2),
                'market_id': market_id,
            }
            logger.info(
                '[SC] [DRY RUN] {} {} @ {:.4f} x {:.2f}', side, market_id, target_price, self.order_size,
            )
        else:
            try:
                order = self.client.place_post_only_limit_order(
                    event_id=event_id,
                    market_id=market_id,
                    outcome_id=outcome_id,
                    side=side,
                    amount=self.order_size,
                    price=target_price,
                    currency=currency,
                )
            except Exception as exc:
                logger.warning(
                    '[SC] Order placement failed ({} {} @ {:.4f}): {}', side, market_id, target_price, exc,
                )
                return None

        order_id = str(order.get('id') or order.get('orderId') or '').strip()
        market_orders[side] = {
            'order_id': order_id,
            'price': round(target_price, 4),
            'amount': round(self.order_size, 2),
        }
        return dict(order)

    def _cancel_side(
        self,
        market_id: str,
        side: str,
        market_orders: dict[str, dict[str, Any]],
    ) -> bool:
        """Remove and cancel a resting order on one side. Returns True on success."""
        existing = market_orders.pop(side, None)
        if not existing:
            return False
        order_id = str(existing.get('order_id') or '').strip()
        if not order_id or self.dry_run:
            if self.dry_run and order_id:
                logger.info('[SC] [DRY RUN] Would cancel {} order {} for {}', side, order_id, market_id)
            return False
        try:
            self.client.cancel_order(order_id)
            logger.info('[SC] Cancelled {} order {} for market {}', side, order_id, market_id)
            return True
        except Exception as exc:
            logger.warning('[SC] Cancel failed for order {} ({}): {}', order_id, side, exc)
            return False

    def _cancel_all(self, market_id: str) -> None:
        """Cancel all resting orders for *market_id* (must be called under ``_lock``)."""
        market_orders = self._active_orders.get(market_id, {})
        for side in list(market_orders.keys()):
            self._cancel_side(market_id, side, market_orders)

    def _mint_if_needed(
        self,
        market_id: str,
        quantity: int,
        inventory_units: float,
        mid_price: float,
    ) -> bool:
        """Mint YES shares when inventory is insufficient to support the SELL quote."""
        needed = self.order_size / max(mid_price, 0.01)
        if inventory_units >= needed:
            return False
        try:
            result = self.client.mint_shares(market_id, quantity=quantity)
            logger.info('[SC] Minted {} share(s) for market {}: {}', quantity, market_id, result)
            return True
        except Exception as exc:
            logger.warning('[SC] Mint failed for market {}: {}', market_id, exc)
            return False
