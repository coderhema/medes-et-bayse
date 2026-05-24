"""medes-et-bayse: PyFlue agent entry point with Ralph loop.

The Ralph loop pattern: Goal → Plan → Execute → Reflect → Repeat.

The agent autonomously:
  1. Scans Bayse Markets for opportunities
  2. Researches market context with Tavily
  3. Evaluates signals with Kelly Criterion + Arbitrage strategies
  4. Executes trades (or simulates in dry-run mode)
  5. Logs everything to SQLite memory
  6. Reports cycle summaries to Telegram
  7. Reflects on results and adjusts

Triggered via the /goal Telegram command, runs autonomously until stopped.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

try:
    from telegram import Bot, Update
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError:
    raise RuntimeError("python-telegram-bot is required: pip install python-telegram-bot")

from bot.tools.bayse import scan_markets, get_portfolio, place_trade, get_client
from bot.tools.search import tavily_search
from bot.tools.memory import initialize_db, log_trade, log_research, log_note, query_memory
from bot.strategies.kelly import KellyStrategy
from bot.strategies.arbitrage import ArbitrageStrategy

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
BANKROLL = float(os.getenv("BANKROLL", "100.0"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.03"))
MAX_POSITION_FRACTION = float(os.getenv("MAX_POSITION_FRACTION", "0.05"))
CYCLE_INTERVAL = int(os.getenv("CYCLE_INTERVAL_SECONDS", "300"))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6433282551")
DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/medes.db")

# Ralph loop state
_goal_active = False
_goal_stop_event: Optional[asyncio.Event] = None


# ---------------------------------------------------------------------------
# Ralph Loop — the autonomous cycle
# ---------------------------------------------------------------------------

async def run_ralph_loop(bot: Bot, chat_id: str) -> None:
    """The Ralph loop: Goal → Plan → Execute → Reflect → Repeat.

    Runs autonomously, pushing cycle summaries to Telegram.
    """
    global _goal_active
    _goal_active = True
    stop = asyncio.Event()

    # Store globally so /stop can cancel it
    global _goal_stop_event
    _goal_stop_event = stop

    cycle_id = 0
    kelly = KellyStrategy(
        bankroll=BANKROLL,
        min_edge=MIN_EDGE,
        max_fraction=MAX_POSITION_FRACTION,
    )
    arbitrage = ArbitrageStrategy(bankroll=BANKROLL, min_edge=MIN_EDGE)

    await bot.send_message(
        chat_id=chat_id,
        text="🎯 <b>Ralph loop started</b>\n"
             f"Bankroll: ${BANKROLL:.2f} | Min edge: {MIN_EDGE:.0%}\n"
             f"Dry run: {'ON' if DRY_RUN else '🔴 LIVE'}\n"
             f"Cycle interval: {CYCLE_INTERVAL}s\n\n"
             "Use /stop to halt.",
        parse_mode="HTML",
    )

    while not stop.is_set():
        cycle_id += 1
        run_id = uuid.uuid4().hex[:8]
        cycle_start = time.monotonic()

        try:
            summary = await _run_cycle(
                run_id=run_id,
                kelly=kelly,
                arbitrage=arbitrage,
            )

            elapsed = time.monotonic() - cycle_start
            await bot.send_message(
                chat_id=chat_id,
                text=f"📊 <b>Cycle #{cycle_id} [{run_id}]</b> ({elapsed:.1f}s)\n\n{summary}",
                parse_mode="HTML",
            )

            await log_note(
                note=summary,
                cycle_id=run_id,
                category="cycle_summary",
                db_path=DATABASE_PATH,
            )

        except Exception as exc:
            logger.error(f"Cycle {cycle_id} failed: {exc}")
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ Cycle #{cycle_id} error: {exc}",
                parse_mode="HTML",
            )
            await log_note(
                note=f"Cycle failed: {exc}",
                cycle_id=run_id,
                category="error",
                db_path=DATABASE_PATH,
            )

        # Wait for next cycle or stop signal
        try:
            await asyncio.wait_for(stop.wait(), timeout=CYCLE_INTERVAL)
        except asyncio.TimeoutError:
            pass  # Normal — timeout means next cycle

    _goal_active = False
    await bot.send_message(
        chat_id=chat_id,
        text="⏹ <b>Ralph loop stopped.</b>",
        parse_mode="HTML",
    )


async def _run_cycle(
    run_id: str,
    kelly: KellyStrategy,
    arbitrage: ArbitrageStrategy,
) -> str:
    """Execute one Ralph loop cycle: Scan → Research → Trade → Log."""

    # 1. SCAN — Get open markets
    events = await scan_markets(size=30)
    if not events or (len(events) == 1 and "error" in events[0]):
        return "No markets available."

    # 2. SIGNAL — Run strategies
    kelly_signals = kelly.scan(events)
    arb_signals = arbitrage.scan(events)
    all_signals = sorted(kelly_signals + arb_signals, key=lambda s: s.get("edge", 0), reverse=True)

    if not all_signals:
        await log_note(
            note="No actionable signals this cycle.",
            cycle_id=run_id,
            category="cycle_summary",
            db_path=DATABASE_PATH,
        )
        return "No actionable signals."

    top_signals = all_signals[:3]  # Focus on top 3

    # 3. RESEARCH — Use Tavily for top signals
    research_notes = []
    for signal in top_signals:
        title = signal.get("event_title", "")
        if title:
            try:
                research = await tavily_search(
                    f"{title} prediction market latest news",
                    max_results=3,
                )
                if research.get("answer"):
                    research_notes.append(f"<b>{title}</b>: {research['answer'][:200]}")
                    await log_research(
                        query=f"{title} prediction market",
                        summary=research["answer"][:500],
                        source="tavily",
                        market_id=signal.get("market_id", ""),
                        event_id=signal.get("event_id", ""),
                        db_path=DATABASE_PATH,
                    )
            except Exception as exc:
                logger.warning(f"Research failed for {title}: {exc}")

    # 4. EXECUTE — Place trades (or simulate)
    trade_results = []
    for signal in top_signals:
        edge = signal.get("edge", 0)
        stake = signal.get("stake", 0)
        side = signal.get("side", "yes")
        title = signal.get("event_title", "Unknown")
        event_id = signal.get("event_id", "")
        market_id = signal.get("market_id", signal.get("event_id", ""))

        result = await place_trade(
            event_id=event_id,
            market_id=market_id,
            side="buy",
            amount=stake,
            outcome=side.upper(),
            dry_run=DRY_RUN,
        )

        await log_trade(
            market_id=market_id,
            side=side,
            amount=stake,
            event_id=event_id,
            event_title=title,
            outcome=side.upper(),
            price=signal.get("market_prob", 0),
            edge=edge,
            kelly_fraction=signal.get("kelly_fraction", 0),
            strategy=signal.get("strategy", ""),
            dry_run=DRY_RUN,
            status="simulated" if DRY_RUN else "submitted",
            result_json=json.dumps(result, default=str),
            db_path=DATABASE_PATH,
        )

        trade_results.append(
            f"{'🧪' if DRY_RUN else '💰'} <b>{title}</b>\n"
            f"  {side.upper()} | edge {edge:.1%} | ${stake:.2f}"
        )

    # 5. BUILD SUMMARY
    summary_parts = [
        f"Markets scanned: {len(events)}",
        f"Signals found: {len(all_signals)}",
        f"Trades {'simulated' if DRY_RUN else 'executed'}: {len(trade_results)}",
        "",
    ]
    summary_parts.extend(trade_results)

    if research_notes:
        summary_parts.append("")
        summary_parts.append("<b>Research:</b>")
        summary_parts.extend(research_notes[:2])

    return "\n".join(summary_parts)


# ---------------------------------------------------------------------------
# Telegram Commands
# ---------------------------------------------------------------------------

async def goal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /goal — start the autonomous Ralph loop."""
    global _goal_active

    message = update.effective_message
    if message is None:
        return

    if _goal_active:
        await message.reply_text("⚡ Ralph loop already running. Use /stop to halt first.")
        return

    chat_id = str(message.chat_id)
    bot = context.bot

    # Start the Ralph loop as a background task
    asyncio.create_task(run_ralph_loop(bot, chat_id))
    logger.info(f"Ralph loop started by /goal command in chat {chat_id}")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop — halt the Ralph loop."""
    global _goal_active, _goal_stop_event

    message = update.effective_message
    if message is None:
        return

    if not _goal_active or _goal_stop_event is None:
        await message.reply_text("No active Ralph loop to stop.")
        return

    _goal_stop_event.set()
    await message.reply_text("⏹ Stopping Ralph loop...")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — show current agent state."""
    message = update.effective_message
    if message is None:
        return

    recent_trades = await query_memory("trades", limit=5, db_path=DATABASE_PATH)
    recent_notes = await query_memory("agent_notes", limit=3, db_path=DATABASE_PATH)

    status_lines = [
        f"<b>medes-et-bayse status</b>",
        f"Ralph loop: {'🟢 ACTIVE' if _goal_active else '⚪ IDLE'}",
        f"Dry run: {'ON' if DRY_RUN else '🔴 LIVE'}",
        f"Bankroll: ${BANKROLL:.2f}",
        f"DB: {DATABASE_PATH}",
        "",
        f"<b>Recent trades ({len(recent_trades)}):</b>",
    ]

    for t in recent_trades:
        status_lines.append(
            f"  {t.get('side', '?').upper()} {t.get('event_title', t.get('market_id', '?'))[:40]}"
            f" ${t.get('amount', 0):.2f} [{t.get('status', '?')}]"
        )

    if recent_notes:
        status_lines.append("")
        status_lines.append("<b>Latest notes:</b>")
        for n in recent_notes:
            status_lines.append(f"  [{n.get('category', '')}] {n.get('note', '')[:100]}")

    await message.reply_text("\n".join(status_lines), parse_mode="HTML")


