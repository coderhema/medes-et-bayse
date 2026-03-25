"""Poke API client for backend orchestration and notifications.

The bot uses Poke as its "brain" backend:
  - Sends trade signals and results back to Poke
  - Poke can trigger the bot via cron webhook (Poke Recipe)
  - Supports alerting the user via Poke's notification system
"""

import httpx
from loguru import logger
from typing import Optional, Any


class PokeClient:
    """Poke API client for notifications and webhook integration."""

    def __init__(self, api_key: str, webhook_url: str):
        self.api_key = api_key
        self.webhook_url = webhook_url
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
        """Send a notification/signal to the Poke webhook.

        The Poke Recipe on the other end can:
        - Text/notify the user
        - Log results to Notion
        - Trigger follow-up automations

        Args:
            message: Human-readable summary.
            payload: Optional structured data (trade signals, results).
            level: 'info', 'success', 'error'

        Returns:
            True if the webhook was reached successfully.
        """
        if not self.webhook_url:
            logger.warning("POKE_WEBHOOK_URL not set — skipping notification")
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
