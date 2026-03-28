from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from .client import BayseClient, BayseClientError
from .models import OrderResponse, QuoteResponse

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except Exception:  # pragma: no cover - keeps imports working in non-Telegram environments
    InlineKeyboardButton = Any  # type: ignore[assignment]
    InlineKeyboardMarkup = Any  # type: ignore[assignment]


DEBUG_SPAM_PHRASES = {"no signals this cycle"}
WATCHLIST_PAGE_SIZE = 10


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    text: str
    raw: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class StickerSetConfig:
    bull: Optional[str] = None
    bear: Optional[str] = None
    rocket: Optional[str] = None
    trophy: Optional[str] = None


def _split_args(text: str) -> list[str]:
    parts = (text or "").strip().split()
    if not parts:
        return []
    if parts[0].startswith("/"):
        return parts[1:]
    return parts


def _format_number(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num.is_integer():
        return str(int(num))
    return f"{num:.8f}".rstrip("0").rstrip(".")


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _side_emoji(side: Any) -> str:
    normalized = _normalize_text(side).lower()
    if normalized in {"buy", "long", "up", "bull", "bullish", "call"}:
        return "📈"
    if normalized in {"sell", "short", "down", "bear", "bearish", "put"}:
        return "📉"
    return ""


def _signal_emoji(direction: Any) -> str:
    normalized = _normalize_text(direction).lower()
    if normalized in {"up", "long", "bull", "bullish", "buy", "call", "rise", "higher"}:
        return "📈"
    if normalized in {"down", "short", "bear", "bearish", "sell", "put", "fall", "lower"}:
        return "📉"
    return ""


def _quote_text(response: QuoteResponse) -> str:
    quote = response.quote
    parts = [
        f"Quote for {quote.symbol or 'unknown'}",
        f"bid: {_format_number(quote.bid)}",
        f"ask: {_format_number(quote.ask)}",
        f"last: {_format_number(quote.last)}",
        f"mark: {_format_number(quote.mark)}",
        f"midpoint: {_format_number(quote.midpoint)}",
        f"timestamp: {quote.timestamp or 'n/a'}",
    ]
    return "\n".join(parts)


def _order_text(response: OrderResponse) -> str:
    order = response.order
    raw = response.raw or {}
    emoji = _side_emoji(order.side) or _signal_emoji(order.side)
    heading = f"{emoji} Order {order.order_id or 'n/a'}" if emoji else f"Order {order.order_id or 'n/a'}"
    parts = [
        heading,
        f"status: {order.status or 'n/a'}",
        f"event id: {_format_number(raw.get('eventId') or raw.get('event_id'))}",
        f"market id: {_format_number(raw.get('marketId') or raw.get('market_id'))}",
        f"outcome: {_format_number(raw.get('outcome') or raw.get('outcomeId') or raw.get('outcome_id') or raw.get('outcomeIndex'))}",
        f"side: {order.side or 'n/a'}",
        f"type: {order.order_type or raw.get('type') or 'n/a'}",
        f"amount: {_format_number(raw.get('amount') or order.quantity)}",
        f"price: {_format_number(order.limit_price or raw.get('price'))}",
        f"filled quantity: {_format_number(order.filled_quantity or raw.get('filled'))}",
        f"average fill price: {_format_number(order.average_fill_price)}",
        f"created at: {order.created_at or raw.get('createdAt') or 'n/a'}",
        f"updated at: {order.updated_at or raw.get('updatedAt') or 'n/a'}",
    ]
    return "\n".join(parts)


def _error_text(exc: BayseClientError) -> str:
    if exc.error is not None:
        details = []
        if exc.error.code:
            details.append(f"code: {exc.error.code}")
        details.append(f"message: {exc.error.message}")
        if exc.error.details:
            details.append(f"details: {exc.error.details}")
        return "Bayse API error\n" + "\n".join(details)
    if exc.status_code is not None:
        return f"Bayse API error\nstatus: {exc.status_code}\nmessage: {exc}"
    return f"Bayse API error\nmessage: {exc}"


def _extract_collection(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("events", "items", "results", "data", "markets"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_collection(value)
            if nested:
                return nested
    return []


def _first_string(*values: Any, default: str = "") -> str:
    for value in values:
        text = _normalize_text(value)
        if text:
            return text
    return default


def _event_id(event: dict[str, Any]) -> str:
    return _first_string(event.get("id"), event.get("eventId"), event.get("event_id"), event.get("slug"), default="unknown")


def _event_title(event: dict[str, Any]) -> str:
    return _first_string(
        event.get("title"),
        event.get("name"),
        event.get("question"),
        event.get("marketName"),
        event.get("label"),
        event.get("symbol"),
        default="Untitled market",
    )


def _event_description(event: dict[str, Any]) -> str:
    return _first_string(
        event.get("description"),
        event.get("subtitle"),
        event.get("category"),
        event.get("state"),
        event.get("status"),
    )


def _event_direction(event: dict[str, Any]) -> str:
    return _first_string(
        event.get("direction"),
        event.get("side"),
        event.get("signal"),
        event.get("trend"),
        event.get("bias"),
        event.get("sentiment"),
    )


def _event_button_label(event: dict[str, Any]) -> str:
    emoji = _signal_emoji(_event_direction(event))
    title = _event_title(event)
    if emoji:
        return f"{emoji} {title}"
    return title


def _watchlist_keyboard(events: Iterable[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for event in events:
        event_id = _event_id(event)
        rows.append([InlineKeyboardButton(_event_button_label(event), callback_data=f"watch:{event_id}")])

    rows.append([InlineKeyboardButton("Refresh list", callback_data="watch:refresh")])
    return InlineKeyboardMarkup(rows)


def _watchlist_text(events: list[dict[str, Any]]) -> str:
    lines = ["What do you want to watch?"]
    for index, event in enumerate(events, start=1):
        emoji = _signal_emoji(_event_direction(event))
        prefix = f"{emoji} " if emoji else ""
        title = _event_title(event)
        description = _event_description(event)
        line = f"{index}. {prefix}{title}"
        if description:
            line += f" — {description}"
        lines.append(line)
    return "\n".join(lines)


def _watchlist_details(event: dict[str, Any]) -> str:
    lines = [
        f"Watching: {_event_title(event)}",
        f"event id: {_event_id(event)}",
    ]
    description = _event_description(event)
    if description:
        lines.append(f"details: {description}")
    direction = _event_direction(event)
    if direction:
        lines.append(f"signal: {direction}")
    return "\n".join(lines)


def _should_suppress_debug_message(text: Any) -> bool:
    normalized = _normalize_text(text).lower()
    return any(phrase in normalized for phrase in DEBUG_SPAM_PHRASES)


def build_quote_command(client: BayseClient, text: str) -> CommandResult:
    args = _split_args(text)
    if not args:
        return CommandResult(False, "Usage: /quote MARKET_ID")
    market_id = args[0]
    try:
        response = QuoteResponse.from_dict(client.get_ticker(market_id))
        return CommandResult(True, _quote_text(response), raw=response.raw)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


def build_order_command(client: BayseClient, text: str) -> CommandResult:
    args = _split_args(text)
    if len(args) < 6:
        return CommandResult(False, "Usage: /order EVENT_ID MARKET_ID OUTCOME SIDE AMOUNT CURRENCY [PRICE] [LIMIT|MARKET]")

    event_id = args[0]
    market_id = args[1]
    outcome = args[2]
    side = args[3]

    try:
        amount = float(args[4])
    except ValueError:
        return CommandResult(False, "Usage: /order EVENT_ID MARKET_ID OUTCOME SIDE AMOUNT CURRENCY [PRICE] [LIMIT|MARKET]")

    currency = args[5].upper()
    price: Optional[float] = None
    order_type = "LIMIT"

    if len(args) >= 7:
        trailing = args[6]
        try:
            price = float(trailing)
            if len(args) >= 8:
                order_type = args[7].upper()
        except ValueError:
            order_type = trailing.upper()
            if order_type not in {"LIMIT", "MARKET"}:
                return CommandResult(False, "Usage: /order EVENT_ID MARKET_ID OUTCOME SIDE AMOUNT CURRENCY [PRICE] [LIMIT|MARKET]")
            if order_type == "MARKET":
                price = None

    try:
        response = client.place_order(
            event_id,
            market_id,
            outcome=outcome,
            side=side,
            amount=amount,
            currency=currency,
            order_type=order_type,
            price=price,
        )
        return CommandResult(True, _order_text(OrderResponse.from_dict(response)), raw=response)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


def build_watchlist_command(client: BayseClient, text: str = "") -> CommandResult:
    try:
        payload = client.list_events(page=1, size=WATCHLIST_PAGE_SIZE)
        events = _extract_collection(payload)
        if not events:
            return CommandResult(False, "No markets or events were returned by Bayse.")
        raw = {"events": events, "payload": payload}
        return CommandResult(True, _watchlist_text(events), raw=raw)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


def format_signal_message(direction: Any, title: str, details: Optional[str] = None) -> str:
    emoji = _signal_emoji(direction)
    header = f"{emoji} {title}" if emoji else title
    parts = [header]
    if details:
        parts.append(details)
    return "\n".join(parts)


def _portfolio_mapping(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in ("portfolio", "data", "result", "wallet", "account"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        return payload
    return {}


def _portfolio_text(payload: Any, heading: str) -> str:
    data = _portfolio_mapping(payload)
    positions = _extract_collection(data.get("positions") or data.get("openPositions") or data.get("holdings") or [])
    lines = [heading]

    balance = _first_string(
        data.get("balance"),
        data.get("walletBalance"),
        data.get("availableBalance"),
        data.get("cashBalance"),
        data.get("cash"),
        default="n/a",
    )
    lines.append(f"wallet balance: {balance}")

    if positions:
        lines.append("open positions:")
        for idx, position in enumerate(positions, start=1):
            title = _first_string(
                position.get("title"),
                position.get("name"),
                position.get("marketName"),
                position.get("symbol"),
                position.get("id"),
                default=f"Position {idx}",
            )
            size = _first_string(
                position.get("quantity"),
                position.get("size"),
                position.get("amount"),
                position.get("exposure"),
                default="n/a",
            )
            direction = _signal_emoji(position.get("direction") or position.get("side") or position.get("sentiment"))
            prefix = f"{direction} " if direction else ""
            lines.append(f"{idx}. {prefix}{title} — {size}")
    else:
        lines.append("open positions: none")

    return "\n".join(lines)


def build_balance_command(client: BayseClient, text: str = "") -> CommandResult:
    try:
        payload = client.get_portfolio()
        return CommandResult(True, _portfolio_text(payload, "Wallet balance"), raw=payload)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


def build_portfolio_command(client: BayseClient, text: str = "") -> CommandResult:
    try:
        payload = client.get_portfolio()
        return CommandResult(True, _portfolio_text(payload, "Open positions"), raw=payload)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


def build_help_command() -> CommandResult:
    return CommandResult(
        True,
        "\n".join(
            [
                "Medes Et Bayse commands:",
                "/quote MARKET_ID - Get a market quote",
                "/order EVENT_ID MARKET_ID OUTCOME SIDE AMOUNT CURRENCY [PRICE] [LIMIT|MARKET] - Place a trade order",
                "/balance - Check your wallet balance",
                "/portfolio - View open positions",
                "/events - List active markets",
                "/help - Show bot usage info",
            ]
        ),
    )


def _order_scenario_from_result(result: CommandResult) -> Optional[str]:
    raw = result.raw or {}
    status = _first_string(raw.get("status"), raw.get("state")).lower()
    if status in {"filled", "executed", "complete", "completed", "success", "successfully placed"}:
        return "trophy"
    if status:
        return "rocket"
    side = _first_string(raw.get("side"), raw.get("direction")).lower()
    if side in {"buy", "long", "up", "bull", "bullish", "call"}:
        return "bull"
    if side in {"sell", "short", "down", "bear", "bearish", "put"}:
        return "bear"
    return None


def format_quote_response(response: QuoteResponse) -> str:
    return _quote_text(response)


def format_order_response(response: OrderResponse) -> str:
    return _order_text(response)


def is_debug_spam_message(text: Any) -> bool:
    return _should_suppress_debug_message(text)


def sticker_config_from_env() -> StickerSetConfig:
    import os

    return StickerSetConfig(
        bull=os.getenv("MEDES_BULL_STICKER_FILE_ID"),
        bear=os.getenv("MEDES_BEAR_STICKER_FILE_ID"),
        rocket=os.getenv("MEDES_ROCKET_STICKER_FILE_ID"),
        trophy=os.getenv("MEDES_TROPHY_STICKER_FILE_ID"),
    )


async def send_scenario_sticker(message: Any, scenario: str, *, config: Optional[StickerSetConfig] = None) -> bool:
    config = config or sticker_config_from_env()
    scenario_key = _normalize_text(scenario).lower()
    sticker_file_id: Optional[str]
    if scenario_key in {"bull", "bullish", "long", "up"}:
        sticker_file_id = config.bull
    elif scenario_key in {"bear", "bearish", "short", "down"}:
        sticker_file_id = config.bear
    elif scenario_key in {"rocket", "success", "trade_success", "win"}:
        sticker_file_id = config.rocket
    elif scenario_key in {"trophy", "profit", "win_trade", "achievement"}:
        sticker_file_id = config.trophy
    else:
        sticker_file_id = None

    if not sticker_file_id:
        return False

    bot = getattr(message, "bot", None)
    if bot is None:
        chat = getattr(message, "chat", None)
        if chat is None:
            return False
        bot = getattr(chat, "bot", None)
    if bot is None:
        return False

    await bot.send_sticker(chat_id=getattr(message.chat, "id", None), sticker=sticker_file_id)
    return True


def quote_handler_factory(client: BayseClient) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        text = getattr(message, "text", "") or ""
        result = build_quote_command(client, text)
        if _should_suppress_debug_message(result.text):
            return
        await message.reply_text(result.text)

    return handler


def order_handler_factory(client: BayseClient) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        text = getattr(message, "text", "") or ""
        result = build_order_command(client, text)
        if _should_suppress_debug_message(result.text):
            return
        await message.reply_text(result.text)
        scenario = _order_scenario_from_result(result)
        if scenario:
            await send_scenario_sticker(message, scenario)

    return handler


def watchlist_handler_factory(client: BayseClient) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        result = build_watchlist_command(client)
        if not result.ok:
            await message.reply_text(result.text)
            return
        events = result.raw.get("events", []) if result.raw else []
        await message.reply_text(result.text, reply_markup=_watchlist_keyboard(events))

    return handler


def watchlist_callback_handler_factory(client: BayseClient) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        query = getattr(update, "callback_query", None)
        if query is None or not getattr(query, "data", None):
            return

        data = str(query.data)
        if not data.startswith("watch:"):
            return

        await query.answer()
        selected = data.split(":", 1)[1]
        if selected == "refresh":
            result = build_watchlist_command(client)
            if not result.ok:
                await query.edit_message_text(result.text)
                return
            events = result.raw.get("events", []) if result.raw else []
            await query.edit_message_text(result.text, reply_markup=_watchlist_keyboard(events))
            return

        try:
            payload = client.get_event(selected)
            events = _extract_collection(payload)
            event = events[0] if events else payload if isinstance(payload, dict) else {}
        except BayseClientError as exc:
            await query.edit_message_text(_error_text(exc))
            return
        except Exception as exc:
            await query.edit_message_text(f"Bayse API error\nmessage: {exc}")
            return

        if not isinstance(event, dict):
            event = {}

        context.user_data["watchlist_event_id"] = selected
        context.user_data["watchlist_event"] = event

        details = _watchlist_details(event)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Refresh list", callback_data="watch:refresh")],
        ])
        await query.edit_message_text(details, reply_markup=keyboard)
        direction = _event_direction(event)
        if direction:
            await send_scenario_sticker(query.message, direction)

    return handler
