from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable, Iterable, Optional

from .bayse_client import BayseClient

try:
    import websockets
except Exception:  # pragma: no cover - optional dependency
    websockets = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketQuoteUpdate:
    market_id: str
    event_id: Optional[str] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    midpoint: Optional[float] = None
    timestamp: Optional[str] = None
    source: str = 'websocket'
    received_at: float = field(default_factory=time.time)
    raw: dict[str, Any] = field(default_factory=dict)


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _unwrap_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ('data', 'result', 'quote', 'ticker', 'market'):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _extract_market_id(payload: dict[str, Any]) -> str:
    market = payload.get('market')
    if isinstance(market, dict):
        nested = market.get('id') or market.get('marketId') or market.get('market_id')
        if nested:
            return str(nested).strip()
    for key in ('marketId', 'market_id', 'id'):
        value = payload.get(key)
        if value:
            return str(value).strip()
    return ''


def _extract_event_id(payload: dict[str, Any]) -> str:
    for key in ('eventId', 'event_id', 'event'):
        value = payload.get(key)
        if value:
            return str(value).strip()
    market = payload.get('market')
    if isinstance(market, dict):
        value = market.get('eventId') or market.get('event_id')
        if value:
            return str(value).strip()
    return ''


def _extract_update(payload: Any, source: str) -> Optional[MarketQuoteUpdate]:
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode('utf-8')
        except Exception:
            return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None

    base = _unwrap_payload(payload)
    market_id = _extract_market_id(payload) or _extract_market_id(base)
    if not market_id:
        return None

    return MarketQuoteUpdate(
        market_id=market_id,
        event_id=_extract_event_id(payload) or _extract_event_id(base) or None,
        bid=_coerce_float(_mapping_value(base, 'bid', 'bestBid', 'bidPrice')),
        ask=_coerce_float(_mapping_value(base, 'ask', 'bestAsk', 'askPrice')),
        last=_coerce_float(_mapping_value(base, 'last', 'lastPrice', 'price', 'tradePrice')),
        midpoint=_coerce_float(_mapping_value(base, 'midpoint', 'mid', 'midPrice')),
        timestamp=str(_mapping_value(base, 'timestamp', 'ts', 'time', 'updatedAt') or '').strip() or None,
        source=source,
        raw=dict(payload),
    )


