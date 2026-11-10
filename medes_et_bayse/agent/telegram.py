"""Telegram cycle reports (plain Bot API call). Disabled unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
from __future__ import annotations

import json
import logging
import os
from typing import Callable, Optional
from urllib import request

logger = logging.getLogger(__name__)

Sender = Callable[[str], None]


def telegram_sender_from_env() -> Optional[Sender]:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return None

    def send(text: str) -> None:
        body = json.dumps({"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
        req = request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST",
                              headers={"Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=20):
                pass
        except Exception as exc:  # pragma: no cover - network
            logger.warning("Telegram report failed: %s", exc)

    return send