async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /trades — show recent trade log."""
    message = update.effective_message
    if message is None:
        return

    trades = await query_memory("trades", limit=10, db_path=DATABASE_PATH)
    if not trades:
        await message.reply_text("No trades logged yet.")
        return

    lines = ["<b>Trade log:</b>"]
    for t in trades:
        emoji = "🧪" if t.get("dry_run") else "💰"
        lines.append(
            f"{emoji} {t.get('side', '?').upper()} "
            f"{(t.get('event_title') or t.get('market_id', '?'))[:35]}\n"
            f"   ${t.get('amount', 0):.2f} | edge {t.get('edge', 0):.1%} | "
            f"{t.get('strategy', '?')} | {t.get('created_at', '')[:16]}"
        )
    await message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Start the Telegram bot with PyFlue agent commands."""
    import logging
    logging.basicConfig(level=logging.INFO)

    # Initialize SQLite memory
    initialize_db(DATABASE_PATH)
    logger.info(f"Database initialized at {DATABASE_PATH}")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    # Build application
    app = Application.builder().token(token).build()

    # Register commands
    app.add_handler(CommandHandler("goal", goal_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("trades", trades_command))

    # Set bot commands menu
    async def post_init(application: Application) -> None:
        from telegram import BotCommand
        await application.bot.set_my_commands([
            BotCommand("goal", "Start autonomous trading loop"),
            BotCommand("stop", "Stop the trading loop"),
            BotCommand("status", "Show agent status"),
            BotCommand("trades", "Show recent trades"),
        ])
        logger.info("Bot commands registered")

    app.post_init = post_init

    logger.info("medes-et-bayse agent starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
