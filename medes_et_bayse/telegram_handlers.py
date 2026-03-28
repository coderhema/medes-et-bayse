from __future__ import annotations

from dataclasses import dataclass
import json
from html import escape as html_escape
from typing import Any, Callable, Iterable, Optional

from .client import BayseClient, BayseClientError
from .models import OrderResponse, QuoteResponse

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except Exception:  # pragma: no cover
    InlineKeyboardButton = Any  # type: ignore[assignment]
    InlineKeyboardMarkup = Any  # type: ignore[assignment]


DEBUG_SPAM_PHRASES = {"no signals this cycle"}
WATCHLIST_PAGE_SIZE = 10
GENERAL_QUANT_GUIDANCE = "quant best practice: prefer limit orders, size positions deliberately, and define an exit before entry."


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


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_html(value: Any) -> str:
    return html_escape(_normalize_text(value))


def _bold(value: Any) -> str:
    return f"<b>{_safe_html(value)}</b>"


def _code(value: Any) -> str:
    text = _normalize_text(value)
    return f"<code>{html_escape(text)}</code>" if text else "<code>n/a</code>"


def _split_args(text: str) -> list[str]:
    parts = (text or "").strip().split()
    if not parts:
        return []
    return parts[1:] if parts[0].startswith("/") else parts


def _first_string(*values: Any, default: str = "") -> str:
    for value in values:
        text = _normalize_text(value)
        if text:
            return text
    return default


def _mapping_value(mapping: Any, *path: str) -> Any:
    current = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


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


def _price_direction_emoji(*values: Any) -> str:
    for value in values:
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if num > 0:
            return "📈"
        if num < 0:
            return "📉"
    return ""


def _label_from_payload(payload: Any, default: str = "Untitled market") -> str:
    if not isinstance(payload, dict):
        return default
    return _first_string(
        _mapping_value(payload, "metadata", "name"),
        _mapping_value(payload, "metadata", "title"),
        _mapping_value(payload, "event", "metadata", "name"),
        _mapping_value(payload, "event", "metadata", "title"),
        payload.get("displayName"),
        payload.get("display_name"),
        payload.get("eventName"),
        payload.get("event_name"),
        payload.get("name"),
        payload.get("title"),
        payload.get("question"),
        payload.get("marketName"),
        payload.get("market_name"),
        payload.get("marketTitle"),
        payload.get("market_title"),
        payload.get("marketLabel"),
        payload.get("market_label"),
        payload.get("label"),
        payload.get("symbol"),
        default=default,
    )


def _event_title(event: dict[str, Any]) -> str:
    return _label_from_payload(event)


def _market_title(market: dict[str, Any]) -> str:
    return _label_from_payload(market)


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


def _market_direction(market: dict[str, Any]) -> str:
    return _first_string(
        market.get("direction"),
        market.get("side"),
        market.get("signal"),
        _mapping_value(market, "pricing", "direction"),
        _mapping_value(market, "pricing", "trend"),
    )


def _quote_text(response: QuoteResponse) -> str:
    quote = response.quote
    raw = response.raw or {}
    title = _first_string(
        _mapping_value(raw, "metadata", "name"),
        _mapping_value(raw, "metadata", "title"),
        _mapping_value(raw, "event", "metadata", "name"),
        _mapping_value(raw, "event", "metadata", "title"),
        raw.get("marketTitle"),
        raw.get("marketName"),
        quote.symbol,
        default="Unknown market",
    )
    direction = _first_string(raw.get("direction"), raw.get("trend"), raw.get("signal"), raw.get("bias"))
    emoji = _signal_emoji(direction) or _price_direction_emoji(raw.get("change"), raw.get("changePercent"), raw.get("priceChange"))
    move_text = _first_string(raw.get("changeDirection"), raw.get("move"), raw.get("movement"), direction)
    parts = [f"Quote for {_bold(title)}"]
    parts.append(f"Bid: {_code(_format_number(quote.bid))} Ask: {_code(_format_number(quote.ask))}")
    parts.append(f"Last: {_code(_format_number(quote.last))} Mark: {_code(_format_number(quote.mark))}")
    parts.append(f"Midpoint: {_code(_format_number(quote.midpoint))}")
    if emoji or move_text:
        parts.append(f"Move: {emoji + ' ' if emoji else ''}{_safe_html(move_text or 'steady')}")
    timestamp = quote.timestamp or raw.get("updatedAt") or raw.get("timestamp")
    if timestamp:
        parts.append(f"Updated: {_code(timestamp)}")
    return "\n".join(parts)


