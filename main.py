from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, request

from medes_et_bayse import (
    BayseClient,
    build_balance_command,
    build_client,
    build_fund_command,
    build_help_command,
    build_portfolio_command,
    build_withdraw_command,
    fund_handler_factory,
    natural_language_handler_factory,
    order_handler_factory,
    quote_handler_factory,
    runtime_config,
    watchlist_callback_handler_factory,
    watchlist_handler_factory,
    withdraw_handler_factory,
)
from medes_et_bayse.hermes import HermesAgent, HermesDatabase, HermesLoopConfig

try:
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
except Exception as exc:  # pragma: no cover
    raise RuntimeError(f"python-telegram-bot is required to run this bot: {exc}") from exc

COMMANDS = [
    {"command": "quote", "description": "Get a market quote"},
    {"command": "order", "description": "Place a trade order"},
    {"command": "balance", "description": "Check your wallet balance"},
    {"command": "portfolio", "description": "View open positions"},
    {"command": "fund", "description": "Show funding options"},
    {"command": "withdraw", "description": "Show withdrawal options"},
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


def build_application(client: BayseClient, store: HermesDatabase, config: HermesLoopConfig) -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    application = Application.builder().token(token).build()
    application.bot_data["poke_api_key"] = runtime_config.poke_api_key
    application.bot_data["hermes_store"] = store
    application.bot_data["hermes_client"] = client
    application.bot_data["hermes_loop_config"] = config
    application.bot_data["hermes_framework"] = {
        "model": config.framework_model,
        "base_url": config.framework_base_url,
        "max_iterations": config.framework_max_iterations,
        "skip_memory": config.framework_skip_memory,
    }

    async def help_handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        await message.reply_text(build_help_command().text, parse_mode="HTML")

    async def balance_handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        result = build_balance_command(client)
        await message.reply_text(result.text, parse_mode="HTML")

    async def portfolio_handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        result = build_portfolio_command(client)
        await message.reply_text(result.text, parse_mode="HTML")

    application.add_handler(CommandHandler("quote", quote_handler_factory(client)))
    application.add_handler(CommandHandler("order", order_handler_factory(client)))
    application.add_handler(CommandHandler("fund", fund_handler_factory()))
    application.add_handler(CommandHandler("withdraw", withdraw_handler_factory()))
    application.add_handler(CommandHandler("events", watchlist_handler_factory(client)))
    application.add_handler(CommandHandler("balance", balance_handler))
    application.add_handler(CommandHandler("portfolio", portfolio_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CallbackQueryHandler(watchlist_callback_handler_factory(client)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language_handler_factory(client)))
    return application


def _start_hermes_agent(agent: HermesAgent) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    thread = threading.Thread(target=agent.run_forever, kwargs={"stop_event": stop_event}, daemon=True)
    thread.start()
    print(json.dumps({"hermes": "started", "interval": agent.config.cycle_interval_seconds}, ensure_ascii=False), flush=True)
    return stop_event, thread


def main() -> None:
    _start_http_server()

    client = build_client(runtime_config)
    store = HermesDatabase()
    loop_config = HermesLoopConfig.from_env()
    agent = HermesAgent(client, store, loop_config)

    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    hermes_stop_event: threading.Event | None = None
    hermes_thread: threading.Thread | None = None

    if token:
        result = set_my_commands(token)
        print(
            json.dumps(
                {
                    "startup": "ok",
                    "setMyCommands": result.get("ok", False),
                    "pokeApiKeyConfigured": bool(runtime_config.poke_api_key),
                    "hermesFrameworkModel": loop_config.framework_model,
                    "hermesFrameworkBaseUrlConfigured": bool(loop_config.framework_base_url),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        application = build_application(client, store, loop_config)
        hermes_stop_event, hermes_thread = _start_hermes_agent(agent)
        print(json.dumps({"polling": "starting"}, ensure_ascii=False), flush=True)
        try:
            application.run_polling(drop_pending_updates=True)
        finally:
            if hermes_stop_event is not None:
                hermes_stop_event.set()
            if hermes_thread is not None:
                hermes_thread.join(timeout=5)
    else:
        print(
            json.dumps(
                {
                    "startup": "ok",
                    "setMyCommands": False,
                    "pokeApiKeyConfigured": bool(runtime_config.poke_api_key),
                    "hermesFrameworkModel": loop_config.framework_model,
                    "hermesFrameworkBaseUrlConfigured": bool(loop_config.framework_base_url),
                    "telegram": "disabled",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        print(json.dumps({"hermes": "running-headless"}, ensure_ascii=False), flush=True)
        agent.run_forever()


if __name__ == "__main__":
    main()
