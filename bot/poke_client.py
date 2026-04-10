"""Poke API client for backend orchestration and notifications.

The bot uses Poke as its "brain" backend:
  - Sends trade signals and results back to Poke
  - Poke can trigger the bot via cron webhook (Poke Recipe)
  - Supports alerting the user via Poke's notification system
  - Mirrors all notifications to Telegram when a TelegramHandler is provided
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Any, TYPE_CHECKING
import httpx
import time
from loguru import logger

if TYPE_CHECKING:
    from bot.telegram_handler import TelegramHandler


class PokeClient:
    """Poke API client for notifications and webhook integration."""

    def __init__(
        self,
        api_key: str,
        webhook_url: str,
        telegram: Optional["TelegramHandler"] = None,
        *,
        request_timeout: float = 8.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.webhook_url = webhook_url
        self.telegram = telegram
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        self._client = httpx.Client(headers=self.headers, timeout=httpx.Timeout(request_timeout, connect=4.0))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='poke-notify')

    def notify(
        self,
        message: str,
        payload: Optional[Any] = None,
        level: str = 'info',
    ) -> bool:
        if not self.webhook_url and self.telegram is None:
            logger.warning('No Poke webhook or Telegram handler configured; dropping notification')
            return False

        try:
            self._executor.submit(self._deliver, message, payload, level)
            return True
        except Exception as exc:
            logger.warning('Notification queue unavailable, sending synchronously: %s', exc)
            return self._deliver(message, payload, level)

    def _deliver(self, message: str, payload: Optional[Any], level: str) -> bool:
        poke_ok = self._send_to_poke(message, payload, level)
        telegram_ok = self._send_to_telegram(message, level)
        return poke_ok or telegram_ok

    def _send_to_poke(self, message: str, payload: Optional[Any], level: str) -> bool:
        if not self.webhook_url:
            return False

        body = {
            'source': 'medes-et-bayse',
            'level': level,
            'message': message,
            'payload': payload or {},
        }

        delay = 0.5
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._client.post(self.webhook_url, json=body)
                resp.raise_for_status()
                logger.info('Poke notified: %s', message)
                return True
            except Exception as e:
                logger.warning('Poke notification attempt %s/%s failed: %s', attempt, self.max_retries, e)
                if attempt >= self.max_retries:
                    return False
                time.sleep(delay)
                delay = min(delay * 2, 4.0)
        return False

    def _send_to_telegram(self, message: str, level: str) -> bool:
        if self.telegram is None:
            return False
        return self.telegram.send_message_sync(text=f'<b>medes-et-bayse</b> [{level}]\n{message}')

    def attach_telegram(self, telegram: 'TelegramHandler') -> None:
        self.telegram = telegram
        logger.info('TelegramHandler attached to PokeClient.')
