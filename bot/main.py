"""medes-et-bayse: Main entry point for the Bayse Markets trading bot.

Uses Poke API as the backend orchestration layer.
Strategies: Kelly Criterion, Arbitrage Detection, Market Making.
"""

from __future__ import annotations

import argparse
import os
import time

from dotenv import load_dotenv
from loguru import logger

from bot.bayse_client import BayseClient
from bot.poke_client import PokeClient
from bot.strategies.arbitrage import ArbitrageStrategy
from bot.strategies.kelly import KellyStrategy
from bot.strategies.market_maker import MarketMakerStrategy

try:
    from bot.telegram_handler import build_telegram_handler_from_env
except Exception as exc:  # pragma: no cover - optional dependency fallback
    build_telegram_handler_from_env = None
    logger.warning(f"Telegram handler unavailable: {exc}")

load_dotenv()


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _resolve_trade_args(signal: dict) -> tuple[str, str, float]:
    side = str(signal.get("side", "")).lower()
    market_id = str(
        signal.get("market_id")
        or signal.get("marketId")
        or signal.get("event_id")
        or signal.get("eventId")
        or ""
    ).strip()
    outcome_id = str(signal.get("outcome_id") or signal.get("outcomeId") or side).strip()

    if side == "yes":
        price = signal.get("yes_price") or signal.get("market_prob") or signal.get("price")
    elif side == "no":
        price = signal.get("no_price") or signal.get("market_prob") or signal.get("price")
    else:
        price = signal.get("price") or signal.get("market_prob")

    if price is None:
        price = 0.0

    return market_id, outcome_id, float(price)


def run_cycle(
    client: BayseClient,
    poke: PokeClient,
    strategies: list,
    dry_run: bool = True,
    bayse_user_id: str = "",
) -> None:
    """Execute one full scan-and-trade cycle."""
    logger.info("Starting trading cycle...")

    events = client.get_open_events(page=1, size=50)
    logger.info(f"Fetched {len(events)} open markets")

    all_signals = []

    for strategy in strategies:
        signals = strategy.scan(events)
        if signals:
            logger.info(f"[{strategy.name}] Found {len(signals)} signal(s)")
            all_signals.extend(signals)

    if not all_signals:
        logger.info("No actionable signals this cycle.")
        poke.notify(
            "medes-et-bayse: No signals this cycle.",
            payload={"user_id": bayse_user_id, "signals": []},
            level="info",
        )
        return

    executed = []
    for signal in all_signals:
        logger.info(
            f"Signal: {signal['event_title']} | "
            f"Side: {signal['side']} | "
            f"Edge: {signal['edge']:.2%} | "
            f"Stake: ${signal['stake']:.2f} USDC"
        )
        if not dry_run:
            market_id, outcome_id, price = _resolve_trade_args(signal)
            if not market_id or not outcome_id:
                logger.warning(
                    f"Skipping live trade for {signal.get('event_title', 'unknown event')} because market/outcome identifiers are missing."
                )
                executed.append({**signal, "trade_result": {"skipped": True, "reason": "missing market_id/outcome_id"}})
                continue

            result = client.place_order(
                event_id=str(signal["event_id"]),
                market_id=market_id,
                outcome_id=outcome_id,
                side=str(signal["side"]).upper(),
                price=price,
                amount=float(signal["stake"]),
                currency=_env("BAYSE_CURRENCY", default="USDC"),
            )
            signal["trade_result"] = result
            executed.append(signal)
        else:
            logger.info("[DRY RUN] Trade not placed.")
            executed.append({**signal, "dry_run": True})

    poke.notify(
        f"medes-et-bayse: Cycle complete. {len(executed)} trade(s) {'simulated' if dry_run else 'executed'}.",
        payload={"user_id": bayse_user_id, "trades": executed},
        level="success",
    )
    logger.info("Cycle complete.")


def main():
    parser = argparse.ArgumentParser(description="medes-et-bayse trading bot")
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Scan markets and log signals without placing trades",
    )
    parser.add_argument(
        "--strategy",
        choices=["kelly", "arbitrage", "market-making", "all"],
        default="all",
        help="Strategy to run",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (useful for Poke Recipe cron)",
    )
    args = parser.parse_args()

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    bankroll = float(os.getenv("BANKROLL", "100.0"))
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

    if args.scan_only:
        dry_run = True

    bayse_public_key = _env("BAYSE_API_KEY", "BAYSE_PUBLIC_KEY")
    bayse_secret_key = _env("BAYSE_API_SECRET", "BAYSE_SECRET_KEY")
    bayse_base_url = _env("BAYSE_BASE_URL", default="https://relay.bayse.markets")
    bayse_user_id = _env("BAYSE_USER_ID")

    if not bayse_public_key:
        logger.warning("BAYSE_API_KEY/BAYSE_PUBLIC_KEY is not set.")
    if not bayse_secret_key:
        logger.warning("BAYSE_API_SECRET/BAYSE_SECRET_KEY is not set.")
    if not bayse_user_id:
        logger.warning("BAYSE_USER_ID is not set.")

    telegram_handler = build_telegram_handler_from_env() if build_telegram_handler_from_env else None

    client = BayseClient(
        public_key=bayse_public_key,
        secret_key=bayse_secret_key,
        base_url=bayse_base_url,
    )
    poke = PokeClient(
        api_key=_env("POKE_API_KEY"),
        webhook_url=_env("POKE_WEBHOOK_URL"),
        telegram=telegram_handler,
    )

    min_edge = float(os.getenv("MIN_EDGE", "0.03"))
    max_position_fraction = float(os.getenv("MAX_POSITION_FRACTION", "0.05"))

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

    if args.once or args.scan_only:
        run_cycle(client, poke, strategies, dry_run=dry_run, bayse_user_id=bayse_user_id)
    else:
        logger.info(f"Starting polling loop every {poll_interval}s...")
        while True:
            try:
                run_cycle(client, poke, strategies, dry_run=dry_run, bayse_user_id=bayse_user_id)
            except Exception as e:
                logger.error(f"Cycle error: {e}")
                poke.notify(f"medes-et-bayse ERROR: {e}", level="error")
            time.sleep(poll_interval)


if __name__ == "__main__":
    main()