def _order_text(response: OrderResponse) -> str:
    order = response.order
    raw = response.raw or {}
    emoji = _side_emoji(order.side) or _signal_emoji(order.side)
    side_emoji = _signal_emoji(order.side) or _side_emoji(order.side)
    parts = [f"{emoji} <b>Order update</b>" if emoji else "<b>Order update</b>"]

    event_title = _first_string(
        _mapping_value(raw, "event", "metadata", "name"),
        _mapping_value(raw, "event", "metadata", "title"),
        _mapping_value(raw, "event", "name"),
        _mapping_value(raw, "event", "title"),
        raw.get("eventTitle"),
    )
    market_title = _first_string(
        _mapping_value(raw, "market", "metadata", "name"),
        _mapping_value(raw, "market", "metadata", "title"),
        _mapping_value(raw, "market", "name"),
        _mapping_value(raw, "market", "title"),
        raw.get("marketTitle"),
    )
    if event_title:
        parts.append(f"Event: {_bold(event_title)}")
    if market_title:
        parts.append(f"Market: {_bold(market_title)}")

    parts.extend([
        f"Status: {_safe_html(order.status or 'n/a')}",
        f"Side: {side_emoji + ' ' if side_emoji else ''}{_safe_html(order.side or 'n/a')}",
        f"Type: {_safe_html(order.order_type or raw.get('type') or 'n/a')}",
        f"Outcome: {_safe_html(_first_string(raw.get('outcome'), raw.get('outcomeId'), raw.get('outcome_id'), raw.get('outcomeIndex')) or 'n/a')}",
        f"Amount: {_code(_format_number(raw.get('amount') or order.quantity))}",
        f"Price: {_code(_format_number(order.limit_price or raw.get('price')))}",
        f"Filled: {_code(_format_number(order.filled_quantity or raw.get('filled')))}",
        f"Avg fill: {_code(_format_number(order.average_fill_price))}",
    ])
    if order.created_at or raw.get('createdAt'):
        parts.append(f"Created: {_code(order.created_at or raw.get('createdAt'))}")
    if order.updated_at or raw.get('updatedAt'):
        parts.append(f"Updated: {_code(order.updated_at or raw.get('updatedAt'))}")
    parts.append(GENERAL_QUANT_GUIDANCE.capitalize())
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


def _watchlist_text(events: list[dict[str, Any]]) -> str:
    lines = ["<b>What do you want to watch?</b>"]
    for index, event in enumerate(events, start=1):
        emoji = _signal_emoji(_event_direction(event))
        prefix = f"{emoji} " if emoji else ""
        title = _event_title(event)
        description = _event_description(event)
        line = f"{index}. {prefix}<b>{_safe_html(title)}</b>"
        if description:
            line += f" — {_safe_html(description)}"
        lines.append(line)
    return "\n".join(lines)


def _watchlist_details(event: dict[str, Any]) -> str:
    lines = [f"Watching: {_bold(_event_title(event))}"]
    description = _event_description(event)
    if description:
        lines.append(f"Details: {_safe_html(description)}")
    direction = _event_direction(event)
    if direction:
        lines.append(f"Signal: {_signal_emoji(direction)} {_safe_html(direction)}".strip())
    return "\n".join(lines)


def _should_suppress_debug_message(text: Any) -> bool:
    normalized = _normalize_text(text).lower()
    return any(phrase in normalized for phrase in DEBUG_SPAM_PHRASES)


def _watch_category_from_text(text: str) -> Optional[str]:
    normalized = _normalize_text(text).lower()
    if not normalized.startswith("watch"):
        return None
    args = _split_args(text)
    if len(args) >= 2 and args[0].lower() == "watch":
        category = " ".join(args[1:]).strip(".,:;!?")
        return category or None
    return None


