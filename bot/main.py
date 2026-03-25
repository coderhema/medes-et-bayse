"""medes-et-bayse: Main entry point for the Bayse Markets trading bot.

Uses Poke API as the backend orchestration layer.
Strategies: Kelly Criterion, Arbitrage Detection, Market Making.
"""

import argparse
import time
from loguru import logger
from dotenv import load_dotenv
import os

from bot.bayse_client import BayseClient
from bot.poke_client import PokeClient
from bot.strategies.kelly import KellyStrategy
from bot.strategies.arbitrage import ArbitrageStrategy
from bot.strategies.market_maker import MarketMakerStrategy

load_dotenv()


def run_cycle(
    client: BayseClient,
    poke: PokeClient,
    strategies: list,
    dry_run: bool = True,
) -> None:
    """Execute one full scan-and-trade cycle."""
    logger.info("Starting trading cycle...")

    # 1. Fetch open prediction market events
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
        poke.notify("medes-et-bayse: No signals this cycle.", level="info")
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
            result = client.place_trade(
                event_id=signal["event_id"],
                side=signal["side"],
                amount=signal["stake"],
            )
            signal["trade_result"] = result
            executed.append(signal)
        else:
            logger.info("[DRY RUN] Trade not placed.")
            executed.append({**signal, "dry_run": True})

    # Notify Poke with results
    poke.notify(
        f"medes-et-bayse: Cycle complete. {len(executed)} trade(s) {'simulated' if dry_run else 'executed'}.",
        payload=executed,
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

    client = BayseClient(
        api_key=os.getenv("BAYSE_API_KEY", ""),
        base_url=os.getenv("BAYSE_BASE_URL", "https://relay.bayse.markets"),
    )
    poke = PokeClient(
        api_key=os.getenv("POKE_API_KEY", ""),
        webhook_url=os.getenv("POKE_WEBHOOK_URL", ""),
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
        run_cycle(client, poke, strategies, dry_run=dry_run)
    else:
        logger.info(f"Starting polling loop every {poll_interval}s...")
        while True:
            try:
                run_cycle(client, poke, strategies, dry_run=dry_run)
            except Exception as e:
                logger.error(f"Cycle error: {e}")
                poke.notify(f"medes-et-bayse ERROR: {e}", level="error")
            time.sleep(poll_interval)


if __name__ == "__main__":
    main()
