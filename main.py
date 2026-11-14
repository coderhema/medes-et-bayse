"""medes-et-bayse entry point (Procfile: `worker: python main.py`).

Starts a tiny health server, the trading agent loop (DRY_RUN=true by default) and, when
TELEGRAM_BOT_TOKEN is set, the Telegram bot (manual /quote, /order, /events ... plus /agent).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from medes_et_bayse.agent import AgentConfig, TradingAgent
from medes_et_bayse.agent.telegram import telegram_sender_from_env
from medes_et_bayse.config import build_client, load_runtime_config

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("medes")

COMMANDS = [
    {"command": "agent", "description": "Trading agent status"},
    {"command": "quote", "description": "Get a market quote"},
    {"command": "order", "description": "Place a trade order"},
    {"command": "balance", "description": "Check your wallet balance"},
    {"command": "portfolio", "description": "View open positions"},
    {"command": "events", "description": "List active markets"},
    {"command": "help", "description": "Show bot usage info"},
]


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        ok = self.path in {"/", "/health", "/healthz"}
        body = b"ok" if ok else b""
        self.send_response(200 if ok else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def _start_http_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("health server on :%s", port)


def build_application(client: Any, agent: TradingAgent, token: str):
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

    from medes_et_bayse import (
        build_balance_command, build_help_command, build_portfolio_command, fund_handler_factory,
        natural_language_handler_factory, order_handler_factory, quote_handler_factory,
        watchlist_callback_handler_factory, watchlist_handler_factory, withdraw_handler_factory,
    )

    application = Application.builder().token(token).build()

    def _msg(update: Any):
        return getattr(update, "effective_message", None) or getattr(update, "message", None)

    async def agent_handler(update: Any, context: Any) -> None:
        message = _msg(update)
        if message is None:
            return
        last = agent.store.last_cycle()
        text = last["summary"] if last and last.get("summary") else "Agent has not completed a cycle yet."
        await message.reply_text(text, parse_mode="HTML")

    def _simple(builder):
        async def handler(update: Any, context: Any) -> None:
            message = _msg(update)
            if message is not None:
                await message.reply_text(builder().text, parse_mode="HTML")
        return handler

    application.add_handler(CommandHandler("agent", agent_handler))
    application.add_handler(CommandHandler("quote", quote_handler_factory(client)))
    application.add_handler(CommandHandler("order", order_handler_factory(client)))
    application.add_handler(CommandHandler("fund", fund_handler_factory()))
    application.add_handler(CommandHandler("withdraw", withdraw_handler_factory()))
    application.add_handler(CommandHandler("events", watchlist_handler_factory(client)))
    application.add_handler(CommandHandler("balance", _simple(lambda: build_balance_command(client))))
    application.add_handler(CommandHandler("portfolio", _simple(lambda: build_portfolio_command(client))))
    application.add_handler(CommandHandler("help", _simple(build_help_command)))
    application.add_handler(CallbackQueryHandler(watchlist_callback_handler_factory(client)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language_handler_factory(client)))

    async def post_init(app: Any) -> None:
        from telegram import BotCommand

        await app.bot.set_my_commands([BotCommand(c["command"], c["description"]) for c in COMMANDS])

    application.post_init = post_init
    return application


def main() -> None:
    _start_http_server()
    cfg = AgentConfig.from_env()
    runtime = load_runtime_config()
    if not cfg.dry_run and not runtime.has_credentials:
        raise RuntimeError("DRY_RUN=false requires BAYSE_PUBLIC_KEY and BAYSE_SECRET_KEY")
    client = build_client(runtime, require_credentials=not cfg.dry_run)
    agent = TradingAgent(client, cfg, reporter=telegram_sender_from_env())
    logger.info("config %s", json.dumps({k: v for k, v in cfg.__dict__.items()}, default=str))

    stop = threading.Event()
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        logger.info("TELEGRAM_BOT_TOKEN not set; running the agent headless")
        agent.run_forever(stop)
        return
    threading.Thread(target=agent.run_forever, kwargs={"stop_event": stop}, daemon=True).start()
    try:
        build_application(client, agent, token).run_polling(drop_pending_updates=True)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
