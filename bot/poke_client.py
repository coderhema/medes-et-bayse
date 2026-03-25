"""Poke API client for backend orchestration and notifications.

The bot uses Poke as its "brain" backend:
  - Sends trade signals and results back to Poke
  - Poke can trigger the bot via cron webhook (Poke Recipe)
  - Supports alerting the user via Poke's notification system
  - Mirrors all notifications to Telegram when a TelegramHandler is provided
"""

import httpx
from loguru import logger
from typing import Optional, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from bot.telegram_handler import TelegramHandler


class PokeClient:
    """Poke API client for notifications and webhook integration."""

    def __init__(
        self,
        api_key: str,
        webhook_url: str,
        telegram: Optional["TelegramHandler"] = None,
    ):
        """
        Args:
            api_key: Poke API key for Authorization header.
            webhook_url: Poke webhook endpoint URL.
            telegram: Optional TelegramHandler instance. When provided,
                      every notify() call is also mirrored to Telegram.
        """
        self.api_key = api_key
        self.webhook_url = webhook_url
        self.telegram = telegram
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def notify(
        self,
        message: str,
        payload: Optional[Any] = None,
        level: str = "info",
    ) -> bool:
        """Send a notification/signal to the Poke webhook and (optionally) Telegram.

        The Poke Recipe on the other end can:
        - Text/notify the user
        - Log results to Notion
        - Trigger follow-up automations

        When a TelegramHandler is attached, the same message is also sent
        as a Telegram alert so the user gets real-time mobile notifications.

        Args:
            message: Human-readable summary.
            payload: Optional structured data (trade signals, results).
            level: 'info', 'success', 'error'

        Returns:
            True if at least the Poke webhook was reached successfully,
            or if the webhook is not configured but Telegram was notified.
        """
        poke_ok = self._send_to_poke(message, payload, level)
        telegram_ok = self._send_to_telegram(message, level)

        return poke_ok or telegram_ok

    def _send_to_poke(self, message: str, payload: Optional[Any], level: str) -> bool:
        """Internal: POST to the Poke webhook."""
        if not self.webhook_url:
            logger.warning("POKE_WEBHOOK_URL not set — skipping Poke notification")
            return False

        body = {
            "source": "medes-et-bayse",
            "level": level,
            "message": message,
            "payload": payload or {},
        }

        try:
            resp = httpx.post(self.webhook_url, headers=self.headers, json=body, timeout=10)
            resp.raise_for_status()
            logger.info(f"Poke notified: {message}")
            return True
        except Exception as e:
            logger.error(f"Poke notification failed: {e}")
            return False

    def _send_to_telegram(self, message: str, level: str) -> bool:
        """Internal: mirror the notification to Telegram if a handler is configured."""
        if self.telegram is None:
            return False
        return self.telegram.send_message_sync(
            text=f"<b>medes-et-bayse</b> [{level}]\n{message}"
        )

    def attach_telegram(self, telegram: "TelegramHandler") -> None:
        """Attach (or replace) the TelegramHandler after construction.

        Useful when the handler is created after PokeClient initialisation.
        """
        self.telegram = telegram
        logger.info("TelegramHandler attached to PokeClient.")