def _general_plain_text_response(text: str) -> CommandResult:
    normalized = _normalize_text(text).lower()
    if any(phrase in normalized for phrase in ("buy", "sell", "should i", "am i", "entry", "exit", "long", "short")):
        return CommandResult(True, "\n".join([
            "If you want buy/sell guidance, give me a market name or symbol.",
            GENERAL_QUANT_GUIDANCE,
            "I can also show a quote, watch a category, or list your portfolio.",
        ]))
    if normalized:
        return CommandResult(True, "\n".join([
            f"I read: {_safe_html(text)}",
            "I can help with quotes, watchlists, balances, orders, and portfolio checks.",
            "Try: watch crypto, quote BTC, /help",
        ]))
    return CommandResult(True, "I can help with quotes, watchlists, balances, orders, and portfolio checks. Try /help.")


def build_quote_command(client: BayseClient, text: str) -> CommandResult:
    args = _split_args(text)
    if not args:
        return CommandResult(False, "Usage: /quote <market name or symbol>")
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
        return CommandResult(False, "Usage: /order <event name> <market name> <outcome> <buy|sell> <amount> <currency> [price] [LIMIT|MARKET]")

    event_id = args[0]
    market_id = args[1]
    outcome = args[2]
    side = args[3]

    try:
        amount = float(args[4])
    except ValueError:
        return CommandResult(False, "Usage: /order <event name> <market name> <outcome> <buy|sell> <amount> <currency> [price] [LIMIT|MARKET]")

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
                return CommandResult(False, "Usage: /order <event name> <market name> <outcome> <buy|sell> <amount> <currency> [price] [LIMIT|MARKET]")
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
        category = _watch_category_from_text(text)
        payload = client.list_events(page=1, size=WATCHLIST_PAGE_SIZE, params={"category": category} if category else None)
        events = _extract_collection(payload)
        if not events:
            return CommandResult(False, "No markets or events were returned by Bayse.")
        raw = {"events": events, "payload": payload, "category": category}
        heading = _watchlist_text(events)
        if category:
            heading = "\n".join([f"<b>Watching category:</b> {_safe_html(category)}", heading])
        return CommandResult(True, heading, raw=raw)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


def build_balance_command(client: BayseClient, text: str = "") -> CommandResult:
    try:
        payload = client.get_portfolio()
        return CommandResult(True, _portfolio_text(payload, "Wallet balance"), raw=payload)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


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
    lines = [f"<b>{_safe_html(heading)}</b>"]

    balance = _first_string(
        data.get("balance"),
        data.get("walletBalance"),
        data.get("availableBalance"),
        data.get("cashBalance"),
        data.get("cash"),
        default="n/a",
    )
    lines.append(f"Wallet balance: {_code(balance)}")

    if positions:
        lines.append("Open positions:")
        for idx, position in enumerate(positions, start=1):
            title = _first_string(
                _mapping_value(position, "metadata", "name"),
                _mapping_value(position, "metadata", "title"),
                position.get("title"),
                position.get("name"),
                position.get("marketName"),
                position.get("market_name"),
                position.get("symbol"),
                default=f"Position {idx}",
            )
            size = _first_string(position.get("quantity"), position.get("size"), position.get("amount"), position.get("exposure"), default="n/a")
            direction = _signal_emoji(position.get("direction") or position.get("side") or position.get("sentiment"))
            prefix = f"{direction} " if direction else ""
            lines.append(f"{idx}. {prefix}<b>{_safe_html(title)}</b> — {_code(size)}")
    else:
        lines.append("Open positions: none")

    return "\n".join(lines)


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
        "\n".join([
            "<b>Medes Et Bayse commands</b>",
            "/quote <code>market name or symbol</code> - Get a market quote",
            "/order <code>event name</code> <code>market name</code> <code>outcome</code> <code>buy|sell</code> <code>amount</code> <code>currency</code> [price] [LIMIT|MARKET] - Place a trade order",
            "/balance - Check your wallet balance",
            "/portfolio - View open positions",
            "/events - List active markets",
            "/help - Show bot usage info",
            GENERAL_QUANT_GUIDANCE.capitalize(),
        ]),
    )


