"""Telegram bot handler for medes-et-bayse.

Uses python-telegram-bot to:
  - Send trading signals and alerts to a configured Telegram chat
  - Respond to basic commands: /start, /status, /balance
  - Mirror all Poke webhook notifications to Telegram

Setup:
  1. Set TELEGRAM_BOT_TOKEN in your .env file.
  2. TELEGRAM_CHAT_ID defaults to 6433282551 but can be overridden via .env.
  3. Start the handler alongside the main bot loop, or run it
     standalone for command listening.
"""

import os
import asyncio
from typing import Optional

from loguru import logger

try:
    from telegram import Update, Bot
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
    )
except ImportError:
    raise ImportError(
        "python-telegram-bot is required. "
        "Install it with: pip install python-telegram-bot"
    )

# Default destination chat ID — can be overridden by TELEGRAM_CHAT_ID env var
DEFAULT_CHAT_ID = "6433282551"


class TelegramHandler:
    """Wraps python-telegram-bot for trading signal delivery and command handling."""

    def __init__(
        self,
        token: str,
        chat_id: str = DEFAULT_CHAT_ID,
        bot_status_callback=None,
        balance_callback=None,
    ):
        """
        Args:
            token: Telegram bot token from BotFather.
            chat_id: Target chat/group ID to push notifications to.
                     Defaults to DEFAULT_CHAT_ID (6433282551).
            bot_status_callback: Optional callable() -> str for /status replies.
            balance_callback: Optional callable() -> str for /balance replies.
        """
        self.token = token
        self.chat_id = chat_id
        self._bot_status_callback = bot_status_callback
        self._balance_callback = balance_callback
        self._app: Optional[Application] = None

    # ------------------------------------------------------------------
    # Outbound: push messages to the configured chat
    # ------------------------------------------------------------------

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a plain text message to the configured Telegram chat.

        Args:
            text: Message body (supports HTML formatting by default).
            parse_mode: 'HTML' or 'MarkdownV2'.

        Returns:
            True if the message was sent successfully.
        """
        try:
            bot = Bot(token=self.token)
            async with bot:
                await bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=parse_mode,
                )
            logger.info(f"Telegram message sent: {text[:80]}..." if len(text) > 80 else f"Telegram message sent: {text}")
            return True
        except Exception as e:
            logger.error(f"Telegram send_message failed: {e}")
            return False

    def send_message_sync(self, text: str, parse_mode: str = "HTML") -> bool:
        """Synchronous wrapper around send_message for use in non-async code."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule as a task if already inside an event loop
                future = asyncio.ensure_future(self.send_message(text, parse_mode))
                return True  # fire-and-forget
            else:
                return loop.run_until_complete(self.send_message(text, parse_mode))
        except RuntimeError:
            return asyncio.run(self.send_message(text, parse_mode))

    async def send_signal(
        self,
        event_title: str,
        side: str,
        edge: float,
        stake: float,
        dry_run: bool = False,
    ) -> bool:
        """Format and send a structured trading signal notification.

        Args:
            event_title: Human-readable market/event name.
            side: Trade direction, e.g. 'YES' or 'NO'.
            edge: Estimated edge as a decimal (e.g. 0.07 for 7%).
            stake: Stake amount in base currency.
            dry_run: If True, marks the signal as simulated.

        Returns:
            True if the message was delivered.
        """
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
        """Send a general alert message with an emoji prefix based on level.

        Args:
            message: Alert body text.
            level: 'info', 'success', or 'error'.

        Returns:
            True if delivered.
        """
        emoji = {"info": "ℹ️", "success": "✅", "error": "❌"}.get(level, "🔔")
        text = f"{emoji} <b>medes-et-bayse</b>\n{message}"
        return await self.send_message(text)

    # ------------------------------------------------------------------
    # Inbound: command handlers
    # ------------------------------------------------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        await update.message.reply_text(
            "👋 <b>medes-et-bayse bot online.</b>\n"
            "Available commands:\n"
            "/status — bot and market status\n"
            "/balance — current bankroll and open positions",
            parse_mode="HTML",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command."""
        if self._bot_status_callback:
            try:
                status_text = self._bot_status_callback()
            except Exception as e:
                status_text = f"Error fetching status: {e}"
        else:
            status_text = "Bot is running. No status callback configured."
        await update.message.reply_text(
            f"<b>Status</b>\n{status_text}",
            parse_mode="HTML",
        )

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /balance command."""
        if self._balance_callback:
            try:
                balance_text = self._balance_callback()
            except Exception as e:
                balance_text = f"Error fetching balance: {e}"
        else:
            balance_text = "No balance callback configured."
        await update.message.reply_text(
            f"<b>Balance</b>\n{balance_text}",
            parse_mode="HTML",
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def build_application(self) -> Application:
        """Build and return the PTB Application with all command handlers registered."""
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("balance", self._cmd_balance))
        self._app = app
        return app

    def run_polling(self) -> None:
        """Start the bot in long-polling mode (blocking). Use for standalone command listener."""
        app = self.build_application()
        logger.info("Telegram bot polling started.")
        app.run_polling()


def build_telegram_handler_from_env(
    bot_status_callback=None,
    balance_callback=None,
) -> Optional["TelegramHandler"]:
    """Convenience factory that reads token and chat ID from environment variables.

    Falls back to DEFAULT_CHAT_ID (6433282551) if TELEGRAM_CHAT_ID is not set.

    Returns:
        A configured TelegramHandler, or None if TELEGRAM_BOT_TOKEN is not set.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID)

    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram notifications disabled.")
        return None

    return TelegramHandler(
        token=token,
        chat_id=chat_id,
        bot_status_callback=bot_status_callback,
        balance_callback=balance_callback,
    )
