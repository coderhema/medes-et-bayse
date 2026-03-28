from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, request

from medes_et_bayse import (
    BayseClient,
    build_help_command,
    natural_language_handler_factory,
    order_handler_factory,
    quote_handler_factory,
    watchlist_callback_handler_factory,
    watchlist_handler_factory,
    runtime_config,
)

try:
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
except Exception as exc:  # pragma: no cover
    raise RuntimeError(f"python-telegram-bot is required to run this bot: {exc}") from exc

COMMANDS = [
    {"command": "quote", "description": "Get a market quote"},
    {"command": "order", "description": "Place a trade order"},
    {"command": "balance", "description": "Check your wallet balance"},
    {"command": "portfolio", "description": "View open positions"},
    {"command": "events", "description": "List active markets"},
    {"command": "help", "description": "Show bot usage info"},
]


def _bot_api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def set_my_commands(token: str) -> dict[str, Any]:
    payload = json.dumps({"commands": COMMANDS}).encode("utf-8")
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
    except error.HTTPError as exc:  # pragma: no cover
        body = exc.read().decode("utf-8") if exc.fp else exc.reason
        raise RuntimeError(f"Telegram API error ({exc.code}): {body}") from exc


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    client = BayseClient(
        api_key=runtime_config.public_key,
        api_secret=runtime_config.secret_key,
        base_url=runtime_config.base_url,
    )

    application = Application.builder().token(token).build()
    application.bot_data["poke_api_key"] = runtime_config.poke_api_key

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


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/health", "/healthz"}:
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def _start_http_server() -> ThreadingHTTPServer:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps({"http": "listening", "port": port}, ensure_ascii=False), flush=True)
    return server


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is missing", file=sys.stderr)
        raise SystemExit(2)

    _start_http_server()

    result = set_my_commands(token)
    print(json.dumps({"startup": "ok", "setMyCommands": result.get("ok", False), "pokeApiKeyConfigured": bool(runtime_config.poke_api_key)}, ensure_ascii=False), flush=True)

    application = build_application()
    print(json.dumps({"polling": "starting"}, ensure_ascii=False), flush=True)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
