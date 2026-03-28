"""Telegram bot handler for medes-et-bayse.

Provides outbound notifications and inbound commands:
  /start, /help, /status, /balance, /portfolio, /events, /quote, /order
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Optional

from loguru import logger

try:
    from telegram import Update, Bot
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError as exc:
    raise ImportError("python-telegram-bot is required. Install it with: pip install python-telegram-bot") from exc

DEFAULT_CHAT_ID = "6433282551"


class TelegramHandler:
    def __init__(
        self,
        token: str,
        chat_id: str = DEFAULT_CHAT_ID,
        bayse_client=None,
        bot_status_callback=None,
    ):
        self.token = token
        self.chat_id = chat_id
        self.bayse_client = bayse_client
        self._bot_status_callback = bot_status_callback
        self._app: Optional[Application] = None

    def attach_bayse_client(self, bayse_client) -> None:
        self.bayse_client = bayse_client

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        try:
            bot = Bot(token=self.token)
            async with bot:
                await bot.send_message(chat_id=self.chat_id, text=text, parse_mode=parse_mode)
            logger.info(f"Telegram message sent: {text[:80]}..." if len(text) > 80 else f"Telegram message sent: {text}")
            return True
        except Exception as e:
            logger.error(f"Telegram send_message failed: {e}")
            return False

    def send_message_sync(self, text: str, parse_mode: str = "HTML") -> bool:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.send_message(text, parse_mode))
                return True
            return loop.run_until_complete(self.send_message(text, parse_mode))
        except RuntimeError:
            return asyncio.run(self.send_message(text, parse_mode))

    async def send_signal(self, event_title: str, side: str, edge: float, stake: float, dry_run: bool = False) -> bool:
        label = "[DRY RUN] " if dry_run else ""
        text = (
            f"<b>{label}Trading Signal</b>\n"
            f"Market: {event_title}\n"
            f"Side: <b>{side}</b>\n"
            f"Edge: {edge:.2%}\n"
            f"Stake: ${stake:.2f}"
        )
        return await self.send_message(text)

    async def send_alert(self, message: str, level: str = "info") -> bool:
        emoji = {"info": "ℹ️", "success": "✅", "error": "❌"}.get(level, "🔔")
        text = f"{emoji} <b>medes-et-bayse</b>\n{message}"
        return await self.send_message(text)

    def _require_client(self):
        if self.bayse_client is None:
            raise RuntimeError("Bayse client not configured")
        return self.bayse_client

    @staticmethod
    def _parse_tokens(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
        named: dict[str, str] = {}
        positional: list[str] = []
        for token in tokens:
            if "=" in token:
                key, value = token.split("=", 1)
                named[key.strip().lower().replace("-", "_")] = value.strip()
            else:
                positional.append(token)
        return named, positional

    @staticmethod
    def _first_value(named: dict[str, str], positional: list[str], keys: list[str], index: int, default: str = "") -> str:
        for key in keys:
            value = named.get(key, "").strip()
            if value:
                return value
        if index < len(positional):
            return positional[index]
        return default

    @staticmethod
    def _fmt_money(value) -> str:
        try:
            return f"{float(value):,.2f}"
        except Exception:
            return str(value)

    @staticmethod
    def _fmt_float(value) -> str:
        try:
            return f"{float(value):,.4f}"
        except Exception:
            return str(value)

    def _format_events(self, events: list[dict], limit: int = 10) -> str:
        if not events:
            return "No active markets found."
        lines = []
        for event in events[:limit]:
            title = event.get("title") or event.get("name") or "Untitled market"
            event_id = event.get("id", "unknown")
            status = event.get("status", "open")
            lines.append(f"• {title}\n  id: {event_id}\n  status: {status}")
        return "\n".join(lines)

    def _format_balance(self, assets: list[dict]) -> str:
        if not assets:
            return "No wallet assets found."
        lines = []
        for asset in assets:
            symbol = asset.get("symbol", "?")
            available = self._fmt_money(asset.get("availableBalance", 0))
            pending = self._fmt_money(asset.get("pendingBalance", 0))
            network = asset.get("network", "n/a")
            lines.append(f"• {symbol}: available {available}, pending {pending} ({network})")
        return "\n".join(lines)

    def _format_portfolio(self, portfolio) -> str:
        if isinstance(portfolio, dict):
            positions = portfolio.get("outcomeBalances") or portfolio.get("data") or portfolio.get("positions") or []
            total_cost = portfolio.get("portfolioCost")
            total_value = portfolio.get("portfolioCurrentValue")
            pct_change = portfolio.get("portfolioPercentageChange")
        else:
            positions = portfolio or []
            total_cost = total_value = pct_change = None

        if not positions:
            return "No open positions found."

        lines = []
        for pos in positions:
            market = pos.get("market", {})
            event = market.get("event", {})
            title = event.get("title") or market.get("title") or "Unknown market"
            outcome = pos.get("outcome", pos.get("outcomeId", "?"))
            balance = self._fmt_money(pos.get("balance", 0))
            current_value = self._fmt_money(pos.get("currentValue", 0))
            avg_price = self._fmt_float(pos.get("averagePrice", 0))
            lines.append(f"��� {title} [{outcome}]\n  balance: {balance}\n  avg price: {avg_price}\n  current value: {current_value}")

        summary = []
        if total_cost is not None:
            summary.append(f"Cost: {self._fmt_money(total_cost)}")
        if total_value is not None:
            summary.append(f"Value: {self._fmt_money(total_value)}")
        if pct_change is not None:
            summary.append(f"PnL: {self._fmt_money(pct_change)}%")
        if summary:
            lines.append("\n" + " | ".join(summary))
        return "\n".join(lines)

    @staticmethod
    def _format_quote(quote: dict, event_id: str, market_id: str, side: str, outcome_id: str, amount: float, currency: str) -> str:
        return (
            f"<b>Quote</b>\n"
            f"Event: {event_id}\n"
            f"Market: {market_id}\n"
            f"Side: <b>{side.upper()}</b>\n"
            f"Outcome: {outcome_id}\n"
            f"Amount: {amount:.2f} {currency}\n"
            f"Price: {float(quote.get('price', 0)):.4f}\n"
            f"Current market price: {float(quote.get('currentMarketPrice', 0)):.4f}\n"
            f"Quantity: {float(quote.get('quantity', 0)):.2f}\n"
            f"Cost of shares: {float(quote.get('costOfShares', 0)):.2f}\n"
            f"Fee: {float(quote.get('fee', 0)):.2f}\n"
            f"Complete fill: {bool(quote.get('completeFill', False))}"
        )

    @staticmethod
    def _format_order(result: dict) -> str:
        order = result.get("order", result)
        engine = result.get("engine", "unknown")
        return (
            f"<b>Order placed</b>\n"
            f"Engine: {engine}\n"
            f"Order ID: {order.get('id', 'unknown')}\n"
            f"Status: {order.get('status', 'unknown')}\n"
            f"Side: {order.get('side', 'unknown')}\n"
            f"Type: {order.get('type', 'unknown')}\n"
            f"Outcome: {order.get('outcome', 'unknown')}\n"
            f"Price: {float(order.get('price', 0)):.4f}\n"
            f"Quantity: {float(order.get('quantity', 0)):.2f}\n"
            f"Amount: {float(order.get('amount', 0)):.2f} {order.get('currency', 'USD')}"
        )

    def _usage_quote(self) -> str:
        return (
            "Usage:\n"
            "/quote event_id=<uuid> market_id=<uuid> side=BUY outcome_id=<uuid> amount=100 currency=USD\n"
            "or positional: /quote <event_id> <market_id> <side> <outcome_id> <amount> [currency]"
        )

    def _usage_order(self) -> str:
        return (
            "Usage:\n"
            "/order event_id=<uuid> market_id=<uuid> side=BUY outcome_id=<uuid> amount=100 currency=USD type=MARKET price=0.72\n"
            "Optional: time_in_force=GTC post_only=false max_slippage=0.02 expires_at=2026-03-28T12:00:00Z\n"
            "or positional: /order <event_id> <market_id> <side> <outcome_id> <amount> [currency] [type] [price]"
        )

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "👋 <b>medes-et-bayse bot online.</b>\n"
            "Type /help for the available Bayse commands.",
            parse_mode="HTML",
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "<b>Commands</b>\n"
            "/status — bot status\n"
            "/balance — wallet balances\n"
            "/portfolio — open positions\n"
            "/events — active markets\n"
            "/quote — price quote before an order\n"
            "/order — place a Bayse order\n\n"
            "Examples:\n"
            "/quote event_id=<uuid> market_id=<uuid> side=BUY outcome_id=<uuid> amount=100 currency=USD\n"
            "/order event_id=<uuid> market_id=<uuid> side=BUY outcome_id=<uuid> amount=100 type=MARKET currency=USD",
            parse_mode="HTML",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._bot_status_callback:
            try:
                status_text = self._bot_status_callback()
            except Exception as e:
                status_text = f"Error fetching status: {e}"
        else:
            status_text = "Bot is running. Telegram commands and Bayse polling are enabled."
        await update.message.reply_text(f"<b>Status</b>\n{status_text}", parse_mode="HTML")

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        client = self._require_client()
        try:
            assets = await asyncio.to_thread(client.get_balance)
            text = self._format_balance(assets if isinstance(assets, list) else [assets])
        except Exception as e:
            text = f"Error fetching balance: {e}"
        await update.message.reply_text(f"<b>Balance</b>\n{text}", parse_mode="HTML")

    async def _cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        client = self._require_client()
        try:
            portfolio = await asyncio.to_thread(client.get_portfolio)
            text = self._format_portfolio(portfolio)
        except Exception as e:
            text = f"Error fetching portfolio: {e}"
        await update.message.reply_text(f"<b>Portfolio</b>\n{text}", parse_mode="HTML")

    async def _cmd_events(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        client = self._require_client()
        limit = 10
        if context.args:
            try:
                limit = max(1, min(25, int(context.args[0])))
            except Exception:
                limit = 10
        try:
            events = await asyncio.to_thread(client.get_open_events, 1, limit)
            text = self._format_events(events, limit=limit)
        except Exception as e:
            text = f"Error fetching active markets: {e}"
        await update.message.reply_text(f"<b>Active markets</b>\n{text}", parse_mode="HTML")

    async def _cmd_quote(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        client = self._require_client()
        named, positional = self._parse_tokens(context.args)
        event_id = self._first_value(named, positional, ["event_id", "eventid"], 0)
        market_id = self._first_value(named, positional, ["market_id", "marketid"], 1)
        side = self._first_value(named, positional, ["side"], 2, "BUY")
        outcome_id = self._first_value(named, positional, ["outcome_id", "outcomeid"], 3)
        amount_raw = self._first_value(named, positional, ["amount"], 4)
        currency = self._first_value(named, positional, ["currency"], 5, "USD")

        if not all([event_id, market_id, side, outcome_id, amount_raw]):
            await update.message.reply_text(self._usage_quote(), parse_mode="HTML")
            return

        try:
            amount = float(amount_raw)
            quote = await asyncio.to_thread(client.get_quote, event_id, market_id, side, outcome_id, amount, currency)
            text = self._format_quote(quote, event_id, market_id, side, outcome_id, amount, currency)
        except Exception as e:
            text = f"Error fetching quote: {e}\n\n{self._usage_quote()}"
        await update.message.reply_text(text, parse_mode="HTML")

    async def _cmd_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        client = self._require_client()
        named, positional = self._parse_tokens(context.args)
        event_id = self._first_value(named, positional, ["event_id", "eventid"], 0)
        market_id = self._first_value(named, positional, ["market_id", "marketid"], 1)
        side = self._first_value(named, positional, ["side"], 2, "BUY")
        outcome_id = self._first_value(named, positional, ["outcome_id", "outcomeid"], 3)
        amount_raw = self._first_value(named, positional, ["amount"], 4)
        currency = self._first_value(named, positional, ["currency"], 5, "USD")
        order_type = self._first_value(named, positional, ["type", "order_type"], 6, "MARKET")
        price_raw = self._first_value(named, positional, ["price"], 7)
        time_in_force = self._first_value(named, positional, ["time_in_force", "tif"], 8)
        post_only_raw = self._first_value(named, positional, ["post_only", "postonly"], 9)
        max_slippage_raw = self._first_value(named, positional, ["max_slippage", "maxslippage"], 10)
        expires_at = self._first_value(named, positional, ["expires_at", "expiresat"], 11)

        if not all([event_id, market_id, side, outcome_id, amount_raw]):
            await update.message.reply_text(self._usage_order(), parse_mode="HTML")
            return

        try:
            amount = float(amount_raw)
            price = float(price_raw) if price_raw else None
            post_only = None
            if post_only_raw:
                post_only = post_only_raw.lower() in {"1", "true", "yes", "y"}
            max_slippage = float(max_slippage_raw) if max_slippage_raw else None

            result = await asyncio.to_thread(
                client.place_order,
                event_id,
                market_id,
                side,
                outcome_id,
                amount,
                currency,
                order_type,
                price,
                time_in_force or None,
                post_only,
                max_slippage,
                expires_at or None,
            )
            text = self._format_order(result)
        except Exception as e:
            text = f"Error placing order: {e}\n\n{self._usage_order()}"
        await update.message.reply_text(text, parse_mode="HTML")

    def build_application(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("balance", self._cmd_balance))
        app.add_handler(CommandHandler("portfolio", self._cmd_portfolio))
        app.add_handler(CommandHandler("events", self._cmd_events))
        app.add_handler(CommandHandler("quote", self._cmd_quote))
        app.add_handler(CommandHandler("order", self._cmd_order))
        self._app = app
        return app

    async def _run_background(self) -> None:
        app = self.build_application()
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Telegram bot polling started.")
        await asyncio.Event().wait()

    def start_background_polling(self) -> threading.Thread:
        thread = threading.Thread(target=lambda: asyncio.run(self._run_background()), daemon=True)
        thread.start()
        return thread

    def run_polling(self) -> None:
        app = self.build_application()
        logger.info("Telegram bot polling started.")
        app.run_polling(stop_signals=None)


def build_telegram_handler_from_env(bot_status_callback=None) -> Optional["TelegramHandler"]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID)

    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram notifications disabled.")
        return None

    return TelegramHandler(token=token, chat_id=chat_id, bot_status_callback=bot_status_callback)
