     1|"""medes-et-bayse: Main entry point for the Bayse Markets trading bot."""
     2|
     3|from __future__ import annotations
     4|
     5|import argparse
     6|import os
     7|import threading
     8|import time
     9|from datetime import datetime, timezone
    10|from typing import Optional
    11|
    12|from dotenv import load_dotenv
    13|from loguru import logger
    14|
    15|from bot.bayse_client import BayseClient
    16|from bot.realtime_feed import QuoteManager
    17|from bot.strategies.arbitrage import ArbitrageStrategy
    18|from bot.strategies.kelly import KellyStrategy
    19|from bot.strategies.market_maker import MarketMakerStrategy, extract_inventory_units
    20|from bot.strategies.spread_capture import SpreadCaptureEngine
    21|
    22|try:
    23|    from bot.telegram_handler import build_telegram_handler_from_env
    24|except Exception as exc:  # pragma: no cover - optional dependency fallback
    25|    build_telegram_handler_from_env = None
    26|    logger.warning(f"Telegram handler unavailable: {exc}")
    27|
    28|load_dotenv()
    29|
    30|
    31|def _env(*names: str, default: str = "") -> str:
    32|    for name in names:
    33|        value = os.getenv(name, "").strip()
    34|        if value:
    35|            return value
    36|    return default
    37|
    38|
    39|def _parse_timestamp(value: str) -> datetime:
    40|    if not value:
    41|        return datetime.now(timezone.utc)
    42|    cleaned = value.replace("Z", "+00:00")
    43|    try:
    44|        return datetime.fromisoformat(cleaned)
    45|    except ValueError:
    46|        return datetime.now(timezone.utc)
    47|
    48|
    49|def _market_id_from_event(event: dict) -> str:
    50|    return str(event.get("marketId") or event.get("market_id") or event.get("id") or "").strip()
    51|
    52|
    53|def _attach_live_quotes(events: list[dict], quote_manager: Optional[QuoteManager]) -> None:
    54|    if quote_manager is None:
    55|        return
    56|    snapshot = quote_manager.snapshot()
    57|    if not snapshot:
    58|        return
    59|    for event in events:
    60|        if not isinstance(event, dict):
    61|            continue
    62|        market_id = _market_id_from_event(event)
    63|        if not market_id:
    64|            continue
    65|        update = snapshot.get(market_id)
    66|        if update is None:
    67|            continue
    68|        event["liveQuote"] = {
    69|            "market_id": update.market_id,
    70|            "event_id": update.event_id,
    71|            "bid": update.bid,
    72|            "ask": update.ask,
    73|            "last": update.last,
    74|            "midpoint": update.midpoint,
    75|            "timestamp": update.timestamp,
    76|            "source": update.source,
    77|        }
    78|        event["liveQuoteAgeSeconds"] = quote_manager.quote_age_seconds(market_id)
    79|
    80|
    81|def _yes_outcome_id_from_event(event: dict) -> str:
    82|    """Extract the YES outcome ID from an event dict using common key patterns."""
    83|    for key in ('yesOutcomeId', 'yes_outcome_id', 'outcomeId', 'outcome_id'):
    84|        value = event.get(key)
    85|        if value:
    86|            return str(value).strip()
    87|    market = event.get('market')
    88|    if isinstance(market, dict):
    89|        for key in ('yesOutcomeId', 'yes_outcome_id', 'outcomeId', 'outcome_id'):
    90|            value = market.get(key)
    91|            if value:
    92|                return str(value).strip()
    93|        outcomes = market.get('outcomes') or market.get('options')
    94|        if isinstance(outcomes, list):
    95|            for outcome in outcomes:
    96|                if not isinstance(outcome, dict):
    97|                    continue
    98|                label = str(outcome.get('label') or outcome.get('name') or outcome.get('title') or '').strip().lower()
    99|                if label in {'yes', 'y', 'true'}:
   100|                    outcome_id = outcome.get('id') or outcome.get('outcomeId') or outcome.get('outcome_id')
   101|                    if outcome_id:
   102|                        return str(outcome_id).strip()
   103|    return ''
   104|
   105|
   106|def _extract_mid_from_update(update: Any) -> Optional[float]:
   107|    """Derive a mid-price from a MarketQuoteUpdate (WebSocket feed snapshot)."""
   108|    if update is None:
   109|        return None
   110|    bid = update.bid
   111|    ask = update.ask
   112|    if bid is not None and ask is not None and ask >= bid:
   113|        return (bid + ask) / 2.0
   114|    if update.midpoint is not None:
   115|        return float(update.midpoint)
   116|    return None
   117|
   118|
   119|def run_spread_capture_cycle(
   120|    client: BayseClient,
   121|    engine: SpreadCaptureEngine,
   122|    quote_manager: QuoteManager,
   123|    series_slug: str,
   124|    *,
   125|    dry_run: bool = True,
   126|    currency: str = 'USD',
   127|) -> None:
   128|    """One iteration of the spread-capture quote-refresh loop.
   129|
   130|    Steps:
   131|    1. Discover the active series market.
   132|    2. Subscribe the orderbook feed and derive the current mid-price
   133|       (WebSocket snapshot first, REST orderbook fallback).
   134|    3. Load the current portfolio to compute inventory.
   135|    4. Call ``engine.refresh_quotes`` — cancels stale orders and places fresh ones
   136|       only when the mid has moved beyond ``reprice_threshold``.
   137|    5. Burn matched YES/NO pairs to recycle USD capital.
   138|    """
   139|    event = engine.discover_series_market(series_slug)
   140|    if event is None:
   141|        logger.info('[SC] No active market found for series {!r}', series_slug)
   142|        return
   143|
   144|    market_id = _market_id_from_event(event)
   145|    event_id = str(event.get('id') or event.get('eventId') or '').strip()
   146|    outcome_id = _yes_outcome_id_from_event(event)
   147|    title = str(event.get('title') or event.get('name') or market_id).strip()
   148|
   149|    if not market_id or not event_id:
   150|        logger.warning('[SC] Cannot resolve market/event ID from series market {!r}', series_slug)
   151|        return
   152|
   153|    quote_manager.feed.subscribe_market(market_id, event_id=event_id)
   154|
   155|    update = quote_manager.latest_for_market(market_id)
   156|    mid_price = _extract_mid_from_update(update)
   157|    if mid_price is None:
   158|        mid_price = engine.get_mid_price(market_id)
   159|
   160|    inventory_units = 0.0
   161|    try:
   162|        portfolio = client.get_portfolio()
   163|        inventory_units = extract_inventory_units(
   164|            portfolio, event_id=event_id, market_id=market_id, outcome_id=outcome_id
   165|        )
   166|    except Exception as exc:
   167|        logger.warning('[SC] Portfolio unavailable for inventory calc: {}', exc)
   168|
   169|    logger.info(
   170|        '[SC] {} | series={!r} | mid={} | inventory={:.4f}',
   171|        title, series_slug, f'{mid_price:.4f}' if mid_price is not None else 'n/a', inventory_units,
   172|    )
   173|
   174|    results = engine.refresh_quotes(
   175|        event,
   176|        mid_price,
   177|        inventory_units=inventory_units,
   178|        event_id=event_id,
   179|        market_id=market_id,
   180|        outcome_id=outcome_id,
   181|        currency=currency,
   182|    )
   183|    if results:
   184|        logger.info('[SC] {} quote action(s) for {}', len(results), market_id)
   185|
   186|    if not dry_run:
   187|        engine.burn_pairs(market_id, quantity=1)
   188|
   189|
   190|def _execute_quote_plan(client: BayseClient, quote_plan: dict, dry_run: bool, currency: str) -> list[dict]:
   191|    placements: list[dict] = []
   192|    event_id = str(quote_plan.get('event_id') or '').strip()
   193|    market_id = str(quote_plan.get('market_id') or '').strip()
   194|    outcome_id = str(quote_plan.get('outcome_id') or '').strip()
   195|
   196|    for order in quote_plan.get('quote_orders', []):
   197|        if not isinstance(order, dict):
   198|            continue
   199|        side = str(order.get('side') or '').strip().upper()
   200|        price = float(order.get('price') or 0.0)
   201|        amount = float(order.get('amount') or 0.0)
   202|        if not side or price <= 0 or amount <= 0:
   203|            continue
   204|        if dry_run:
   205|            logger.info(
   206|                f"[DRY RUN] Quote {quote_plan.get('event_title', 'unknown event')} | {side} @ {price:.4f} x {amount:.2f}"
   207|            )
   208|            result = {
   209|                'dry_run': True,
   210|                'side': side,
   211|                'price': round(price, 4),
   212|                'amount': round(amount, 2),
   213|            }
   214|        else:
   215|            result = client.place_post_only_limit_order(
   216|                event_id=event_id,
   217|                market_id=market_id,
   218|                outcome_id=outcome_id,
   219|                side=side,
   220|                amount=amount,
   221|                price=price,
   222|                currency=currency,
   223|            )
   224|        placements.append(result)
   225|
   226|    return placements
   227|
   228|
   229|def _resolve_trade_args(signal: dict) -> tuple[str, str, str, str, float]:
   230|    side = str(signal.get("side", "")).upper()
   231|    market_id = str(
   232|        signal.get("market_id")
   233|        or signal.get("marketId")
   234|        or signal.get("event_id")
   235|        or signal.get("eventId")
   236|        or ""
   237|    ).strip()
   238|    outcome_label = str(signal.get("outcome_label") or signal.get("outcome") or "").strip().upper()
   239|    if not outcome_label:
   240|        raw_side = side.lower()
   241|        outcome_label = "YES" if raw_side in {"yes", "buy", "long"} else "NO"
   242|    event_id = str(signal.get("event_id") or signal.get("eventId") or market_id).strip()
   243|
   244|    if side == "YES" or side == "BUY":
   245|        price = signal.get("yes_price") or signal.get("market_prob") or signal.get("price")
   246|    elif side == "NO" or side == "SELL":
   247|        price = signal.get("no_price") or signal.get("market_prob") or signal.get("price")
   248|    else:
   249|        price = signal.get("price") or signal.get("market_prob")
   250|
   251|    if price is None:
   252|        price = 0.0
   253|
   254|    currency = _env("BAYSE_CURRENCY", default="USD")
   255|    return event_id, market_id, outcome_label, currency, float(price)
   256|
   257|
   258|def _format_trade_alert(trade: dict) -> str:
   259|    timestamp = trade.get("timestamp", "")
   260|    market_id = trade.get("marketId", "unknown")
   261|    outcome = trade.get("outcome", "unknown")
   262|    side = trade.get("side", "unknown")
   263|    price = trade.get("price", 0)
   264|    quantity = trade.get("quantity", 0)
   265|    return (
   266|        f"New Bayse trade detected\n"
   267|        f"Market: {market_id}\n"
   268|        f"Outcome: {outcome}\n"
   269|        f"Side: {side}\n"
   270|        f"Price: {float(price):.4f}\n"
   271|        f"Quantity: {float(quantity):.2f}\n"
   272|        f"Time: {timestamp}"
   273|    )
   274|
   275|
   276|def _format_event_alert(event: dict) -> str:
   277|    return (
   278|        f"New active market detected\n"
   279|        f"Title: {event.get('title') or event.get('name') or 'Untitled market'}\n"
   280|        f"Event ID: {event.get('id', 'unknown')}"
   281|    )
   282|
   283|
   284|def _notify(message: str, payload: Optional[dict] = None, level: str = "info") -> None:
   285|    """Log a notification. Telegram push is handled by bot/agent.py."""
   286|    log_fn = getattr(logger, level if level in ("info", "warning", "error", "debug") else "info")
   287|    log_fn(f"[NOTIFY] {message}")
   288|
   289|
   290|def run_cycle(
   291|    client: BayseClient,
   292|    strategies: list,
   293|    dry_run: bool = True,
   294|    bayse_user_id: str = "",
   295|    quote_manager: Optional[QuoteManager] = None,
   296|    quote_currency: str = "USD",
   297|) -> None:
   298|    logger.info("Starting trading cycle...")
   299|
   300|    events = client.get_open_events(page=1, size=50)
   301|    logger.info(f"Fetched {len(events)} open markets")
   302|    if quote_manager is not None:
   303|        quote_manager.sync_markets(events)
   304|        _attach_live_quotes(events, quote_manager)
   305|        logger.info(f"Realtime quote manager tracking {len(quote_manager.snapshot())} market(s)")
   306|
   307|    executed = []
   308|    all_signals = []
   309|    portfolio = None
   310|    if any(isinstance(strategy, MarketMakerStrategy) for strategy in strategies):
   311|        try:
   312|            portfolio = client.get_portfolio()
   313|            logger.debug("Fetched portfolio snapshot for market making")
   314|        except Exception as exc:
   315|            logger.warning(f"Portfolio snapshot unavailable for market making: {exc}")
   316|
   317|    for strategy in strategies:
   318|        if isinstance(strategy, MarketMakerStrategy):
   319|            quote_plans = strategy.generate_quotes(events, portfolio=portfolio)
   320|            if quote_plans:
   321|                logger.info(f"[{strategy.name}] Built {len(quote_plans)} quote plan(s)")
   322|            for quote_plan in quote_plans:
   323|                placements = _execute_quote_plan(client, quote_plan, dry_run=dry_run, currency=quote_currency)
   324|                executed.append({**quote_plan, "placements": placements})
   325|            continue
   326|
   327|        signals = strategy.scan(events)
   328|        if signals:
   329|            logger.info(f"[{strategy.name}] Found {len(signals)} signal(s)")
   330|            all_signals.extend(signals)
   331|
   332|    if not all_signals and not executed:
   333|        logger.debug("No actionable signals this cycle.")
   334|        return
   335|
   336|    for signal in all_signals:
   337|        logger.info(
   338|            f"Signal: {signal['event_title']} | Side: {signal['side']} | Edge: {signal['edge']:.2%} | Stake: $"
   339|            + format(float(signal['stake']), '.2f')
   340|            + " USDC"
   341|        )
   342|        if not dry_run:
   343|            event_id, market_id, outcome_label, currency, price = _resolve_trade_args(signal)
   344|            if not market_id or not outcome_label:
   345|                logger.warning(
   346|                    f"Skipping live trade for {signal.get('event_title', 'unknown event')} because market/outcome identifiers are missing."
   347|                )
   348|                executed.append({**signal, "trade_result": {"skipped": True, "reason": "missing market_id/outcome_label"}})
   349|                continue
   350|
   351|            result = client.place_order(
   352|                event_id=event_id,
   353|                market_id=market_id,
   354|                side=str(signal["side"]),
   355|                outcome=outcome_label,
   356|                price=price,
   357|                amount=float(signal["stake"]),
   358|                currency=currency,
   359|                order_type="LIMIT" if price else "MARKET",
   360|                time_in_force="GTC" if price else None,
   361|            )
   362|            signal["trade_result"] = result
   363|            executed.append(signal)
   364|        else:
   365|            logger.info("[DRY RUN] Trade not placed.")
   366|            executed.append({**signal, "dry_run": True})
   367|
   368|    _notify(
   369|        f"medes-et-bayse: Cycle complete. {len(executed)} action(s) {'simulated' if dry_run else 'executed'}.",
   370|        payload={"user_id": bayse_user_id, "actions": executed},
   371|        level="success",
   372|    )
   373|    logger.info("Cycle complete.")
   374|
   375|
   376|def monitor_bayse_activity(
   377|    client: BayseClient,
   378|    poll_interval: int,
   379|    stop_event: threading.Event,
   380|) -> None:
   381|    seen_trade_ids: set[str] = set()
   382|    seen_event_ids: set[str] = set()
   383|
   384|    try:
   385|        for trade in client.get_trades(limit=100):
   386|            trade_id = str(trade.get("id", "")).strip()
   387|            if trade_id:
   388|                seen_trade_ids.add(trade_id)
   389|        for event in client.get_open_events(page=1, size=100):
   390|            event_id = str(event.get("id", "")).strip()
   391|            if event_id:
   392|                seen_event_ids.add(event_id)
   393|    except Exception as exc:
   394|        logger.warning(f"Initial Bayse activity snapshot failed: {exc}")
   395|
   396|    logger.info(f"Started Bayse activity polling every {poll_interval}s")
   397|
   398|    while not stop_event.is_set():
   399|        try:
   400|            trades = client.get_trades(limit=50)
   401|            new_trades = []
   402|            for trade in sorted(trades, key=lambda item: _parse_timestamp(str(item.get("timestamp", "")))):
   403|                trade_id = str(trade.get("id", "")).strip()
   404|                if trade_id and trade_id not in seen_trade_ids:
   405|                    seen_trade_ids.add(trade_id)
   406|                    new_trades.append(trade)
   407|
   408|            for trade in new_trades:
   409|                _notify(_format_trade_alert(trade), payload={"trade": trade}, level="info")
   410|
   411|            events = client.get_open_events(page=1, size=100)
   412|            new_events = []
   413|            for event in events:
   414|                event_id = str(event.get("id", "")).strip()
   415|                if event_id and event_id not in seen_event_ids:
   416|                    seen_event_ids.add(event_id)
   417|                    new_events.append(event)
   418|
   419|            for event in new_events:
   420|                _notify(_format_event_alert(event), payload={"event": event}, level="info")
   421|        except Exception as exc:
   422|            logger.error(f"Bayse activity polling error: {exc}")
   423|        stop_event.wait(poll_interval)
   424|
   425|
   426|def main():
   427|    parser = argparse.ArgumentParser(description="medes-et-bayse trading bot")
   428|    parser.add_argument("--scan-only", action="store_true", help="Scan markets and log signals without placing trades")
   429|    parser.add_argument("--strategy", choices=["kelly", "arbitrage", "market-making", "all"], default="all", help="Strategy to run")
   430|    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
   431|    parser.add_argument("--series", default="", help="Series slug for spread-capture market-maker (e.g. nfl-sunday-showcase)")
   432|    args = parser.parse_args()
   433|
   434|    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
   435|    bankroll = float(os.getenv("BANKROLL", "100.0"))
   436|    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
   437|    trade_poll_interval = int(os.getenv("TRADE_POLL_INTERVAL_SECONDS", "30"))
   438|
   439|    if args.scan_only:
   440|        dry_run = True
   441|
   442|    bayse_public_key = _env("BAYSE_PUBLIC_KEY")
   443|    bayse_secret_key = _env("BAYSE_SECRET_KEY")
   444|    bayse_base_url = _env("BAYSE_API_URL", default="https://relay.bayse.markets")
   445|    bayse_user_id = _env("BAYSE_USER_ID")
   446|
   447|    if not bayse_public_key:
   448|        logger.warning("BAYSE_PUBLIC_KEY is not set.")
   449|    if not bayse_secret_key:
   450|        logger.warning("BAYSE_SECRET_KEY is not set.")
   451|    if not bayse_user_id:
   452|        logger.warning("BAYSE_USER_ID is not set.")
   453|
   454|    client = BayseClient(public_key=bayse_public_key, secret_key=bayse_secret_key, base_url=bayse_base_url)
   455|    quote_manager = QuoteManager(client, websocket_url=_env("BAYSE_WS_URL"), poll_interval=float(os.getenv("QUOTE_POLL_INTERVAL_SECONDS", "10")))
   456|
   457|    telegram_handler = None
   458|    if build_telegram_handler_from_env:
   459|        telegram_handler = build_telegram_handler_from_env()
   460|        if telegram_handler:
   461|            telegram_handler.attach_bayse_client(client)
   462|
   463|    min_edge = float(os.getenv("MIN_EDGE", "0.03"))
   464|    max_position_fraction = float(os.getenv("MAX_POSITION_FRACTION", "0.05"))
   465|    quote_currency = _env("BAYSE_CURRENCY", default="USD")
   466|
   467|    strategy_map = {
   468|        "kelly": [KellyStrategy(bankroll=bankroll, min_edge=min_edge, max_fraction=max_position_fraction)],
   469|        "arbitrage": [ArbitrageStrategy(bankroll=bankroll, min_edge=min_edge)],
   470|        "market-making": [MarketMakerStrategy(bankroll=bankroll, min_edge=min_edge)],
   471|        "all": [
   472|            KellyStrategy(bankroll=bankroll, min_edge=min_edge, max_fraction=max_position_fraction),
   473|            ArbitrageStrategy(bankroll=bankroll, min_edge=min_edge),
   474|            MarketMakerStrategy(bankroll=bankroll, min_edge=min_edge),
   475|        ],
   476|    }
   477|
   478|    strategies = strategy_map[args.strategy]
   479|
   480|    series_slug = args.series or _env("SERIES_SLUG")
   481|    spread_engine: Optional[SpreadCaptureEngine] = None
   482|    if series_slug:
   483|        spread_engine = SpreadCaptureEngine(
   484|            client,
   485|            bankroll=bankroll,
   486|            half_spread=float(os.getenv("SC_HALF_SPREAD", "0.02")),
   487|            order_size=float(os.getenv("SC_ORDER_SIZE", "10.0")),
   488|            reprice_threshold=float(os.getenv("SC_REPRICE_THRESHOLD", "0.005")),
   489|            pre_close_seconds=float(os.getenv("SC_PRE_CLOSE_SECONDS", "300")),
   490|            inventory_skew=float(os.getenv("SC_INVENTORY_SKEW", "0.60")),
   491|            max_position_fraction=max_position_fraction,
   492|            dry_run=dry_run,
   493|        )
   494|        logger.info(f"Spread-capture engine enabled for series {series_slug!r}")
   495|
   496|    if not args.scan_only:
   497|        quote_manager.start()
   498|        logger.info("Realtime quote management enabled")
   499|
   500|    if not args.once and not args.scan_only:
   501|