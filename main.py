from __future__ import annotations

import json
import os
import sys
from typing import Any, Iterable
from urllib import error, request

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


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is missing", file=sys.stderr)
        return 2

    result = set_my_commands(token, COMMANDS)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