def format_signal_message(direction: Any, title: str, details: Optional[str] = None) -> str:
    emoji = _signal_emoji(direction)
    header = f"{emoji} {title}" if emoji else title
    parts = [header]
    if details:
        parts.append(details)
    return "\n".join(parts)


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


def _looks_like_watch_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("watch", "watchlist", "monitor", "track", "follow", "list markets", "active markets", "market events", "economy trades", "trades"))


def _looks_like_balance_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("balance", "wallet", "cash", "funds"))


def _looks_like_portfolio_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("portfolio", "positions", "holdings", "open positions"))


def _looks_like_help_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("help", "how do i use", "usage", "commands"))


def _looks_like_quote_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("quote", "price", "ticker"))


def build_natural_language_command(client: BayseClient, text: str) -> CommandResult:
    if not _normalize_text(text):
        return _general_plain_text_response(text)
    if _looks_like_watch_intent(text):
        return build_watchlist_command(client, text)
    if _looks_like_balance_intent(text):
        return build_balance_command(client)
    if _looks_like_portfolio_intent(text):
        return build_portfolio_command(client)
    if _looks_like_help_intent(text):
        return build_help_command()
    if _looks_like_quote_intent(text):
        args = _split_args(text)
        if args and args[0].startswith('/'):
            args = args[1:]
        symbol = next((token for token in args if token.lower() not in {'quote', 'price', 'ticker', 'of', 'for', 'the', 'a'}), None)
        if symbol:
            return build_quote_command(client, f"/quote {symbol}")
        return CommandResult(False, "Say a market name or symbol after quote, for example: quote BTC")
    return _general_plain_text_response(text)


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


def natural_language_handler_factory(client: BayseClient) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        text = getattr(message, "text", "") or ""
        print(json.dumps({"telegram": "incoming_text", "text": text}, ensure_ascii=False), flush=True)
        result = build_natural_language_command(client, text)
        if not result.ok:
            print(json.dumps({"telegram": "text_error", "text": text, "response": result.text}, ensure_ascii=False), flush=True)
            await message.reply_text(result.text, parse_mode="HTML")
            return
        if result.raw and isinstance(result.raw, dict) and result.raw.get("events"):
            events = result.raw.get("events", [])
            print(json.dumps({"telegram": "text_routed", "route": "watchlist", "text": text}, ensure_ascii=False), flush=True)
            await message.reply_text(result.text, reply_markup=_watchlist_keyboard(events), parse_mode="HTML")
            return
        print(json.dumps({"telegram": "text_routed", "route": "general", "text": text}, ensure_ascii=False), flush=True)
        await message.reply_text(result.text, parse_mode="HTML")

    return handler


def quote_handler_factory(client: BayseClient) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        text = getattr(message, "text", "") or ""
        result = build_quote_command(client, text)
        if _should_suppress_debug_message(result.text):
            return
        await message.reply_text(result.text, parse_mode="HTML")

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
        await message.reply_text(result.text, parse_mode="HTML")
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
            await message.reply_text(result.text, parse_mode="HTML")
            return
        events = result.raw.get("events", []) if result.raw else []
        await message.reply_text(result.text, reply_markup=_watchlist_keyboard(events), parse_mode="HTML")

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
                await query.edit_message_text(result.text, parse_mode="HTML")
                return
            events = result.raw.get("events", []) if result.raw else []
            await query.edit_message_text(result.text, reply_markup=_watchlist_keyboard(events), parse_mode="HTML")
            return

        try:
            payload = client.get_event(selected)
            events = _extract_collection(payload)
            event = events[0] if events else payload if isinstance(payload, dict) else {}
        except BayseClientError as exc:
            await query.edit_message_text(_error_text(exc), parse_mode="HTML")
            return
        except Exception as exc:
            await query.edit_message_text(f"Bayse API error\nmessage: {exc}")
            return

        if not isinstance(event, dict):
            event = {}

        context.user_data["watchlist_event_id"] = selected
        context.user_data["watchlist_event"] = event

        details = _watchlist_details(event)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Refresh list", callback_data="watch:refresh")]])
        await query.edit_message_text(details, reply_markup=keyboard, parse_mode="HTML")
        direction = _event_direction(event)
        if direction:
            await send_scenario_sticker(query.message, direction)

    return handler
