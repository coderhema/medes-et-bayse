from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib import error, request

from medes_et_bayse import (
    BayseClient,
    build_help_command,
    natural_language_handler_factory,
    order_handler_factory,
    quote_handler_factory,
    watchlist_callback_handler_factory,
    watchlist_handler_factory,
)
from medes_et_bayse.config import runtime_config

COMMANDS = [
    {"command": "quote", "description": "Get a market quote"},
    {"command": "order", "description": "Place a trade order"},
    {"command": "balance", "description": "Check your wallet balance"},
    {"command": "portfolio", "description": "View open positions"},
    {"command": "events", "description": "List active markets"},
    {"command": "help", "description": "Show bot usage info"},
]


@dataclass(frozen=True)
class BotRuntimeConfig:
    telegram_bot_token: Optional[str]
    poke_api_key: Optional[str]


def load_bot_runtime_config() -> BotRuntimeConfig:
    return BotRuntimeConfig(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN"),
        poke_api_key=runtime_config.poke_api_key,
    )


def _bot_api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def set_my_commands(token: str, commands: Iterable[dict[str, str]]) -> dict[str, Any]:
    payload = json.dumps({"commands": list(commands)}).encode("utf-8")
    req = request.Request(
        _bot_api_url(token, "setMyCommands"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {"ok": True}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8") if exc.fp else exc.reason
        raise RuntimeError(f"Telegram API error ({exc.code}): {body}") from exc


def build_application() -> Any:
    try:
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
    except Exception:
        return None

    config = load_bot_runtime_config()
    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    client = BayseClient(api_key=runtime_config.public_key, api_secret=runtime_config.secret_key, base_url=runtime_config.base_url)
    application = Application.builder().token(config.telegram_bot_token).build()
    application.bot_data["poke_api_key"] = config.poke_api_key

    async def help_handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        await message.reply_text(build_help_command().text)

    application.add_handler(CommandHandler("quote", quote_handler_factory(client)))
    application.add_handler(CommandHandler("order", order_handler_factory(client)))
    application.add_handler(CommandHandler("events", watchlist_handler_factory(client)))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CallbackQueryHandler(watchlist_callback_handler_factory(client)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language_handler_factory(client)))
    return application


def main() -> int:
    config = load_bot_runtime_config()
    if not config.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is missing", file=sys.stderr)
        return 2

    result = set_my_commands(config.telegram_bot_token, COMMANDS)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
