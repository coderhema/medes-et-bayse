"""medes-et-bayse: Main entry point for the Bayse Markets trading bot."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

from bot.bayse_client import BayseClient
from bot.poke_client import PokeClient
from bot.realtime_feed import QuoteManager
from bot.strategies.arbitrage import ArbitrageStrategy
from bot.strategies.kelly import KellyStrategy
from bot.strategies.market_maker import MarketMakerStrategy, extract_inventory_units
from bot.strategies.quant_advisory import QuantAdvisory
from bot.strategies.spread_capture import SpreadCaptureEngine

try:
    from bot.telegram_handler import build_telegram_handler_from_env
except Exception as exc:  # pragma: no cover - optional dependency fallback
    build_telegram_handler_from_env = None
    logger.warning(f"Telegram handler unavailable: {exc}")

load_dotenv()
PLACEHOLDER_ORDER_VALUES = {"", "n/a", "na", "none", "null", "unknown", "-"}


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _parse_timestamp(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    cleaned = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return datetime.now(timezone.utc)


def _market_id_from_event(event: dict) -> str:
    return str(event.get("marketId") or event.get("market_id") or event.get("id") or "").strip()


def _is_suspicious_event_id(event_id: str, market_id: str, outcome_id: str = "") -> bool:
    normalized_event = str(event_id or "").strip()
    if not normalized_event:
        return True
    lowered_event = normalized_event.lower()
    if lowered_event in PLACEHOLDER_ORDER_VALUES:
        return True
    if any(char.isspace() for char in normalized_event):
        return True
    lowered_market = str(market_id or "").strip().lower()
    lowered_outcome = str(outcome_id or "").strip().lower()
    return lowered_event in {"yes", "no", "buy", "sell", lowered_market, lowered_outcome}


def _attach_live_quotes(events: list[dict], quote_manager: Optional[QuoteManager]) -> None:
    if quote_manager is None:
        return
    snapshot = quote_manager.snapshot()
    if not snapshot:
        return
    for event in events:
        if not isinstance(event, dict):
            continue
        market_id = _market_id_from_event(event)
        if not market_id:
            continue
        update = snapshot.get(market_id)
        if update is None:
            continue
        event["liveQuote"] = {
            "market_id": update.market_id,
            "event_id": update.event_id,
            "bid": update.bid,
            "ask": update.ask,
            "last": update.last,
            "midpoint": update.midpoint,
            "timestamp": update.timestamp,
            "source": update.source,
        }
        event["liveQuoteAgeSeconds"] = quote_manager.quote_age_seconds(market_id)


def _yes_outcome_id_from_event(event: dict) -> str:
    """Extract the YES outcome ID from an event dict using common key patterns."""
    for key in ('yesOutcomeId', 'yes_outcome_id', 'outcomeId', 'outcome_id'):
        value = event.get(key)
        if value:
            return str(value).strip()
    market = event.get('market')
    if isinstance(market, dict):
        for key in ('yesOutcomeId', 'yes_outcome_id', 'outcomeId', 'outcome_id'):
            value = market.get(key)
            if value:
                return str(value).strip()
        outcomes = market.get('outcomes') or market.get('options')
        if isinstance(outcomes, list):
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                label = str(outcome.get('label') or outcome.get('name') or outcome.get('title') or '').strip().lower()
                if label in {'yes', 'y', 'true'}:
                    outcome_id = outcome.get('id') or outcome.get('outcomeId') or outcome.get('outcome_id')
                    if outcome_id:
                        return str(outcome_id).strip()
    return ''


def _extract_mid_from_update(update: Any) -> Optional[float]:
    """Derive a mid-price from a MarketQuoteUpdate (WebSocket feed snapshot)."""
    if update is None:
        return None
    bid = update.bid
    ask = update.ask
    if bid is not None and ask is not None and ask >= bid:
        return (bid + ask) / 2.0
    if update.midpoint is not None:
        return float(update.midpoint)
    return None


def run_spread_capture_cycle(
    client: BayseClient,
    engine: SpreadCaptureEngine,
    quote_manager: QuoteManager,
    series_slug: str,
    *,
    dry_run: bool = True,
    currency: str = 'USD',
) -> None:
    """One iteration of the spread-capture quote-refresh loop.

    Steps:
    1. Discover the active series market.
    2. Subscribe the orderbook feed and derive the current mid-price
       (WebSocket snapshot first, REST orderbook fallback).
    3. Load the current portfolio to compute inventory.
    4. Call ``engine.refresh_quotes`` — cancels stale orders and places fresh ones
       only when the mid has moved beyond ``reprice_threshold``.
    5. Burn matched YES/NO pairs to recycle USD capital.
    """
    event = engine.discover_series_market(series_slug)
    if event is None:
        logger.info('[SC] No active market found for series {!r}', series_slug)
        return

    market_id = _market_id_from_event(event)
    event_id = str(event.get('id') or event.get('eventId') or '').strip()
    outcome_id = _yes_outcome_id_from_event(event)
    title = str(event.get('title') or event.get('name') or market_id).strip()

    if not market_id or not event_id:
        logger.warning('[SC] Cannot resolve market/event ID from series market {!r}', series_slug)
        return

    quote_manager.feed.subscribe_market(market_id, event_id=event_id)

    update = quote_manager.latest_for_market(market_id)
    mid_price = _extract_mid_from_update(update)
    if mid_price is None:
        mid_price = engine.get_mid_price(market_id)

    inventory_units = 0.0
    try:
        portfolio = client.get_portfolio()
        inventory_units = extract_inventory_units(
            portfolio, event_id=event_id, market_id=market_id, outcome_id=outcome_id
        )
    except Exception as exc:
        logger.warning('[SC] Portfolio unavailable for inventory calc: {}', exc)

    logger.info(
        '[SC] {} | series={!r} | mid={} | inventory={:.4f}',
        title, series_slug, f'{mid_price:.4f}' if mid_price is not None else 'n/a', inventory_units,
    )

    results = engine.refresh_quotes(
        event,
        mid_price,
        inventory_units=inventory_units,
        event_id=event_id,
        market_id=market_id,
        outcome_id=outcome_id,
        currency=currency,
    )
    if results:
        logger.info('[SC] {} quote action(s) for {}', len(results), market_id)

    if not dry_run:
        engine.burn_pairs(market_id, quantity=1)


def _execute_quote_plan(client: BayseClient, quote_plan: dict, dry_run: bool, currency: str) -> list[dict]:
    placements: list[dict] = []
    event_id = str(quote_plan.get('event_id') or '').strip()
    market_id = str(quote_plan.get('market_id') or '').strip()
    outcome_id = str(quote_plan.get('outcome_id') or '').strip()

    for order in quote_plan.get('quote_orders', []):
        if not isinstance(order, dict):
            continue
        side = str(order.get('side') or '').strip().upper()
        price = float(order.get('price') or 0.0)
        amount = float(order.get('amount') or 0.0)
        if not side or price <= 0 or amount <= 0:
            continue
        if dry_run:
            logger.info(
                f"[DRY RUN] Quote {quote_plan.get('event_title', 'unknown event')} | {side} @ {price:.4f} x {amount:.2f}"
            )
            result = {
                'dry_run': True,
                'side': side,
                'price': round(price, 4),
                'amount': round(amount, 2),
            }
        else:
            if _is_suspicious_event_id(event_id, market_id, outcome_id):
                logger.warning(
                    "Skipping quote placement due to suspicious IDs eventId={!r} marketId={!r} outcomeId={!r}",
                    event_id,
                    market_id,
                    outcome_id,
                )
                continue
            logger.info(
                "place_order canonical identifiers: eventId={} marketId={} outcomeId={} side={} amount={} currency={} price={}",
                event_id,
                market_id,
                outcome_id,
                side,
                amount,
                currency,
                price,
            )
            result = client.place_post_only_limit_order(
                event_id=event_id,
                market_id=market_id,
                outcome_id=outcome_id,
                side=side,
                amount=amount,
                price=price,
                currency=currency,
            )
        placements.append(result)

    return placements


def _resolve_trade_args(signal: dict) -> tuple[str, str, str, str, float]:
    side = str(signal.get("side", "")).upper()
    market_id = str(
        signal.get("market_id")
        or signal.get("marketId")
        or ""
    ).strip()
    outcome_label = str(signal.get("outcome_label") or signal.get("outcome") or "").strip().upper()
    if not outcome_label:
        raw_side = side.lower()
        outcome_label = "YES" if raw_side in {"yes", "buy", "long"} else "NO"
    event_id = str(signal.get("event_id") or signal.get("eventId") or "").strip()

    if side == "YES" or side == "BUY":
        price = signal.get("yes_price") or signal.get("market_prob") or signal.get("price")
    elif side == "NO" or side == "SELL":
        price = signal.get("no_price") or signal.get("market_prob") or signal.get("price")
    else:
        price = signal.get("price") or signal.get("market_prob")

    if price is None:
        price = 0.0

    currency = _env("BAYSE_CURRENCY", default="USD")
    return event_id, market_id, outcome_label, currency, float(price)


def _format_trade_alert(trade: dict) -> str:
    timestamp = trade.get("timestamp", "")
    market_id = trade.get("marketId", "unknown")
    outcome = trade.get("outcome", "unknown")
    side = trade.get("side", "unknown")
    price = trade.get("price", 0)
    quantity = trade.get("quantity", 0)
    return (
        f"New Bayse trade detected\n"
        f"Market: {market_id}\n"
        f"Outcome: {outcome}\n"
        f"Side: {side}\n"
        f"Price: {float(price):.4f}\n"
        f"Quantity: {float(quantity):.2f}\n"
        f"Time: {timestamp}"
    )


def _format_event_alert(event: dict) -> str:
    return (
        f"New active market detected\n"
        f"Title: {event.get('title') or event.get('name') or 'Untitled market'}\n"
        f"Event ID: {event.get('id', 'unknown')}"
    )


def _notify(poke: PokeClient, message: str, payload: Optional[dict] = None, level: str = "info") -> None:
    try:
        poke.notify(message, payload=payload, level=level)
    except Exception as exc:
        logger.error(f"Notification failed: {exc}")


def run_cycle(
    client: BayseClient,
    poke: PokeClient,
    strategies: list,
    dry_run: bool = True,
    bayse_user_id: str = "",
    quote_manager: Optional[QuoteManager] = None,
    quote_currency: str = "USD",
) -> None:
    logger.info("Starting trading cycle...")

    events = client.get_open_events(page=1, size=50)
    logger.info(f"Fetched {len(events)} open markets")
    if quote_manager is not None:
        quote_manager.sync_markets(events)
        _attach_live_quotes(events, quote_manager)
        logger.info(f"Realtime quote manager tracking {len(quote_manager.snapshot())} market(s)")

    executed = []
    all_signals = []
    portfolio = None
    if any(isinstance(strategy, MarketMakerStrategy) for strategy in strategies):
        try:
            portfolio = client.get_portfolio()
            logger.debug("Fetched portfolio snapshot for market making")
        except Exception as exc:
            logger.warning(f"Portfolio snapshot unavailable for market making: {exc}")

    for strategy in strategies:
        if isinstance(strategy, MarketMakerStrategy):
            quote_plans = strategy.generate_quotes(events, portfolio=portfolio)
            if quote_plans:
                logger.info(f"[{strategy.name}] Built {len(quote_plans)} quote plan(s)")
            for quote_plan in quote_plans:
                placements = _execute_quote_plan(client, quote_plan, dry_run=dry_run, currency=quote_currency)
                executed.append({**quote_plan, "placements": placements})
            continue

        signals = strategy.scan(events)
        if signals:
            logger.info(f"[{strategy.name}] Found {len(signals)} signal(s)")
            all_signals.extend(signals)

    if not all_signals and not executed:
        logger.debug("No actionable signals this cycle.")
        return

    for signal in all_signals:
        logger.info(
            f"Signal: {signal['event_title']} | Side: {signal['side']} | Edge: {signal['edge']:.2%} | Stake: $"
            + format(float(signal['stake']), '.2f')
            + " USDC"
        )
        if not dry_run:
            event_id, market_id, outcome_label, currency, price = _resolve_trade_args(signal)
            if not event_id or not market_id or not outcome_label:
                logger.warning(
                    f"Skipping live trade for {signal.get('event_title', 'unknown event')} because canonical event/market/outcome identifiers are missing."
                )
                executed.append({**signal, "trade_result": {"skipped": True, "reason": "missing event_id/market_id/outcome_label"}})
                continue
            if _is_suspicious_event_id(event_id, market_id, outcome_label):
                logger.warning(
                    "Skipping live trade for {} because event_id={} is suspicious for market_id={} outcome_id={}.",
                    signal.get("event_title", "unknown event"),
                    event_id,
                    market_id,
                    outcome_label,
                )
                executed.append({**signal, "trade_result": {"skipped": True, "reason": "suspicious_event_id"}})
                continue

            logger.info(
                "place_order canonical identifiers: eventId={} marketId={} outcomeId={} side={} amount={} currency={} price={}",
                event_id,
                market_id,
                outcome_label,
                str(signal["side"]),
                float(signal["stake"]),
                currency,
                price,
            )
            result = client.place_order(
                event_id=event_id,
                market_id=market_id,
                side=str(signal["side"]),
                outcome=outcome_label,
                price=price,
                amount=float(signal["stake"]),
                currency=currency,
                order_type="LIMIT" if price else "MARKET",
                time_in_force="GTC" if price else None,
            )
            # Log raw API response so the JSON structure can be inspected.
            logger.info("place_order raw response: %s", json.dumps(result, default=str))
            print(json.dumps({"place_order_raw": result}, default=str), flush=True)
            signal["trade_result"] = result
            executed.append(signal)
        else:
            logger.info("[DRY RUN] Trade not placed.")
            executed.append({**signal, "dry_run": True})

    _notify(
        poke,
        f"medes-et-bayse: Cycle complete. {len(executed)} action(s) {'simulated' if dry_run else 'executed'}.",
        payload={"user_id": bayse_user_id, "actions": executed},
        level="success",
    )
    logger.info("Cycle complete.")


def monitor_bayse_activity(
    client: BayseClient,
    poke: PokeClient,
    poll_interval: int,
    stop_event: threading.Event,
) -> None:
    seen_trade_ids: set[str] = set()
    seen_event_ids: set[str] = set()

    try:
        for trade in client.get_trades(limit=100):
            trade_id = str(trade.get("id", "")).strip()
            if trade_id:
                seen_trade_ids.add(trade_id)
        for event in client.get_open_events(page=1, size=100):
            event_id = str(event.get("id", "")).strip()
            if event_id:
                seen_event_ids.add(event_id)
    except Exception as exc:
        logger.warning(f"Initial Bayse activity snapshot failed: {exc}")

    logger.info(f"Started Bayse activity polling every {poll_interval}s")

    while not stop_event.is_set():
        try:
            trades = client.get_trades(limit=50)
            new_trades = []
            for trade in sorted(trades, key=lambda item: _parse_timestamp(str(item.get("timestamp", "")))):
                trade_id = str(trade.get("id", "")).strip()
                if trade_id and trade_id not in seen_trade_ids:
                    seen_trade_ids.add(trade_id)
                    new_trades.append(trade)

            for trade in new_trades:
                _notify(poke, _format_trade_alert(trade), payload={"trade": trade}, level="info")

            events = client.get_open_events(page=1, size=100)
            new_events = []
            for event in events:
                event_id = str(event.get("id", "")).strip()
                if event_id and event_id not in seen_event_ids:
                    seen_event_ids.add(event_id)
                    new_events.append(event)

            for event in new_events:
                _notify(poke, _format_event_alert(event), payload={"event": event}, level="info")
        except Exception as exc:
            logger.error(f"Bayse activity polling error: {exc}")
        stop_event.wait(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="medes-et-bayse trading bot")
    parser.add_argument("--scan-only", action="store_true", help="Scan markets and log signals without placing trades")
    parser.add_argument("--strategy", choices=["kelly", "arbitrage", "market-making", "all"], default="all", help="Strategy to run")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--series", default="", help="Series slug for spread-capture market-maker (e.g. nfl-sunday-showcase)")
    args = parser.parse_args()

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    bankroll = float(os.getenv("BANKROLL", "100.0"))
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    trade_poll_interval = int(os.getenv("TRADE_POLL_INTERVAL_SECONDS", "30"))

    if args.scan_only:
        dry_run = True

    bayse_public_key = _env("BAYSE_PUBLIC_KEY")
    bayse_secret_key = _env("BAYSE_SECRET_KEY")
    bayse_base_url = _env("BAYSE_API_URL", default="https://relay.bayse.markets")
    bayse_user_id = _env("BAYSE_USER_ID")

    if not bayse_public_key:
        logger.warning("BAYSE_PUBLIC_KEY is not set.")
    if not bayse_secret_key:
        logger.warning("BAYSE_SECRET_KEY is not set.")
    if not bayse_user_id:
        logger.warning("BAYSE_USER_ID is not set.")

    client = BayseClient(public_key=bayse_public_key, secret_key=bayse_secret_key, base_url=bayse_base_url)
    quote_manager = QuoteManager(client, websocket_url=_env("BAYSE_WS_URL"), poll_interval=float(os.getenv("QUOTE_POLL_INTERVAL_SECONDS", "10")))

    telegram_handler = None
    if build_telegram_handler_from_env:
        telegram_handler = build_telegram_handler_from_env()
        if telegram_handler:
            telegram_handler.attach_bayse_client(client)

    poke = PokeClient(api_key=_env("POKE_API_KEY"), webhook_url=_env("POKE_WEBHOOK_URL"), telegram=telegram_handler)

    min_edge = float(os.getenv("MIN_EDGE", "0.03"))
    max_position_fraction = float(os.getenv("MAX_POSITION_FRACTION", "0.05"))
    quote_currency = _env("BAYSE_CURRENCY", default="USD")

    quant_advisory = QuantAdvisory(min_edge=min_edge)
    if telegram_handler:
        telegram_handler.attach_quant_advisory(quant_advisory)

    strategy_map = {
        "kelly": [KellyStrategy(bankroll=bankroll, min_edge=min_edge, max_fraction=max_position_fraction)],
        "arbitrage": [ArbitrageStrategy(bankroll=bankroll, min_edge=min_edge)],
        "market-making": [MarketMakerStrategy(bankroll=bankroll, min_edge=min_edge)],
        "all": [
            KellyStrategy(bankroll=bankroll, min_edge=min_edge, max_fraction=max_position_fraction),
            ArbitrageStrategy(bankroll=bankroll, min_edge=min_edge),
            MarketMakerStrategy(bankroll=bankroll, min_edge=min_edge),
        ],
    }

    strategies = strategy_map[args.strategy]

    series_slug = args.series or _env("SERIES_SLUG")
    spread_engine: Optional[SpreadCaptureEngine] = None
    if series_slug:
        spread_engine = SpreadCaptureEngine(
            client,
            bankroll=bankroll,
            half_spread=float(os.getenv("SC_HALF_SPREAD", "0.02")),
            order_size=float(os.getenv("SC_ORDER_SIZE", "10.0")),
            reprice_threshold=float(os.getenv("SC_REPRICE_THRESHOLD", "0.005")),
            pre_close_seconds=float(os.getenv("SC_PRE_CLOSE_SECONDS", "300")),
            inventory_skew=float(os.getenv("SC_INVENTORY_SKEW", "0.60")),
            max_position_fraction=max_position_fraction,
            dry_run=dry_run,
        )
        logger.info(f"Spread-capture engine enabled for series {series_slug!r}")

    if not args.scan_only:
        quote_manager.start()
        logger.info("Realtime quote management enabled")

    if not args.once and not args.scan_only:
        stop_event = threading.Event()
        if telegram_handler:
            telegram_handler.start_background_polling()
        threading.Thread(target=monitor_bayse_activity, args=(client, poke, trade_poll_interval, stop_event), daemon=True).start()
        logger.info(f"Starting polling loop every {poll_interval}s...")
        try:
            while True:
                try:
                    run_cycle(client, poke, strategies, dry_run=dry_run, bayse_user_id=bayse_user_id, quote_manager=quote_manager, quote_currency=quote_currency)
                    if spread_engine is not None:
                        run_spread_capture_cycle(client, spread_engine, quote_manager, series_slug, dry_run=dry_run, currency=quote_currency)
                except Exception as e:
                    logger.error(f"Cycle error: {e}")
                    _notify(poke, f"medes-et-bayse ERROR: {e}", level="error")
                time.sleep(poll_interval)
        except KeyboardInterrupt:
            stop_event.set()
            quote_manager.stop()
    else:
        run_cycle(client, poke, strategies, dry_run=dry_run, bayse_user_id=bayse_user_id, quote_manager=quote_manager, quote_currency=quote_currency)
        if spread_engine is not None:
            run_spread_capture_cycle(client, spread_engine, quote_manager, series_slug, dry_run=dry_run, currency=quote_currency)
        quote_manager.stop()


if __name__ == "__main__":
    main()