class RealtimeFeed:
    def __init__(
        self,
        client: BayseClient,
        *,
        websocket_url: Optional[str] = None,
        poll_interval: float = 10.0,
        reconnect_delay: float = 5.0,
        subscription_message: Optional[dict[str, Any]] = None,
    ) -> None:
        self.client = client
        self.websocket_url = websocket_url.strip() if websocket_url else ''
        self.poll_interval = max(1.0, float(poll_interval))
        self.reconnect_delay = max(1.0, float(reconnect_delay))
        self.subscription_message = subscription_message
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._updates: dict[str, MarketQuoteUpdate] = {}
        self._markets: dict[str, str] = {}
        self._on_update: Optional[Callable[[MarketQuoteUpdate], None]] = None
        self._subscriptions_dirty = threading.Event()

    def subscribe_market(self, market_id: str, event_id: Optional[str] = None) -> None:
        market = str(market_id or '').strip()
        if not market:
            return
        with self._lock:
            if event_id:
                self._markets[market] = str(event_id).strip()
            elif market not in self._markets:
                self._markets[market] = ''
        self._subscriptions_dirty.set()

    def sync_markets(self, events: Iterable[dict[str, Any]]) -> None:
        for event in events:
            if not isinstance(event, dict):
                continue
            market_id = str(
                event.get('marketId')
                or event.get('market_id')
                or event.get('id')
                or ''
            ).strip()
            if not market_id:
                continue
            event_id = str(event.get('eventId') or event.get('event_id') or event.get('id') or '').strip() or None
            self.subscribe_market(market_id, event_id=event_id)

    def start(self, on_update: Callable[[MarketQuoteUpdate], None]) -> None:
        with self._lock:
            self._on_update = on_update
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name='bayse-realtime-feed', daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def snapshot(self) -> dict[str, MarketQuoteUpdate]:
        with self._lock:
            return dict(self._updates)

    def latest_for_market(self, market_id: str) -> Optional[MarketQuoteUpdate]:
        with self._lock:
            return self._updates.get(str(market_id).strip())

    def age_seconds(self, market_id: str) -> Optional[float]:
        update = self.latest_for_market(market_id)
        if update is None:
            return None
        return max(0.0, time.time() - update.received_at)

    def _publish(self, update: MarketQuoteUpdate) -> None:
        with self._lock:
            self._updates[update.market_id] = update
            callback = self._on_update
        if callback is not None:
            try:
                callback(update)
            except Exception as exc:  # pragma: no cover - callback safety
                logger.warning('Realtime quote callback failed: %s', exc)

    def _subscription_payload(self) -> Optional[dict[str, Any]]:
        with self._lock:
            markets = sorted(self._markets.keys())
            event_ids = sorted({event_id for event_id in self._markets.values() if event_id})
        if not markets and not event_ids:
            return None
        if self.subscription_message is not None:
            return dict(self.subscription_message)
        payload: dict[str, Any] = {'type': 'subscribe'}
        if markets:
            payload['marketIds'] = markets
        if event_ids:
            payload['eventIds'] = event_ids
        return payload

    def _run(self) -> None:
        if self.websocket_url and websockets is not None:
            while not self._stop_event.is_set():
                try:
                    asyncio.run(self._run_websocket())
                    return
                except Exception as exc:
                    logger.warning('Realtime websocket feed failed, falling back to polling: %s', exc)
                    if self._stop_event.wait(self.reconnect_delay):
                        return
        self._run_polling()

    async def _run_websocket(self) -> None:
        assert websockets is not None
        async with websockets.connect(self.websocket_url, ping_interval=20, ping_timeout=20, close_timeout=10) as socket:
            await self._send_subscription(socket)
            while not self._stop_event.is_set():
                try:
                    message = await asyncio.wait_for(socket.recv(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    try:
                        await socket.ping()
                    except Exception:
                        return
                    continue
                except Exception:
                    return

                update = _extract_update(message, source='websocket')
                if update is not None:
                    self._publish(update)

                if self._subscriptions_dirty.is_set():
                    self._subscriptions_dirty.clear()
                    await self._send_subscription(socket)

    async def _send_subscription(self, socket: Any) -> None:
        payload = self._subscription_payload()
        if payload is None:
            return
        try:
            await socket.send(json.dumps(payload))
        except Exception as exc:
            logger.warning('Realtime websocket subscription failed: %s', exc)

    def _run_polling(self) -> None:
        logger.info('Realtime feed using REST polling fallback')
        while not self._stop_event.is_set():
            with self._lock:
                markets = list(self._markets.items())
            for market_id, event_id in markets:
                if self._stop_event.is_set():
                    break
                try:
                    payload = self.client.get_market_ticker(market_id)
                except Exception as exc:
                    logger.debug('Ticker refresh failed for %s: %s', market_id, exc)
                    continue
                if isinstance(payload, dict):
                    payload = dict(payload)
                    if 'marketId' not in payload and 'market_id' not in payload:
                        payload['marketId'] = market_id
                    if event_id and 'eventId' not in payload and 'event_id' not in payload:
                        payload['eventId'] = event_id
                update = _extract_update(payload, source='polling')
                if update is not None:
                    self._publish(update)
            self._stop_event.wait(self.poll_interval)


class QuoteManager:
    def __init__(self, client: BayseClient, *, websocket_url: Optional[str] = None, poll_interval: float = 10.0) -> None:
        self.feed = RealtimeFeed(client, websocket_url=websocket_url, poll_interval=poll_interval)
        self._lock = threading.RLock()
        self._latest: dict[str, MarketQuoteUpdate] = {}
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.feed.start(self._handle_update)

    def stop(self) -> None:
        self.feed.stop()

    def sync_markets(self, events: Iterable[dict[str, Any]]) -> None:
        self.feed.sync_markets(events)

    def snapshot(self) -> dict[str, MarketQuoteUpdate]:
        with self._lock:
            return dict(self._latest)

    def latest_for_market(self, market_id: str) -> Optional[MarketQuoteUpdate]:
        with self._lock:
            return self._latest.get(str(market_id).strip())

    def quote_age_seconds(self, market_id: str) -> Optional[float]:
        update = self.latest_for_market(market_id)
        if update is None:
            return None
        return max(0.0, time.time() - update.received_at)

    def markets_due_for_refresh(self, max_age_seconds: float = 30.0) -> list[str]:
        threshold = max(1.0, float(max_age_seconds))
        now = time.time()
        with self._lock:
            return [market_id for market_id, update in self._latest.items() if now - update.received_at >= threshold]

    def _handle_update(self, update: MarketQuoteUpdate) -> None:
        with self._lock:
            self._latest[update.market_id] = update
        logger.debug(
            'Realtime quote update: market=%s bid=%s ask=%s source=%s',
            update.market_id,
            update.bid,
            update.ask,
            update.source,
        )
