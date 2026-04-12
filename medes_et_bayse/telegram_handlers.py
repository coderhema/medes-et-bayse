from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
import re
from html import escape as html_escape
from typing import Any, Callable, Iterable, Optional
from urllib import error, request

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
logger = logging.getLogger(__name__)


def _should_suppress_debug_message(text: Any) -> bool:
    normalized = _normalize_text(text).lower()
    return any(phrase in normalized for phrase in DEBUG_SPAM_PHRASES)


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


KNOWN_EVENT_CATEGORIES = {"sports", "crypto", "politics", "economy", "entertainment", "culture", "technology", "business", "music", "world"}
QUOTE_STOP_WORDS = {"quote", "price", "prices", "ticker", "market", "markets", "the", "a", "an", "for", "of", "on", "about"}
WATCH_STOP_WORDS = {"watch", "watchlist", "events", "event", "markets", "market", "monitor", "track", "follow", "list", "show", "active", "my", "me"}


def _is_uuid_like(value: Any) -> bool:
    text = _normalize_text(value)
    parts = text.split("-")
    return len(text) == 36 and len(parts) == 5


def _truncate_text(value: Any, limit: int = 44) -> str:
    text = _normalize_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _search_term_after(text: str, stop_words: set[str]) -> str:
    tokens = _split_args(text)
    if tokens and tokens[0].startswith("/"):
        tokens = tokens[1:]
    filtered = [token for token in tokens if token.lower() not in stop_words]
    return " ".join(filtered).strip(" .,:;!?")


def _event_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    markets = event.get("markets")
    if isinstance(markets, list):
        return [market for market in markets if isinstance(market, dict)]
    if any(key in event for key in ("outcome1Price", "outcome2Price", "yesBuyPrice", "noBuyPrice")):
        return [event]
    return []


def _event_currency_label(event: dict[str, Any]) -> str:
    currencies = event.get("supportedCurrencies")
    if isinstance(currencies, list) and currencies:
        cleaned = [str(item).upper() for item in currencies if _normalize_text(item)]
        if cleaned:
            return ", ".join(cleaned)
    return _first_string(event.get("currency"), default="USD").upper()


def _market_yes_no_prices(market: dict[str, Any]) -> tuple[str, str]:
    yes_price = _first_string(
        _format_number(market.get("yesBuyPrice")),
        _format_number(market.get("outcome1Price")),
        _format_number(market.get("price")),
        default="n/a",
    )
    no_price = _first_string(
        _format_number(market.get("noBuyPrice")),
        _format_number(market.get("outcome2Price")),
        default="n/a",
    )
    return yes_price, no_price


def _market_summary_line(market: dict[str, Any], *, prefix: str = "") -> str:
    yes_price, no_price = _market_yes_no_prices(market)
    title = _market_title(market)
    line = f"{prefix}<b>{_safe_html(title)}</b>" if title else f"{prefix}<b>Market</b>"
    line += f" — YES {_code(yes_price)} | NO {_code(no_price)}"
    status = _first_string(market.get("status"), default="").lower()
    if status:
        line += f" | {_safe_html(status)}"
    return line


def _event_list_title(event: dict[str, Any]) -> str:
    title = _event_title(event)
    if not title or _is_uuid_like(title):
        return "Untitled event"
    return title


def _event_summary_line(event: dict[str, Any], index: int) -> str:
    emoji = _signal_emoji(_event_direction(event))
    prefix = f"{emoji} " if emoji else ""
    return f"{index}. {prefix}<b>{_safe_html(_event_list_title(event))}</b>"


def _event_details_text(event: dict[str, Any], *, heading: str = "Selected event") -> str:
    lines = [f"<b>{_safe_html(heading)}</b>: {_bold(_event_title(event))}"]
    description = _event_description(event)
    if description:
        lines.append(f"Details: {_safe_html(description)}")
    category = _first_string(event.get("category"), default="")
    if category:
        lines.append(f"Category: {_safe_html(category)}")
    status = _first_string(event.get("status"), default="")
    if status:
        lines.append(f"Status: {_safe_html(status)}")
    currencies = _event_currency_label(event)
    if currencies:
        lines.append(f"Currencies: {_safe_html(currencies)}")
    markets = _event_markets(event)
    if markets:
        lines.append("Markets:")
        for idx, market in enumerate(markets, start=1):
            lines.append(f"{idx}. {_market_summary_line(market)}")
    else:
        lines.append("Markets: no market data returned by Bayse.")
    return chr(10).join(lines)


def _watchlist_text(events: list[dict[str, Any]]) -> str:
    lines = ["<b>What do you want to watch?</b>"]
    for index, event in enumerate(events, start=1):
        lines.append(_event_summary_line(event, index))
    return chr(10).join(lines)


def _events_text(events: list[dict[str, Any]], *, heading: str = "Active markets") -> str:
    lines = [f"<b>{_safe_html(heading)}</b>"]
    for index, event in enumerate(events, start=1):
        lines.append(_event_summary_line(event, index))
    return chr(10).join(lines)


def _fund_text(asset: Optional[str]) -> str:
    asset_label = (asset or "USD").upper()
    return chr(10).join([
        "<b>Funding options</b>",
        f"Selected currency: {_safe_html(asset_label)}",
        "Bayse docs show deposits are handled through mint shares on a market, while wallet assets show what is active for your account.",
        "Use /fund USD or /fund NGN to see the matching wallet funding details.",
        "Security: verify the asset, network, and destination before moving funds.",
        "Supported currencies: USD and NGN.",
    ])


def _withdraw_text(asset: Optional[str]) -> str:
    asset_label = (asset or "USD").upper()
    return chr(10).join([
        "<b>Withdrawal options</b>",
        f"Selected currency: {_safe_html(asset_label)}",
        "Bayse docs show withdrawals are handled through burning equal YES and NO shares on a market, plus wallet balances determine what you can move.",
        "Use /withdraw USD or /withdraw NGN to see the matching wallet withdrawal details.",
        "Security: only withdraw to details you control and recognize.",
        "Supported currencies: USD and NGN.",
    ])


def _funding_asset_from_text(text: str) -> Optional[str]:
    normalized = _normalize_text(text).lower()
    if any(keyword in normalized for keyword in ("ngn", "naira", "cash", "local currency", "wallet")):
        return "NGN"
    if any(keyword in normalized for keyword in ("usd", "usdt", "crypto", "stablecoin", "bep20", "crypto wallet")):
        return "USD"
    return None


def _withdraw_asset_from_text(text: str) -> Optional[str]:
    normalized = _normalize_text(text).lower()
    if any(keyword in normalized for keyword in ("ngn", "naira", "bank", "cash")):
        return "NGN"
    if any(keyword in normalized for keyword in ("usd", "usdt", "crypto", "wallet", "stablecoin")):
        return "USD"
    return None


def _asset_row_text(asset: dict[str, Any]) -> str:
    symbol = _first_string(asset.get("symbol"), default="n/a")
    balance = _code(_format_number(asset.get("availableBalance")))
    pending = _code(_format_number(asset.get("pendingBalance")))
    deposit = _safe_html(asset.get("depositActivity") or "n/a")
    withdraw = _safe_html(asset.get("withdrawalActivity") or "n/a")
    network = _safe_html(asset.get("network") or "n/a")
    lines = [f"<b>{_safe_html(symbol)}</b> — balance {balance} pending {pending}"]
    lines.append(f"Network: {network} | deposit: {deposit} | withdrawal: {withdraw}")
    addresses = asset.get("addresses") if isinstance(asset.get("addresses"), list) else []
    if addresses:
        for address in addresses:
            addr = _first_string(address.get("address"), default="")
            if addr:
                lines.append(f"Deposit address: <code>{_safe_html(addr)}</code>")
    return chr(10).join(lines)


def _wallet_assets_text(payload: Any, *, asset_filter: Optional[str] = None, purpose: str = "Funding") -> str:
    assets: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        maybe_assets = payload.get("assets")
        if isinstance(maybe_assets, list):
            assets = [item for item in maybe_assets if isinstance(item, dict)]
    elif isinstance(payload, list):
        assets = [item for item in payload if isinstance(item, dict)]

    if asset_filter:
        filtered = []
        for asset in assets:
            symbol = _first_string(asset.get("symbol")).upper()
            if symbol == asset_filter.upper():
                filtered.append(asset)
        assets = filtered or assets

    lines = [f"<b>{_safe_html(purpose)}</b>"]
    if not assets:
        lines.append("No wallet assets were returned by Bayse.")
        return chr(10).join(lines)

    for asset in assets:
        lines.append(_asset_row_text(asset))
        lines.append("")

    if purpose.lower().startswith("fund"):
        lines.extend([
            "Security: verify the deposit address, asset symbol, and network before sending funds.",
            "Bayse docs show supported currencies include USD and NGN.",
        ])
    else:
        lines.extend([
            "Security: withdrawals should only be sent to addresses or bank details you control and recognize.",
            "If withdrawal activity is SUSPENDED, check KYC and account status in the app.",
        ])
    return chr(10).join(lines).strip()


SMART_TRADE_MIN_PATTERN = re.compile(r"\b(buy|sell|long|short)\b", re.IGNORECASE)
SMART_TRADE_SIDE_PATTERN = re.compile(r"\b(buy|sell|long|short)\b", re.IGNORECASE)
SMART_TRADE_OUTCOME_PATTERN = re.compile(r"\b(yes|no)\b", re.IGNORECASE)
SMART_TRADE_AMOUNT_PATTERN = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)(?!\w)")
SMART_TRADE_CURRENCY_PATTERN = re.compile(r"\b(NGN|USD)\b", re.IGNORECASE)
DETAIL_PREVIEW_LINE_LIMIT = 7


def _detail_view_bucket(context: Any) -> dict[str, dict[str, Any]]:
    if context is None:
        return {}
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return {}
    bucket = data.get("detail_views")
    if not isinstance(bucket, dict):
        bucket = {}
        data["detail_views"] = bucket
    return bucket


def _detail_view_key(prefix: str, identifier: str) -> str:
    base = f"{prefix}:{identifier}".strip(":")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def _detail_preview_text(full_text: str, *, limit_lines: int = DETAIL_PREVIEW_LINE_LIMIT) -> str:
    lines = (full_text or "").split(chr(10))
    if len(lines) <= limit_lines:
        return full_text
    preview = lines[:limit_lines]
    preview.append("Tap View more for the full details.")
    return chr(10).join(preview)


def _detail_keyboard(
    view_key: str,
    *,
    back_callback: Optional[str] = None,
    view_more: bool = True,
    back_label: str = "Back",
    extra_rows: Optional[list[list[Any]]] = None,
) -> Any:
    rows = []
    if view_more:
        rows.append([InlineKeyboardButton("View more", callback_data=f"more:{view_key}")])
    if extra_rows:
        rows.extend(extra_rows)
    if back_callback:
        rows.append([InlineKeyboardButton(back_label, callback_data=back_callback)])
    return InlineKeyboardMarkup(rows) if rows else None


def _prepare_detail_view(
    context: Any,
    *,
    prefix: str,
    identifier: str,
    full_text: str,
    back_callback: Optional[str] = None,
    back_label: str = "Back",
    extra_rows: Optional[list[list[Any]]] = None,
) -> tuple[str, Any]:
    key = _detail_view_key(prefix, identifier)
    bucket = _detail_view_bucket(context)
    bucket[key] = {"text": full_text, "back_callback": back_callback, "back_label": back_label}
    preview = _detail_preview_text(full_text)
    if preview != full_text:
        return preview, _detail_keyboard(key, back_callback=back_callback, view_more=True, back_label=back_label, extra_rows=extra_rows)
    return full_text, _detail_keyboard(key, back_callback=back_callback, view_more=False, back_label=back_label, extra_rows=extra_rows)


def _smart_trade_currency(candidate: dict[str, Any]) -> str:
    currency = _first_string(candidate.get("currency"), default="USD").upper()
    if "," in currency:
        currency = currency.split(",", 1)[0].strip() or "USD"
    return currency


def _active_trade_order_state(context: Any) -> Optional[dict[str, Any]]:
    if context is None:
        return None
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return None
    state = data.get("trade_order_state")
    return state if isinstance(state, dict) else None


def _set_trade_order_state(context: Any, candidate: dict[str, Any], **fields: Any) -> None:
    if context is None:
        return
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return
    state = data.get("trade_order_state")
    if not isinstance(state, dict):
        state = {}
    event_id = _first_string(
        candidate.get("event_id"),
        candidate.get("eventId"),
        candidate.get("eventid"),
        state.get("event_id"),
        state.get("eventId"),
        state.get("eventid"),
        default="",
    )
    market_id = _first_string(
        candidate.get("market_id"),
        candidate.get("marketId"),
        candidate.get("marketid"),
        state.get("market_id"),
        state.get("marketId"),
        state.get("marketid"),
        default="",
    )
    state.update({
        "candidate": candidate,
        "event": candidate.get("event") if isinstance(candidate.get("event"), dict) else {},
        "market": candidate.get("market") if isinstance(candidate.get("market"), dict) else {},
        "event_id": event_id,
        "eventId": event_id,
        "eventid": event_id,
        "market_id": market_id,
        "marketId": market_id,
        "marketid": market_id,
        "event_title": _first_string(candidate.get("event_title"), default=""),
        "market_title": _first_string(candidate.get("market_title"), default=""),
        "outcome_id": _first_string(fields.get("outcome_id") or state.get("outcome_id"), default=""),
        "outcome_label": _first_string(fields.get("outcome_label") or state.get("outcome_label"), default=""),
        "side": _normalize_text(fields.get("side") or state.get("side")).lower(),
        "currency": _normalize_text(fields.get("currency") or state.get("currency")).upper(),
        "amount": fields.get("amount") if fields.get("amount") is not None else state.get("amount"),
        "stage": _normalize_text(fields.get("stage") or state.get("stage")).lower(),
    })
    data["trade_order_state"] = state


def _clear_trade_order_state(context: Any) -> None:
    if context is None:
        return
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return
    data.pop("trade_order_state", None)


def _trade_currency_keyboard() -> Any:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("NGN", callback_data="tradec:NGN"), InlineKeyboardButton("USD", callback_data="tradec:USD")],
    ])


def _trade_currency_prompt_text(candidate: dict[str, Any]) -> str:
    return chr(10).join([
        f"Active market: {_safe_html(candidate.get('event_title') or '')} · {_safe_html(candidate.get('market_title') or '')}",
        "Choose a currency to continue.",
    ])


def _trade_view_bucket(context: Any) -> dict[str, dict[str, Any]]:
    if context is None:
        return {}
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return {}
    bucket = data.get("trade_views")
    if not isinstance(bucket, dict):
        bucket = {}
        data["trade_views"] = bucket
    return bucket


def _trade_view_key(candidate: dict[str, Any]) -> str:
    event_id = _first_string(candidate.get("event_id"), candidate.get("eventId"), candidate.get("eventid"), default="")
    market_id = _first_string(candidate.get("market_id"), candidate.get("marketId"), candidate.get("marketid"), default="")
    base = f"{event_id}:{market_id}".strip(":") or _first_string(candidate.get("event_title"), candidate.get("market_title"), default="trade")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def _active_trade_selection(context: Any) -> Optional[dict[str, Any]]:
    if context is None:
        return None
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return None
    selection = data.get("trade_selection")
    return selection if isinstance(selection, dict) else None


def _set_trade_selection(
    context: Any,
    candidate: dict[str, Any],
    *,
    outcome_id: Optional[str] = None,
    outcome_label: Optional[str] = None,
    side: Optional[str] = None,
) -> None:
    if context is None:
        return
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return
    event_id = _first_string(candidate.get("event_id"), candidate.get("eventId"), candidate.get("eventid"), default="")
    market_id = _first_string(candidate.get("market_id"), candidate.get("marketId"), candidate.get("marketid"), default="")
    data["trade_selection"] = {
        "candidate": candidate,
        "event": candidate.get("event") if isinstance(candidate.get("event"), dict) else {},
        "market": candidate.get("market") if isinstance(candidate.get("market"), dict) else {},
        "event_id": event_id,
        "eventId": event_id,
        "eventid": event_id,
        "market_id": market_id,
        "marketId": market_id,
        "marketid": market_id,
        "outcome_id": _first_string(outcome_id, default=""),
        "outcome_label": _first_string(outcome_label, default=""),
        "side": _normalize_text(side).lower(),
    }


def _clear_trade_selection(context: Any) -> None:
    if context is None:
        return
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return
    data.pop("trade_selection", None)


def _trade_outcomes(market: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    raw_outcomes = market.get("outcomes")
    if isinstance(raw_outcomes, list):
        for index, outcome in enumerate(raw_outcomes):
            if not isinstance(outcome, dict):
                continue
            label = _first_string(
                outcome.get("name"),
                outcome.get("label"),
                outcome.get("title"),
                outcome.get("side"),
                outcome.get("description"),
                default=f"Outcome {index + 1}",
            )
            outcomes.append({
                "label": label or f"Outcome {index + 1}",
                "outcome_id": _first_string(outcome.get("id"), outcome.get("outcomeId"), outcome.get("outcome_id"), default=""),
                "raw": outcome,
            })
        if outcomes:
            return outcomes

    yes_id = _first_string(market.get("outcome1Id"), market.get("yesOutcomeId"), default="")
    no_id = _first_string(market.get("outcome2Id"), market.get("noOutcomeId"), default="")
    yes_label = _first_string(market.get("outcome1"), market.get("outcome1Name"), market.get("yesOutcome"), market.get("yesLabel"), default="Yes")
    no_label = _first_string(market.get("outcome2"), market.get("outcome2Name"), market.get("noOutcome"), market.get("noLabel"), default="No")
    if yes_id or no_id or any(key in market for key in ("yesBuyPrice", "noBuyPrice", "outcome1Price", "outcome2Price")):
        return [
            {"label": yes_label or "Yes", "outcome_id": yes_id, "raw": {"name": yes_label or "Yes"}},
            {"label": no_label or "No", "outcome_id": no_id, "raw": {"name": no_label or "No"}},
        ]

    return []


def _trade_outcome_aliases(outcome: dict[str, Any]) -> set[str]:
    aliases = {
        _normalize_text(outcome.get("label")).lower(),
        _normalize_text(outcome.get("outcome_id")).lower(),
    }
    raw = outcome.get("raw") if isinstance(outcome.get("raw"), dict) else {}
    aliases.update({
        _normalize_text(raw.get("name")).lower(),
        _normalize_text(raw.get("label")).lower(),
        _normalize_text(raw.get("title")).lower(),
        _normalize_text(raw.get("side")).lower(),
        _normalize_text(raw.get("outcome")).lower(),
    })
    return {alias for alias in aliases if alias}


def _resolve_order_outcome_id(
    candidate: dict[str, Any],
    *,
    outcome_text: str = "",
    side: str = "",
    selected_trade: Optional[dict[str, Any]] = None,
) -> str:
    market = candidate.get("market") if isinstance(candidate.get("market"), dict) else {}
    if selected_trade:
        selected_id = _first_string(selected_trade.get("outcome_id"), default="")
        if selected_id:
            return selected_id

    normalized_outcome = _normalize_text(outcome_text).lower()
    normalized_side = _normalize_text(side).lower()
    outcomes = _trade_outcomes(market)
    if normalized_outcome and outcomes:
        for outcome in outcomes:
            if normalized_outcome in _trade_outcome_aliases(outcome):
                return _first_string(outcome.get("outcome_id"), default="")

    if outcomes:
        if normalized_side == "buy":
            return _first_string(outcomes[0].get("outcome_id"), default="")
        if normalized_side == "sell":
            if len(outcomes) > 1:
                return _first_string(outcomes[1].get("outcome_id"), default="")
            return _first_string(outcomes[0].get("outcome_id"), default="")

    return _first_string(
        market.get("outcome1Id"),
        market.get("outcome2Id"),
        market.get("outcomeId"),
        market.get("selectedOutcomeId"),
        default="",
    )


def _brain_quant_prediction(candidate: dict[str, Any]) -> str:
    prompt = {
        "task": "compose_quant_prediction",
        "instruction": "Write a short market-aware prediction blurb for the user. Keep it concise, practical, and grounded in the active market context. Avoid certainty unless the context is clear.",
        "active_context": {
            "event_title": candidate.get("event_title"),
            "market_title": candidate.get("market_title"),
            "event_id": candidate.get("event_id"),
            "market_id": candidate.get("market_id"),
            "currency": candidate.get("currency"),
            "yes_price": candidate.get("yes_price"),
            "no_price": candidate.get("no_price"),
        },
        "expected_shape": {
            "blurb": "A short actionable prediction sentence or two",
        },
    }

    brain_url = os.getenv("POKE_BRAIN_URL", "").strip() or os.getenv("POKE_API_BRAIN_URL", "").strip()
    poke_api_key = os.getenv("POKE_API_KEY", "").strip()
    if brain_url:
        try:
            body = json.dumps(prompt).encode("utf-8")
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if poke_api_key:
                headers["Authorization"] = f"Bearer {poke_api_key}"
            req = request.Request(brain_url, data=body, headers=headers, method="POST")
            with request.urlopen(req, timeout=12) as resp:
                payload = resp.read().decode("utf-8")
            if payload:
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    for key in ("blurb", "text", "message", "result"):
                        value = parsed.get(key)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
                        if isinstance(value, dict):
                            nested = _first_string(value.get("blurb"), value.get("text"), value.get("message"), default="")
                            if nested:
                                return nested
        except Exception:
            pass

    yes_price = candidate.get("yes_price")
    no_price = candidate.get("no_price")
    try:
        yes_num = float(yes_price)
    except (TypeError, ValueError):
        yes_num = None
    try:
        no_num = float(no_price)
    except (TypeError, ValueError):
        no_num = None

    if yes_num is not None and no_num is not None:
        if yes_num >= 0.7:
            return "The market is leaning hard toward YES, so I’d treat fresh buys as crowded and prefer patience or a lighter contrarian entry."
        if yes_num <= 0.3:
            return "The market is leaning hard toward NO, so the cleaner edge is usually on the opposite side if the thesis still holds."
        return "The market looks balanced, which usually means smaller size, cleaner confirmation, and no rushed entry."

    return "The market context looks incomplete, so wait for cleaner confirmation before taking size."


def _trade_selection_text(candidate: dict[str, Any], *, selected_outcome_label: Optional[str] = None, selected_side: Optional[str] = None) -> str:
    lines = [
        _event_details_text(candidate.get("event") if isinstance(candidate.get("event"), dict) else {}, heading="Selected event"),
        f"<b>Active market</b>: {_bold(candidate.get('market_title') or '')}",
        f"<b>Poke's Quant Prediction</b>",
        _safe_html(_brain_quant_prediction(candidate)),
    ]
    if selected_outcome_label:
        lines.append(f"Selected outcome: {_safe_html(selected_outcome_label)}")
    if selected_side:
        lines.append(f"Selected side: {_safe_html(selected_side.upper())}")
    lines.append("Pick an outcome below, then choose Buy or Sell to continue.")
    return chr(10).join(lines)


def _trade_keyboard_rows(candidate: dict[str, Any], *, view_key: str, selected_outcome_id: Optional[str] = None, selected_side: Optional[str] = None) -> list[list[Any]]:
    market = candidate.get("market") if isinstance(candidate.get("market"), dict) else {}
    outcomes = _trade_outcomes(market)
    rows: list[list[Any]] = []
    for index, outcome in enumerate(outcomes):
        label = _truncate_text(outcome.get("label") or f"Outcome {index + 1}", 18)
        rows.append([InlineKeyboardButton(label, callback_data=f"tradeo:{view_key}:{index}")])

    if selected_outcome_id or selected_side:
        rows.append([
            InlineKeyboardButton("Buy", callback_data=f"trades:{view_key}:buy"),
            InlineKeyboardButton("Sell", callback_data=f"trades:{view_key}:sell"),
        ])
    return rows


def _trade_keyboard(context: Any, candidate: dict[str, Any], *, selected_outcome_id: Optional[str] = None, selected_side: Optional[str] = None, back_callback: Optional[str] = "watch:refresh") -> Any:
    view_key = _trade_view_key(candidate)
    bucket = _trade_view_bucket(context)
    bucket[view_key] = {"candidate": candidate}
    rows = _trade_keyboard_rows(candidate, view_key=view_key, selected_outcome_id=selected_outcome_id, selected_side=selected_side)
    if back_callback:
        rows.append([InlineKeyboardButton("Refresh list", callback_data=back_callback)])
    return InlineKeyboardMarkup(rows) if rows else None


def _brain_parse_trade_intent(text: str, candidate: dict[str, Any]) -> dict[str, Any]:
    prompt = {
        "task": "parse_short_trade_reply",
        "instruction": "Parse a short Bayse trade reply into side, amount, currency, outcome, and whether the currency should be normalized to the market currency. Use the active market context and active_market_id to infer the trade target when the reply is short.",
        "text": text,
        "active_context": {
            "event_title": candidate.get("event_title"),
            "market_title": candidate.get("market_title"),
            "event_id": candidate.get("event_id"),
            "eventId": candidate.get("eventId"),
            "eventid": candidate.get("eventid"),
            "active_market_id": candidate.get("market_id"),
            "market_id": candidate.get("market_id"),
            "marketId": candidate.get("marketId"),
            "marketid": candidate.get("marketid"),
            "supported_currency": candidate.get("currency"),
        },
        "expected_shape": {
            "side": "buy|sell",
            "amount": 700,
            "currency": "NGN|USD",
            "outcome": "YES|NO",
            "normalized_currency": "Bayse market currency",
            "notes": "optional",
        },
    }

    local = _local_parse_trade_intent(text, candidate)
    brain_url = os.getenv("POKE_BRAIN_URL", "").strip() or os.getenv("POKE_API_BRAIN_URL", "").strip()
    poke_api_key = os.getenv("POKE_API_KEY", "").strip()
    if brain_url:
        try:
            body = json.dumps(prompt).encode("utf-8")
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if poke_api_key:
                headers["Authorization"] = f"Bearer {poke_api_key}"
            req = request.Request(brain_url, data=body, headers=headers, method="POST")
            with request.urlopen(req, timeout=12) as resp:
                payload = resp.read().decode("utf-8")
            if payload:
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    result = parsed.get("result") if isinstance(parsed.get("result"), dict) else parsed
                    if isinstance(result, dict):
                        merged = dict(local)
                        for key, value in result.items():
                            if value is None:
                                continue
                            if isinstance(value, str) and not value.strip():
                                continue
                            merged[key] = value
                        return merged if merged else local
        except Exception:
            pass

    return local


def _local_parse_trade_intent(text: str, candidate: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_text(text).lower()
    side_match = SMART_TRADE_SIDE_PATTERN.search(normalized)
    amount_match = SMART_TRADE_AMOUNT_PATTERN.search(normalized)
    currency_match = SMART_TRADE_CURRENCY_PATTERN.search(normalized)
    outcome_match = SMART_TRADE_OUTCOME_PATTERN.search(normalized)

    side = side_match.group(1).lower() if side_match else ""
    if side == "long":
        side = "buy"
    elif side == "short":
        side = "sell"

    amount = float(amount_match.group(1)) if amount_match else None
    currency = currency_match.group(1).upper() if currency_match else _smart_trade_currency(candidate)
    outcome = outcome_match.group(1).upper() if outcome_match else ""
    if not outcome and side in {"buy", "sell"}:
        outcome = "YES" if side == "buy" else "NO"

    parsed: dict[str, Any] = {
        "side": side,
        "currency": currency,
        "outcome": outcome,
        "normalized_currency": _smart_trade_currency(candidate),
    }
    if amount is not None:
        parsed["amount"] = amount
    return parsed


def _looks_like_smart_trade_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return bool(SMART_TRADE_MIN_PATTERN.search(normalized) and SMART_TRADE_AMOUNT_PATTERN.search(normalized))


def _quote_candidate_label(candidate: dict[str, Any]) -> str:
    event_title = _truncate_text(candidate.get("event_title") or candidate.get("eventTitle") or candidate.get("event"), 28)
    market_title = _truncate_text(candidate.get("market_title") or candidate.get("marketTitle") or candidate.get("market"), 24)
    yes_price = _format_number(candidate.get("yes_price"))
    no_price = _format_number(candidate.get("no_price"))
    return _truncate_text(f"{event_title} · {market_title} YES {yes_price} / NO {no_price}", 58)


def _quote_candidates_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for event in events:
        markets = _event_markets(event)
        event_title = _event_title(event)
        for market in markets:
            yes_price, no_price = _market_yes_no_prices(market)
            event_id = _first_string(event.get("id"), event.get("eventId"), event.get("eventid"), default="")
            market_id = _first_string(market.get("id"), market.get("marketId"), market.get("marketid"), default="")
            candidates.append({
                "event": event,
                "market": market,
                "event_title": event_title,
                "market_title": _market_title(market),
                "yes_price": yes_price,
                "no_price": no_price,
                "event_id": event_id,
                "eventId": event_id,
                "eventid": event_id,
                "market_id": market_id,
                "marketId": market_id,
                "marketid": market_id,
                "outcome1_id": _first_string(market.get("outcome1Id"), default=""),
                "outcome2_id": _first_string(market.get("outcome2Id"), default=""),
                "currency": _event_currency_label(event),
                "status": _first_string(market.get("status"), event.get("status"), default=""),
            })
    return candidates


def _candidate_from_event_market(event: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    yes_price, no_price = _market_yes_no_prices(market)
    return {
        "event": event,
        "market": market,
        "event_title": _event_title(event),
        "market_title": _market_title(market),
        "yes_price": yes_price,
        "no_price": no_price,
        "event_id": _first_string(event.get("id"), event.get("eventId"), event.get("eventid"), default=""),
        "eventId": _first_string(event.get("id"), event.get("eventId"), event.get("eventid"), default=""),
        "eventid": _first_string(event.get("id"), event.get("eventId"), event.get("eventid"), default=""),
        "market_id": _first_string(market.get("id"), market.get("marketId"), market.get("marketid"), default=""),
        "marketId": _first_string(market.get("id"), market.get("marketId"), market.get("marketid"), default=""),
        "marketid": _first_string(market.get("id"), market.get("marketId"), market.get("marketid"), default=""),
        "outcome1_id": _first_string(market.get("outcome1Id"), default=""),
        "outcome2_id": _first_string(market.get("outcome2Id"), default=""),
        "currency": _event_currency_label(event),
        "status": _first_string(market.get("status"), event.get("status"), default=""),
    }


def _candidate_from_state(state: Any) -> Optional[dict[str, Any]]:
    if not isinstance(state, dict):
        return None
    candidate = state.get("candidate")
    if isinstance(candidate, dict):
        event_id = _first_string(candidate.get("event_id"), candidate.get("eventId"), candidate.get("eventid"), default="")
        market_id = _first_string(candidate.get("market_id"), candidate.get("marketId"), candidate.get("marketid"), default="")
        if event_id and market_id:
            return candidate
    event_id = _first_string(state.get("event_id"), state.get("eventId"), state.get("eventid"), default="")
    market_id = _first_string(state.get("market_id"), state.get("marketId"), state.get("marketid"), default="")
    if not (event_id and market_id):
        return None
    rebuilt: dict[str, Any] = {
        "event": state.get("event") if isinstance(state.get("event"), dict) else {},
        "market": state.get("market") if isinstance(state.get("market"), dict) else {},
        "event_title": _first_string(state.get("event_title"), state.get("eventTitle"), default=""),
        "market_title": _first_string(state.get("market_title"), state.get("marketTitle"), default=""),
        "yes_price": state.get("yes_price"),
        "no_price": state.get("no_price"),
        "event_id": event_id,
        "eventId": event_id,
        "eventid": event_id,
        "market_id": market_id,
        "marketId": market_id,
        "marketid": market_id,
        "outcome1_id": _first_string(state.get("outcome1_id"), default=""),
        "outcome2_id": _first_string(state.get("outcome2_id"), default=""),
        "currency": _normalize_text(state.get("currency")).upper(),
        "status": _first_string(state.get("status"), default=""),
    }
    outcome_id = _first_string(state.get("outcome_id"), default="")
    if outcome_id:
        rebuilt["outcome_id"] = outcome_id
    outcome_label = _first_string(state.get("outcome_label"), default="")
    if outcome_label:
        rebuilt["outcome_label"] = outcome_label
    side = _normalize_text(state.get("side")).lower()
    if side:
        rebuilt["side"] = side
    return rebuilt


def _active_market_candidate(context: Any) -> Optional[dict[str, Any]]:
    if context is None:
        return None
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return None
    candidate = data.get("active_market_candidate")
    if isinstance(candidate, dict):
        return candidate
    candidate = data.get("active_quote")
    if isinstance(candidate, dict):
        return candidate
    candidate = _candidate_from_state(data.get("trade_order_state"))
    if isinstance(candidate, dict):
        return candidate
    candidate = _candidate_from_state(data.get("trade_selection"))
    if isinstance(candidate, dict):
        return candidate
    event = data.get("active_event")
    market = data.get("active_market")
    if isinstance(event, dict) and isinstance(market, dict):
        return _candidate_from_event_market(event, market)
    return None


def _trade_context_candidate(context: Any) -> Optional[dict[str, Any]]:
    candidate = _active_market_candidate(context)
    if isinstance(candidate, dict) and _first_string(candidate.get("event_id"), candidate.get("market_id"), default=""):
        return candidate
    if context is None:
        return None
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return None
    for key in ("trade_order_state", "trade_selection"):
        candidate = _candidate_from_state(data.get(key))
        if isinstance(candidate, dict) and _first_string(candidate.get("event_id"), candidate.get("market_id"), default=""):
            return candidate
    event = data.get("active_event")
    market = data.get("active_market")
    if isinstance(event, dict) and isinstance(market, dict):
        candidate = _candidate_from_event_market(event, market)
        if _first_string(candidate.get("event_id"), candidate.get("market_id"), default=""):
            return candidate
    return None

def _sync_candidate_ids(candidate: dict[str, Any]) -> None:
    """Ensure event_id/eventId/eventid and market_id/marketId/marketid aliases stay in sync."""
    event_id = _first_string(candidate.get("event_id"), candidate.get("eventId"), candidate.get("eventid"), default="")
    market_id = _first_string(candidate.get("market_id"), candidate.get("marketId"), candidate.get("marketid"), default="")
    if event_id:
        candidate["event_id"] = event_id
        candidate["eventId"] = event_id
        candidate["eventid"] = event_id
    if market_id:
        candidate["market_id"] = market_id
        candidate["marketId"] = market_id
        candidate["marketid"] = market_id


def _set_active_market_context(context: Any, candidate: dict[str, Any]) -> None:
    if context is None:
        return
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return
    _sync_candidate_ids(candidate)
    event = candidate.get("event") if isinstance(candidate.get("event"), dict) else None
    market = candidate.get("market") if isinstance(candidate.get("market"), dict) else None
    if isinstance(event, dict):
        data["active_event"] = event
    if isinstance(market, dict):
        data["active_market"] = market
    data["active_market_candidate"] = candidate


def _quote_keyboard(candidates: list[dict[str, Any]]) -> Any:
    rows = []
    for index, candidate in enumerate(candidates[:10]):
        rows.append([InlineKeyboardButton(_quote_candidate_label(candidate), callback_data=f"quote:{index}")])
    if rows:
        rows.append([InlineKeyboardButton("Refresh search", callback_data="quote:refresh")])
    return InlineKeyboardMarkup(rows) if rows else None


def _watchlist_keyboard(events: list[dict[str, Any]]) -> Any:
    rows = []
    for event in events[:10]:
        rows.append([InlineKeyboardButton(_truncate_text(_event_title(event), 52), callback_data=f"watch:{_first_string(event.get('id'), event.get('eventId'), event.get('eventid'), default='')}")])
    if rows:
        rows.append([InlineKeyboardButton("Refresh list", callback_data="watch:refresh")])
    return InlineKeyboardMarkup(rows) if rows else None


def _asset_keyboard(action: str) -> Any:
    action_key = _normalize_text(action).lower()
    if action_key not in {"fund", "withdraw"}:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("NGN", callback_data=f"{action_key}:NGN"), InlineKeyboardButton("USD", callback_data=f"{action_key}:USD")]
    ])


def _quote_search_text(term: str, candidates: list[dict[str, Any]]) -> str:
    lines = [f"<b>Select a market for</b> {_bold(term)}"]
    for index, candidate in enumerate(candidates[:10], start=1):
        lines.append(f"{index}. {_quote_candidate_label(candidate)}")
    lines.append("Choose the matching market below, and I’ll set it as the active context.")
    return chr(10).join(lines)


def _quote_deductions_text(candidate: dict[str, Any]) -> str:
    yes_price = candidate.get("yes_price")
    no_price = candidate.get("no_price")
    try:
        yes_num = float(yes_price)
    except (TypeError, ValueError):
        yes_num = None
    try:
        no_num = float(no_price)
    except (TypeError, ValueError):
        no_num = None

    lines = ["<b>Deductions</b>"]
    lines.append(f"YES price: {_code(_format_number(yes_num))}")
    lines.append(f"NO price: {_code(_format_number(no_num))}")

    if yes_num is not None and no_num is not None:
        if yes_num >= 0.7:
            lines.append("Bias: YES is crowded. Best move bias is sell YES, buy NO, or switch if your thesis is weaker.")
        elif yes_num <= 0.3:
            lines.append("Bias: NO is crowded. Best move bias is sell NO, buy YES, or switch if the setup changes.")
        else:
            lines.append("Bias: balanced. Best move bias is wait, size small, and avoid forcing an entry.")
    else:
        lines.append("Bias: price data is incomplete, so wait for a cleaner read before acting.")

    currency = _first_string(candidate.get("currency"), default="USD").upper()
    lines.append(f"Currency context: {_safe_html(currency)}")
    return chr(10).join(lines)


def _quant_monitor_text(candidate: dict[str, Any]) -> str:
    yes_price = candidate.get("yes_price")
    no_price = candidate.get("no_price")
    try:
        yes_num = float(yes_price)
    except (TypeError, ValueError):
        yes_num = None
    try:
        no_num = float(no_price)
    except (TypeError, ValueError):
        no_num = None

    if yes_num is not None and no_num is not None:
        if yes_num >= 0.7:
            action = "sell YES / buy NO / switch if you need to reduce risk"
        elif yes_num <= 0.3:
            action = "buy YES / sell NO / switch if your thesis improved"
        else:
            action = "wait, scale smaller, or switch when a stronger edge appears"
    else:
        action = "wait until the price feed is complete"

    return chr(10).join([
        "<b>Quant monitor</b>",
        f"Active context: {_safe_html(candidate.get('event_title') or '')} · {_safe_html(candidate.get('market_title') or '')}",
        f"Best move bias: {_safe_html(action)}",
        "Monitor rule: reassess if YES/NO drifts sharply, spread widens, or liquidity dries up.",
        "Monitor state saved so future alerts can reuse the same market context.",
    ])


def _selected_quote_text(candidate: dict[str, Any], quote_response: Optional[QuoteResponse] = None) -> str:
    event = candidate.get("event") if isinstance(candidate.get("event"), dict) else {}
    market = candidate.get("market") if isinstance(candidate.get("market"), dict) else {}
    lines = [f"<b>Active market</b>: {_bold(candidate.get('event_title') or _event_title(event))}"]
    lines.append(f"Market: {_bold(candidate.get('market_title') or _market_title(market))}")
    if quote_response is not None:
        lines.append(f"Live quote bid: {_code(_format_number(quote_response.quote.bid))} ask: {_code(_format_number(quote_response.quote.ask))}")
        lines.append(f"Last: {_code(_format_number(quote_response.quote.last))} midpoint: {_code(_format_number(quote_response.quote.midpoint))}")
    lines.append(_quote_deductions_text(candidate))
    lines.append(_quant_monitor_text(candidate))
    return chr(10).join(lines)


def build_fund_command(client: Optional[BayseClient] = None, text: str = "") -> CommandResult:
    asset = _funding_asset_from_text(text)
    if asset is None and _normalize_text(text):
        return CommandResult(False, "Usage: /fund [NGN|USD]")
    if client is None:
        return CommandResult(True, _fund_text(asset))
    try:
        payload = client.get_assets()
        return CommandResult(True, _wallet_assets_text(payload, asset_filter=asset, purpose="Funding"), raw=payload)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


def build_withdraw_command(client: Optional[BayseClient] = None, text: str = "") -> CommandResult:
    asset = _withdraw_asset_from_text(text)
    if asset is None and _normalize_text(text):
        return CommandResult(False, "Usage: /withdraw [NGN|USD]")
    if client is None:
        return CommandResult(True, _withdraw_text(asset))
    try:
        payload = client.get_assets()
        return CommandResult(True, _wallet_assets_text(payload, asset_filter=asset, purpose="Withdrawal"), raw=payload)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


def build_quote_command(client: BayseClient, text: str, context: Any = None) -> CommandResult:
    term = _search_term_after(text, QUOTE_STOP_WORDS)
    active_candidate = _trade_context_candidate(context)
    if not term and active_candidate:
        market_id = active_candidate.get("market_id")
        if market_id:
            try:
                quote_payload = client.get_ticker(market_id)
                quote_response = QuoteResponse.from_dict(quote_payload)
                raw = {"mode": "quote", "term": "", "events": [active_candidate.get("event")], "quote_candidates": [active_candidate], "payload": quote_payload, "active": True}
                return CommandResult(True, _selected_quote_text(active_candidate, quote_response), raw=raw)
            except BayseClientError as exc:
                return CommandResult(False, _error_text(exc))
            except Exception as exc:
                return CommandResult(False, f"Bayse API error\nmessage: {exc}")
    if not term:
        return CommandResult(False, "What do you want to quote?")

    payload: Any = None
    if _is_uuid_like(term):
        try:
            payload = client.get_event(term)
        except BayseClientError:
            payload = None
        except Exception:
            payload = None

    if payload is None:
        return CommandResult(False, f"No quote data found for {term}")


def build_order_command(client: BayseClient, text: str, context: Any = None) -> CommandResult:
    args = _split_args(text)
    active_candidate = _active_market_candidate(context)
    selected_trade = _active_trade_selection(context)
    order_state = _active_trade_order_state(context)
    if not isinstance(selected_trade, dict) and isinstance(order_state, dict):
        selected_trade = order_state
    context_candidate = active_candidate if isinstance(active_candidate, dict) else order_state if isinstance(order_state, dict) else selected_trade if isinstance(selected_trade, dict) else None
    use_active_context = isinstance(context_candidate, dict) and bool(
        _first_string(
            context_candidate.get("event_id"),
            context_candidate.get("eventId"),
            context_candidate.get("eventid"),
            order_state.get("event_id") if isinstance(order_state, dict) else None,
            order_state.get("eventId") if isinstance(order_state, dict) else None,
            order_state.get("eventid") if isinstance(order_state, dict) else None,
            selected_trade.get("event_id") if isinstance(selected_trade, dict) else None,
            selected_trade.get("eventId") if isinstance(selected_trade, dict) else None,
            selected_trade.get("eventid") if isinstance(selected_trade, dict) else None,
            default="",
        )
    ) and bool(
        _first_string(
            context_candidate.get("market_id"),
            context_candidate.get("marketId"),
            context_candidate.get("marketid"),
            order_state.get("market_id") if isinstance(order_state, dict) else None,
            order_state.get("marketId") if isinstance(order_state, dict) else None,
            order_state.get("marketid") if isinstance(order_state, dict) else None,
            selected_trade.get("market_id") if isinstance(selected_trade, dict) else None,
            selected_trade.get("marketId") if isinstance(selected_trade, dict) else None,
            selected_trade.get("marketid") if isinstance(selected_trade, dict) else None,
            default="",
        )
    )
    outcome_text = ""
    outcome_id = ""

    event_id = _first_string(
        context_candidate.get("event_id") if use_active_context and isinstance(context_candidate, dict) else None,
        context_candidate.get("eventId") if use_active_context and isinstance(context_candidate, dict) else None,
        context_candidate.get("eventid") if use_active_context and isinstance(context_candidate, dict) else None,
        order_state.get("event_id") if use_active_context and isinstance(order_state, dict) else None,
        order_state.get("eventId") if use_active_context and isinstance(order_state, dict) else None,
        order_state.get("eventid") if use_active_context and isinstance(order_state, dict) else None,
        selected_trade.get("event_id") if use_active_context and isinstance(selected_trade, dict) else None,
        selected_trade.get("eventId") if use_active_context and isinstance(selected_trade, dict) else None,
        selected_trade.get("eventid") if use_active_context and isinstance(selected_trade, dict) else None,
        default="",
    )
    market_id = _first_string(
        context_candidate.get("market_id") if use_active_context and isinstance(context_candidate, dict) else None,
        context_candidate.get("marketId") if use_active_context and isinstance(context_candidate, dict) else None,
        context_candidate.get("marketid") if use_active_context and isinstance(context_candidate, dict) else None,
        order_state.get("market_id") if use_active_context and isinstance(order_state, dict) else None,
        order_state.get("marketId") if use_active_context and isinstance(order_state, dict) else None,
        order_state.get("marketid") if use_active_context and isinstance(order_state, dict) else None,
        selected_trade.get("market_id") if use_active_context and isinstance(selected_trade, dict) else None,
        selected_trade.get("marketId") if use_active_context and isinstance(selected_trade, dict) else None,
        selected_trade.get("marketid") if use_active_context and isinstance(selected_trade, dict) else None,
        default="",
    )
    side = _normalize_text((order_state or {}).get("side") or (selected_trade.get("side") if isinstance(selected_trade, dict) else "")).lower()
    if side == "long":
        side = "buy"
    elif side == "short":
        side = "sell"
    currency = _normalize_text((order_state or {}).get("currency")).upper()
    amount: Optional[float] = None
    price: Optional[float] = None
    order_type = "MARKET"
    trailing: list[str] = []

    if use_active_context and selected_trade and len(args) >= 2:
        outcome_text = _first_string(selected_trade.get("outcome_label"), default="")
        if not side:
            side = _normalize_text(args[2] if len(args) >= 3 else "").lower()
        try:
            amount = float(args[0])
        except ValueError:
            return CommandResult(False, "Choose a currency first, then send the amount.", raw={"next_step": "currency"})
        currency = _normalize_text(args[1]).upper() or currency
        trailing = args[2:]
        outcome_id = _resolve_order_outcome_id(context_candidate or {}, outcome_text=outcome_text, side=side, selected_trade=selected_trade)
    elif use_active_context and len(args) >= 4:
        outcome_text = args[0]
        side = _normalize_text(args[1]).lower() or side
        try:
            amount = float(args[2])
        except ValueError:
            return CommandResult(False, "Choose a currency first, then send the amount.", raw={"next_step": "currency"})
        currency = _normalize_text(args[3]).upper() or currency
        trailing = args[4:]
        outcome_id = _resolve_order_outcome_id(context_candidate or {}, outcome_text=outcome_text, side=side, selected_trade=selected_trade)
    elif use_active_context:
        if not side:
            side = _normalize_text(order_state.get("side") if isinstance(order_state, dict) else "").lower()
        if not side and isinstance(selected_trade, dict):
            side = _normalize_text(selected_trade.get("side")).lower()
        outcome_text = _first_string((order_state or {}).get("outcome_label"), selected_trade.get("outcome_label") if isinstance(selected_trade, dict) else "", default="")
        if not outcome_text and side in {"buy", "sell"}:
            outcome_text = "YES" if side == "buy" else "NO"
        if not currency and isinstance(order_state, dict):
            currency = _normalize_text(order_state.get("currency")).upper()
        if not currency:
            prompt = f"Active market: {_safe_html(context_candidate.get('event_title') or '')} · {_safe_html(context_candidate.get('market_title') or '')}\nChoose a currency to continue."
            _set_pending_interaction(context, "trade_currency", prompt=prompt)
            return CommandResult(False, prompt, raw={"next_step": "currency", "active_market": context_candidate.get("market_id") if isinstance(context_candidate, dict) else None})
        if len(args) == 1:
            try:
                amount = float(args[0])
            except ValueError:
                return CommandResult(False, "Send the amount as a number, like 200.", raw={"next_step": "amount"})
        elif len(args) == 0:
            prompt = f"Active market: {_safe_html(context_candidate.get('event_title') or '')} · {_safe_html(context_candidate.get('market_title') or '')}\nSend the amount now."
            _set_pending_interaction(context, "trade_amount", prompt=prompt)
            return CommandResult(False, prompt, raw={"next_step": "amount", "active_market": context_candidate.get("market_id") if isinstance(context_candidate, dict) else None})
        else:
            try:
                amount = float(args[0])
            except ValueError:
                return CommandResult(False, "Send the amount as a number, like 200.", raw={"next_step": "amount"})
            trailing = args[1:]
        outcome_id = _resolve_order_outcome_id(context_candidate or {}, outcome_text=outcome_text, side=side, selected_trade=selected_trade)
    else:
        if len(args) < 6:
            if use_active_context:
                prompt = f"Active market: {_safe_html(context_candidate.get('event_title') or '')} · {_safe_html(context_candidate.get('market_title') or '')}\nChoose an outcome and send the amount."
                return CommandResult(False, prompt, raw={"next_step": "amount"})

    if not isinstance(context_candidate, dict):
        return CommandResult(False, "I need an active market before I can place that order.")
    if not event_id or not market_id:
        return CommandResult(False, "I need both an event and market before I can place that order.")
    if not side:
        return CommandResult(False, "Choose buy or sell before placing the order.")
    if amount is None:
        return CommandResult(False, "Send the amount as a number, like 200.", raw={"next_step": "amount"})
    if not currency:
        return CommandResult(False, "Choose a currency first, then send the amount.", raw={"next_step": "currency"})
    if not outcome_id:
        outcome_id = _resolve_order_outcome_id(context_candidate or {}, outcome_text=outcome_text, side=side, selected_trade=selected_trade)
    if not outcome_id:
        return CommandResult(False, "I couldn’t determine the outcome for this order.")

    for token in trailing:
        token_text = _normalize_text(token).upper()
        if token_text in {"LIMIT", "MARKET"}:
            order_type = token_text
            continue
        if price is None:
            try:
                price = float(token)
                order_type = "LIMIT"
            except ValueError:
                continue

    order_type = _normalize_text(order_type).upper() or "MARKET"
    if order_type not in {"MARKET", "LIMIT"}:
        order_type = "MARKET"
    if price is not None and order_type != "LIMIT":
        order_type = "LIMIT"

    try:
        response_payload = client.place_order(
            event_id,
            market_id,
            outcome_id=outcome_id,
            side=side,
            amount=amount,
            currency=currency,
            order_type=order_type,
            price=price,
        )
        response = OrderResponse.from_dict(response_payload)
        return CommandResult(True, _order_text(response), raw=response.raw or response_payload)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")

def build_smart_trade_command(client: BayseClient, text: str, context: Any = None) -> Optional[CommandResult]:
    active_candidate = _trade_context_candidate(context)
    if not isinstance(active_candidate, dict) or not active_candidate.get("event_id") or not active_candidate.get("market_id"):
        return None
    if not _looks_like_smart_trade_intent(text):
        return None

    parsed = _brain_parse_trade_intent(text, active_candidate)
    if not parsed:
        return CommandResult(False, "I couldn’t parse that trade. Try something like ‘Buy Yes for 200 NGN’ or ‘Sell No at 1 USD’.")

    side = _normalize_text(parsed.get("side")).lower()
    if side == "long":
        side = "buy"
    elif side == "short":
        side = "sell"
    if side not in {"buy", "sell"}:
        return CommandResult(False, "I couldn’t determine whether that was a buy or sell. Try again with buy or sell first.")

    amount_value = parsed.get("amount")
    try:
        amount = float(amount_value)
    except (TypeError, ValueError):
        return CommandResult(False, "I couldn’t read the amount. Try something like ‘Buy Yes for 200 NGN’.")

    order_state = _active_trade_order_state(context)
    selected_trade = _active_trade_selection(context)
    if not isinstance(selected_trade, dict) and isinstance(order_state, dict):
        selected_trade = order_state
    currency = _normalize_text(parsed.get("currency") or parsed.get("normalized_currency") or _smart_trade_currency(active_candidate)).upper()
    if not currency:
        currency = _smart_trade_currency(active_candidate)

    outcome = _normalize_text(parsed.get("outcome") or (selected_trade.get("outcome_label") if isinstance(selected_trade, dict) else "") or ("YES" if side == "buy" else "NO")).upper()
    if outcome in {"LONG", "SHORT"}:
        outcome = "YES" if outcome == "LONG" else "NO"
    if not outcome:
        outcome = "YES" if side == "buy" else "NO"

    _set_trade_order_state(context, active_candidate, side=side, currency=currency, amount=amount, outcome_label=outcome, stage="ready")
    synthetic_text = f"{amount:g} {currency}"
    result = build_order_command(client, synthetic_text, context=context)
    if result.ok and isinstance(result.raw, dict):
        return CommandResult(result.ok, result.text, raw={**result.raw, "smart_trade": True, "smart_trade_source": text, "smart_trade_parsed": parsed})
    return result


def build_events_command(client: BayseClient, text: str = "") -> CommandResult:
    term = _search_term_after(text, WATCH_STOP_WORDS)
    params: dict[str, Any] = {"status": "open"}
    heading = "Active markets"
    if term:
        heading = f"Search results for {_normalize_text(term)}"
        if term.lower() in KNOWN_EVENT_CATEGORIES:
            params = {"category": term.lower(), "status": "open"}
        else:
            params = {"keyword": term, "status": "open"}
    try:
        payload = client.list_events(page=1, size=WATCHLIST_PAGE_SIZE, params=params)
        events = _extract_collection(payload)
        if not events:
            return CommandResult(False, "No markets or events were returned by Bayse.")
        raw = {"mode": "events", "term": term, "events": events, "payload": payload, "params": params}
        return CommandResult(True, _events_text(events, heading=heading), raw=raw)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


def build_watchlist_command(client: BayseClient, text: str = "") -> CommandResult:
    term = _search_term_after(text, WATCH_STOP_WORDS)
    params: dict[str, Any]
    heading = "Watchlist"
    if term:
        heading = f"Watch results for {_normalize_text(term)}"
        if term.lower() in KNOWN_EVENT_CATEGORIES:
            params = {"category": term.lower()}
        else:
            params = {"keyword": term}
    else:
        params = {"watchlist": True}
    try:
        payload = client.list_events(page=1, size=WATCHLIST_PAGE_SIZE, params=params)
        events = _extract_collection(payload)
        if not events and not term:
            payload = client.list_events(page=1, size=WATCHLIST_PAGE_SIZE, params={"status": "open"})
            events = _extract_collection(payload)
            heading = "Active markets"
        if not events:
            return CommandResult(False, "No markets or events were returned by Bayse.")
        raw = {"mode": "watch", "term": term, "events": events, "payload": payload, "params": params}
        return CommandResult(True, _events_text(events, heading=heading), raw=raw)
    except BayseClientError as exc:
        return CommandResult(False, _error_text(exc))
    except Exception as exc:
        return CommandResult(False, f"Bayse API error\nmessage: {exc}")


def _portfolio_payloads(client: BayseClient) -> list[tuple[str, Any]]:
    payloads: list[tuple[str, Any]] = []
    for label, fetcher in (("balance", client.get_balance), ("portfolio", client.get_portfolio)):
        try:
            payloads.append((label, fetcher()))
        except BayseClientError as exc:
            logger.info("Bayse %s request failed: %s", label, exc)
        except Exception as exc:
            logger.info("Bayse %s request failed: %s", label, exc)
    return payloads


def build_balance_command(client: BayseClient, text: str = "") -> CommandResult:
    last_error: Optional[BayseClientError] = None
    last_payload: Any = None
    for label, fetcher in (("balance", client.get_balance), ("portfolio", client.get_portfolio), ("assets", client.get_assets)):
        try:
            payload = fetcher()
            last_payload = payload
            balance_value = _portfolio_balance_value(payload)
            logger.info("Bayse %s data fetched for balance command", label)
            if balance_value != "n/a":
                return CommandResult(True, _portfolio_text(payload, "Wallet balance"), raw=payload)
        except BayseClientError as exc:
            last_error = exc
            logger.info("Bayse %s request failed for balance command: %s", label, exc)
        except Exception as exc:
            logger.info("Bayse %s request failed for balance command: %s", label, exc)
    if last_payload is not None:
        return CommandResult(True, _portfolio_text(last_payload, "Wallet balance"), raw=last_payload)
    if last_error is not None:
        return CommandResult(False, _error_text(last_error))
    return CommandResult(False, "Bayse API error\nmessage: unable to fetch portfolio data")


def _portfolio_mapping(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in ("portfolio", "data", "result", "wallet", "account"):
            value = payload.get(key)
            if isinstance(value, dict):
                nested = _portfolio_mapping(value)
                if nested:
                    return nested
        return payload
    return {}


def _portfolio_collection(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return _extract_collection(payload)
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        extracted = _extract_collection(value)
        if extracted:
            return extracted
        if isinstance(value, dict):
            nested = _portfolio_collection(value, keys)
            if nested:
                return nested
    for value in payload.values():
        if isinstance(value, dict):
            nested = _portfolio_collection(value, keys)
            if nested:
                return nested
    return []


def _portfolio_balance_value(payload: Any) -> str:
    data = _portfolio_mapping(payload)
    balance_keys = (
        "balance",
        "walletBalance",
        "availableBalance",
        "cashBalance",
        "cash",
        "available",
        "available_cash",
        "totalBalance",
        "settledBalance",
        "equity",
        "ngnBalance",
        "primaryBalance",
        "primary_balance",
    )
    preferred_asset_keys = (
        ("balances", "NGN", "availableBalance"),
        ("balances", "NGN", "balance"),
        ("balances", "ngn", "availableBalance"),
        ("balances", "ngn", "balance"),
        ("wallet", "balances", "NGN", "availableBalance"),
        ("wallet", "balances", "NGN", "balance"),
        ("wallet", "balances", "ngn", "availableBalance"),
        ("wallet", "balances", "ngn", "balance"),
        ("balances", "cash", "NGN"),
        ("balances", "available", "NGN"),
        ("funds", "available", "NGN"),
    )
    for path in preferred_asset_keys:
        value = _mapping_value(data, *path)
        if value is not None and _normalize_text(value):
            return _format_number(value)
    for key in balance_keys:
        value = data.get(key)
        if value is not None and _normalize_text(value):
            return _format_number(value)
    for path in (
        ("wallet", "balance"),
        ("wallet", "availableBalance"),
        ("wallet", "cashBalance"),
        ("account", "balance"),
        ("account", "availableBalance"),
        ("portfolio", "balance"),
        ("portfolio", "availableBalance"),
        ("summary", "balance"),
        ("summary", "availableBalance"),
        ("balances", "cash"),
        ("balances", "available"),
        ("funds", "available"),
    ):
        value = _mapping_value(data, *path)
        if value is not None and _normalize_text(value):
            return _format_number(value)
    return "n/a"


def _portfolio_text(payload: Any, heading: str) -> str:
    data = _portfolio_mapping(payload)
    positions = _portfolio_collection(payload, ("positions", "openPositions", "open_positions", "holdings", "portfolioPositions", "assets", "items"))
    lines = [f"<b>{_safe_html(heading)}</b>"]

    lines.append(f"Wallet balance: {_code(_portfolio_balance_value(data))}")

    if positions:
        lines.append("Open positions:")
        for idx, position in enumerate(positions, start=1):
            title = _first_string(
                _mapping_value(position, "metadata", "name"),
                _mapping_value(position, "metadata", "title"),
                _mapping_value(position, "market", "name"),
                _mapping_value(position, "market", "title"),
                _mapping_value(position, "market", "metadata", "name"),
                _mapping_value(position, "market", "metadata", "title"),
                position.get("title"),
                position.get("name"),
                position.get("marketName"),
                position.get("market_name"),
                position.get("symbol"),
                default=f"Position {idx}",
            )
            size = _first_string(position.get("quantity"), position.get("size"), position.get("amount"), position.get("exposure"), position.get("availableBalance"), default="n/a")
            direction = _signal_emoji(position.get("direction") or position.get("side") or position.get("sentiment"))
            prefix = f"{direction} " if direction else ""
            lines.append(f"{idx}. {prefix}<b>{_safe_html(title)}</b> — {_code(size)}")
    else:
        lines.append("Open positions: none")

    return "\n".join(lines)


def build_portfolio_command(client: BayseClient, text: str = "") -> CommandResult:
    try:
        payload = client.get_portfolio()
        logger.info("Bayse portfolio data fetched for portfolio command")
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
            "/quote <code>market name or symbol</code> - Search Bayse markets and pick one interactively (or send /quote alone to be prompted)",
            "/events [term] - List active markets or search by keyword (or send /events alone to be prompted)",
            "/order <code>event name</code> <code>market name</code> <code>outcome</code> <code>buy|sell</code> <code>amount</code> <code>currency</code> [price] [LIMIT|MARKET] - Place a trade order (or send /order alone for a guided prompt)",
            "Reply with something short like ‘Buy, 700 NGN’ once a market is active and I’ll parse it for you",
            "/balance - Check your wallet balance",
            "/portfolio - View open positions",
            "/fund [NGN|USD] - Show funding options for the selected currency (or send /fund alone for buttons)",
            "/withdraw [NGN|USD] - Show withdrawal options for the selected currency (or send /withdraw alone for buttons)",
            "/help - Show bot usage info",
            GENERAL_QUANT_GUIDANCE.capitalize(),
        ]),
    )


def format_quote_response(response: QuoteResponse) -> str:
    return _quote_text(response)


def format_order_response(response: OrderResponse) -> str:
    return _order_text(response)


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


def _looks_like_events_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("show events", "events", "active markets", "list markets", "market list", "market events"))


def _looks_like_watch_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("watch", "watchlist", "monitor", "track", "follow", "economy trades", "trades"))


def _looks_like_balance_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("balance", "wallet", "cash", "funds"))


def _looks_like_portfolio_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("portfolio", "positions", "holdings", "open positions"))


def _looks_like_help_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("help", "how do i use", "usage", "commands"))


def _looks_like_fund_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("fund", "deposit", "add cash", "cash in", "top up", "topup"))


def _looks_like_withdraw_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("withdraw", "cash out", "payout", "send out", "remove funds"))


def _looks_like_quote_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("quote", "price", "ticker"))


def _looks_like_order_intent(text: str) -> bool:
    normalized = _normalize_text(text).lower()
    return any(keyword in normalized for keyword in ("order", "buy", "sell", "long", "short", "trade", "place", "limit", "market"))


def _general_plain_text_response(text: str) -> CommandResult:
    return CommandResult(True, chr(10).join([
        "<b>Medes Et Bayse</b>",
        "Try /events to browse markets, /quote to inspect one, or /order to place a trade.",
        "If you already picked a market, just send quote, order, or a short reply like “Buy Yes for 200 NGN” and I’ll reuse the active context.",
        GENERAL_QUANT_GUIDANCE.capitalize(),
    ]))


def _pending_interaction_kind(context: Any) -> str:
    pending = getattr(context, "user_data", {}).get("pending_interaction") if context is not None else None
    if isinstance(pending, dict):
        return _normalize_text(pending.get("kind")).lower()
    return _normalize_text(pending).lower()


def _set_pending_interaction(context: Any, kind: str, *, prompt: Optional[str] = None) -> None:
    if context is None:
        return
    context.user_data["pending_interaction"] = {"kind": _normalize_text(kind).lower(), "prompt": prompt or ""}


def _clear_pending_interaction(context: Any) -> None:
    if context is None:
        return
    context.user_data.pop("pending_interaction", None)


def _route_pending_interaction(client: BayseClient, context: Any, text: str) -> Optional[CommandResult]:
    kind = _pending_interaction_kind(context)
    if not kind:
        return None

    text_value = _normalize_text(text)
    if kind == "quote":
        if not text_value:
            return CommandResult(False, "What do you want to quote?")
        result = build_quote_command(client, text_value, context=context)
        if result.ok:
            _clear_pending_interaction(context)
        return result

    if kind == "events":
        if not text_value:
            return CommandResult(False, "What events do you want to see?")
        result = build_events_command(client, text_value)
        if result.ok:
            _clear_pending_interaction(context)
        return result

    if kind == "order":
        if not text_value:
            return CommandResult(False, "What order do you want to place? Send the event, market, outcome, side, amount, and currency.")
        smart_result = build_smart_trade_command(client, text_value, context=context)
        if smart_result is not None:
            if smart_result.ok:
                _clear_pending_interaction(context)
            return smart_result
        result = build_order_command(client, text_value, context=context)
        if result.ok:
            _clear_pending_interaction(context)
        return result

    if kind == "trade_currency":
        if not text_value:
            return CommandResult(False, "Choose NGN or USD to continue.", raw={"next_step": "currency"})
        currency = _normalize_text(text_value).upper()
        if currency not in {"NGN", "USD"}:
            return CommandResult(False, "Choose NGN or USD to continue.", raw={"next_step": "currency"})
        candidate = _trade_context_candidate(context) or {}
        if isinstance(candidate, dict) and candidate:
            state = _active_trade_order_state(context) or {}
            _set_trade_order_state(context, candidate, currency=currency, stage="amount", outcome_id=state.get("outcome_id"), outcome_label=state.get("outcome_label"), side=state.get("side"))
        _set_pending_interaction(context, "trade_amount", prompt="Send the amount now.")
        return CommandResult(False, "Send the amount now.", raw={"next_step": "amount", "currency": currency})

    if kind == "trade_amount":
        if not text_value:
            return CommandResult(False, "Send the amount as a number, like 200.", raw={"next_step": "amount"})
        state = _active_trade_order_state(context)
        if not isinstance(state, dict) or not state.get("currency"):
            return CommandResult(False, "Choose a currency first.", raw={"next_step": "currency"})
        try:
            amount = float(text_value)
        except ValueError:
            return CommandResult(False, "Send the amount as a number, like 200.", raw={"next_step": "amount"})
        candidate = _trade_context_candidate(context) or {}
        if isinstance(candidate, dict) and candidate:
            _set_trade_order_state(context, candidate, amount=amount, stage="ready", outcome_id=state.get("outcome_id"), outcome_label=state.get("outcome_label"), side=state.get("side"), currency=state.get("currency"))
        result = build_order_command(client, f"{amount:g} {_normalize_text(state.get('currency')).upper()}", context=context)
        if result.ok:
            _clear_pending_interaction(context)
        return result

    if kind == "fund":
        if not text_value:
            return CommandResult(False, "Choose NGN or USD to see funding options.")
        result = build_fund_command(client, text_value)
        if result.ok:
            _clear_pending_interaction(context)
        return result

    if kind == "withdraw":
        if not text_value:
            return CommandResult(False, "Choose NGN or USD to see withdrawal options.")
        result = build_withdraw_command(client, text_value)
        if result.ok:
            _clear_pending_interaction(context)
        return result

    return None


def build_natural_language_command(client: BayseClient, text: str, context: Any = None) -> CommandResult:
    if not _normalize_text(text):
        return _general_plain_text_response(text)
    if _looks_like_quote_intent(text):
        return build_quote_command(client, text, context=context)
    smart_trade = build_smart_trade_command(client, text, context=context)
    if smart_trade is not None:
        return smart_trade
    if _looks_like_events_intent(text):
        return build_events_command(client, text)
    if _looks_like_order_intent(text):
        return build_order_command(client, text, context=context)
    if _looks_like_watch_intent(text):
        return build_watchlist_command(client, text)
    if _looks_like_balance_intent(text):
        return build_balance_command(client)
    if _looks_like_portfolio_intent(text):
        return build_portfolio_command(client)
    if _looks_like_fund_intent(text):
        return build_fund_command(text=text)
    if _looks_like_withdraw_intent(text):
        return build_withdraw_command(text=text)
    if _looks_like_help_intent(text):
        return build_help_command()
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
        pending_result = _route_pending_interaction(client, context, text)
        if pending_result is not None:
            result = pending_result
        else:
            result = build_natural_language_command(client, text, context=context)
        if not result.ok:
            print(json.dumps({"telegram": "text_error", "text": text, "response": result.text}, ensure_ascii=False), flush=True)
            if isinstance(result.raw, dict) and result.raw.get("next_step") == "currency":
                candidate = _trade_context_candidate(context) or {}
                reply_text = result.text
                if isinstance(candidate, dict) and candidate:
                    reply_text = _trade_currency_prompt_text(candidate)
                await message.reply_text(reply_text, reply_markup=_trade_currency_keyboard(), parse_mode="HTML")
                return
            await message.reply_text(result.text, parse_mode="HTML")
            return
        if result.raw and isinstance(result.raw, dict) and result.raw.get("quote_candidates"):
            candidates = result.raw.get("quote_candidates", [])
            context.user_data["quote_candidates"] = candidates
            context.user_data["quote_search_term"] = result.raw.get("term")
            if not result.raw.get("active"):
                context.user_data.pop("active_event", None)
                context.user_data.pop("active_market", None)
                context.user_data.pop("active_market_candidate", None)
            print(json.dumps({"telegram": "text_routed", "route": "quote_search", "text": text}, ensure_ascii=False), flush=True)
            await message.reply_text(result.text, reply_markup=_quote_keyboard(candidates), parse_mode="HTML")
            return
        if result.raw and isinstance(result.raw, dict) and result.raw.get("events"):
            events = result.raw.get("events", [])
            route = str(result.raw.get("mode") or "watchlist")
            context.user_data["watch_query"] = text
            print(json.dumps({"telegram": "text_routed", "route": route, "text": text}, ensure_ascii=False), flush=True)
            await message.reply_text(result.text, reply_markup=_watchlist_keyboard(events), parse_mode="HTML")
            return
        print(json.dumps({"telegram": "text_routed", "route": "general", "text": text}, ensure_ascii=False), flush=True)
        await message.reply_text(result.text, parse_mode="HTML")

    return handler


def fund_handler_factory(client: Optional[BayseClient] = None) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        text = getattr(message, "text", "") or ""
        if len(_split_args(text)) == 0:
            _set_pending_interaction(context, "fund", prompt="Choose NGN or USD to see funding options.")
            await message.reply_text(
                "Choose NGN or USD to see funding options.",
                reply_markup=_asset_keyboard("fund"),
                parse_mode="HTML",
            )
            return
        result = build_fund_command(client, text=text)
        await message.reply_text(result.text, parse_mode="HTML")

    return handler


def withdraw_handler_factory(client: Optional[BayseClient] = None) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        text = getattr(message, "text", "") or ""
        if len(_split_args(text)) == 0:
            _set_pending_interaction(context, "withdraw", prompt="Choose NGN or USD to see withdrawal options.")
            await message.reply_text(
                "Choose NGN or USD to see withdrawal options.",
                reply_markup=_asset_keyboard("withdraw"),
                parse_mode="HTML",
            )
            return
        result = build_withdraw_command(client, text=text)
        await message.reply_text(result.text, parse_mode="HTML")

    return handler


def quote_handler_factory(client: BayseClient) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        text = getattr(message, "text", "") or ""
        active_candidate = _active_market_candidate(context)
        if len(_split_args(text)) == 0 and not active_candidate:
            _set_pending_interaction(context, "quote", prompt="What do you want to quote?")
            await message.reply_text("What do you want to quote?", parse_mode="HTML")
            return
        result = build_quote_command(client, text, context=context)
        if _should_suppress_debug_message(result.text):
            return
        candidates = result.raw.get("quote_candidates", []) if result.raw else []
        if candidates and not result.raw.get("active"):
            context.user_data["quote_candidates"] = candidates
            context.user_data["quote_search_term"] = result.raw.get("term") if result.raw else None
            await message.reply_text(result.text, reply_markup=_quote_keyboard(candidates), parse_mode="HTML")
            return
        await message.reply_text(result.text, parse_mode="HTML")

    return handler


def order_handler_factory(client: BayseClient) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        text = getattr(message, "text", "") or ""
        active_candidate = _trade_context_candidate(context)
        smart_result = build_smart_trade_command(client, text, context=context)
        if smart_result is not None:
            if not smart_result.ok:
                await message.reply_text(smart_result.text, parse_mode="HTML")
                return
            await message.reply_text(smart_result.text, parse_mode="HTML")
            scenario = _order_scenario_from_result(smart_result)
            if scenario:
                await send_scenario_sticker(message, scenario)
            return
        if active_candidate:
            result = build_order_command(client, text, context=context)
            if _should_suppress_debug_message(result.text):
                return
            if isinstance(result.raw, dict) and result.raw.get("next_step") == "currency":
                await message.reply_text(_trade_currency_prompt_text(active_candidate), reply_markup=_trade_currency_keyboard(), parse_mode="HTML")
                return
            await message.reply_text(result.text, parse_mode="HTML")
            scenario = _order_scenario_from_result(result)
            if scenario:
                await send_scenario_sticker(message, scenario)
            return
        needs_prompt = len(_split_args(text)) < 6
        if needs_prompt:
            _set_pending_interaction(context, "order", prompt="What order do you want to place? Send outcome, buy|sell, amount, and currency.")
            await message.reply_text("What order do you want to place? Send outcome, buy|sell, amount, and currency.", parse_mode="HTML")
            return
        result = build_order_command(client, text, context=context)
        if _should_suppress_debug_message(result.text):
            return
        if isinstance(result.raw, dict) and result.raw.get("next_step") == "currency" and active_candidate:
            await message.reply_text(_trade_currency_prompt_text(active_candidate), reply_markup=_trade_currency_keyboard(), parse_mode="HTML")
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
        text = getattr(message, "text", "") or ""
        result = build_watchlist_command(client, text=text)
        if not result.ok:
            await message.reply_text(result.text, parse_mode="HTML")
            return
        events = result.raw.get("events", []) if result.raw else []
        context.user_data["watch_query"] = text
        await message.reply_text(result.text, reply_markup=_watchlist_keyboard(events), parse_mode="HTML")

    return handler


def events_handler_factory(client: BayseClient) -> Callable[[Any, Any], Any]:
    async def handler(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return
        text = getattr(message, "text", "") or ""
        if len(_split_args(text)) == 0:
            _set_pending_interaction(context, "events", prompt="What events do you want to see?")
            await message.reply_text(
                "What events do you want to see? Send a keyword, category, or say show events.",
                parse_mode="HTML",
            )
            return
        result = build_events_command(client, text=text)
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
        if not (data.startswith("watch:") or data.startswith("quote:") or data.startswith("fund:") or data.startswith("withdraw:") or data.startswith("more:") or data.startswith("tradeo:") or data.startswith("trades:") or data.startswith("tradec:")):
            return

        await query.answer()
        prefix, selected = data.split(":", 1)

        if prefix == "more":
            bucket = _detail_view_bucket(context)
            detail = bucket.get(selected)
            if not isinstance(detail, dict):
                await query.edit_message_text("That expanded view is no longer available.", parse_mode="HTML")
                return
            back_callback = _normalize_text(detail.get("back_callback")) or None
            back_label = _normalize_text(detail.get("back_label")) or "Back"
            keyboard = _detail_keyboard(selected, back_callback=back_callback, view_more=False, back_label=back_label)
            await query.edit_message_text(str(detail.get("text") or ""), reply_markup=keyboard, parse_mode="HTML")
            return

        if prefix in {"tradeo", "trades"}:
            parts = data.split(":", 2)
            if len(parts) != 3:
                await query.edit_message_text("That trade selection is no longer available.", parse_mode="HTML")
                return
            _, view_key, selected_value = parts
            trade_view = _trade_view_bucket(context).get(view_key)
            if not isinstance(trade_view, dict):
                await query.edit_message_text("That trade selection is no longer available.", parse_mode="HTML")
                return
            candidate = trade_view.get("candidate") if isinstance(trade_view.get("candidate"), dict) else None
            if not isinstance(candidate, dict):
                await query.edit_message_text("That trade selection is no longer available.", parse_mode="HTML")
                return

            market = candidate.get("market") if isinstance(candidate.get("market"), dict) else {}
            outcomes = _trade_outcomes(market)
            if prefix == "tradeo":
                try:
                    outcome_index = int(selected_value)
                except ValueError:
                    await query.edit_message_text("Invalid outcome selection.", parse_mode="HTML")
                    return
                if outcome_index < 0 or outcome_index >= len(outcomes):
                    await query.edit_message_text("That outcome selection is no longer available.", parse_mode="HTML")
                    return
                outcome = outcomes[outcome_index]
                _set_active_market_context(context, candidate)
                _set_trade_selection(context, candidate, outcome_id=_first_string(outcome.get("outcome_id"), default=""), outcome_label=_first_string(outcome.get("label"), default=""))
                details = _trade_selection_text(candidate, selected_outcome_label=_first_string(outcome.get("label"), default=""))
                preview, keyboard = _prepare_detail_view(
                    context,
                    prefix="watch",
                    identifier=_first_string(candidate.get("event_id"), candidate.get("market_id"), default=view_key),
                    full_text=details,
                    back_callback="watch:refresh",
                    back_label="Refresh list",
                    extra_rows=_trade_keyboard_rows(candidate, view_key=view_key, selected_outcome_id=_first_string(outcome.get("outcome_id"), default="")),
                )
                await query.edit_message_text(preview, reply_markup=keyboard, parse_mode="HTML")
                return

            if prefix == "trades":
                side = _normalize_text(selected_value).lower()
                if side not in {"buy", "sell"}:
                    await query.edit_message_text("Invalid trade side.", parse_mode="HTML")
                    return
                selected_trade = _active_trade_selection(context)
                outcome_id = _first_string(selected_trade.get("outcome_id") if isinstance(selected_trade, dict) else "", default="")
                outcome_label = _first_string(selected_trade.get("outcome_label") if isinstance(selected_trade, dict) else "", default="")
                if not outcome_id:
                    outcome_id = _resolve_order_outcome_id(candidate, side=side, selected_trade=selected_trade)
                _set_active_market_context(context, candidate)
                _set_trade_selection(context, candidate, outcome_id=outcome_id, outcome_label=outcome_label, side=side)
                _set_trade_order_state(context, candidate, outcome_id=outcome_id, outcome_label=outcome_label, side=side, stage="currency")
                details = _trade_selection_text(candidate, selected_outcome_label=outcome_label, selected_side=side)
                preview, keyboard = _prepare_detail_view(
                    context,
                    prefix="watch",
                    identifier=_first_string(candidate.get("event_id"), candidate.get("market_id"), default=view_key),
                    full_text=details + chr(10) + "Choose a currency to continue.",
                    back_callback="watch:refresh",
                    back_label="Refresh list",
                    extra_rows=[[InlineKeyboardButton("NGN", callback_data=f"tradec:{view_key}:NGN"), InlineKeyboardButton("USD", callback_data=f"tradec:{view_key}:USD")]],
                )
                await query.edit_message_text(preview, reply_markup=keyboard, parse_mode="HTML")
                return

        if prefix == "tradec":
            callback_parts = data.split(":", 2)
            tradec_candidate: Optional[dict[str, Any]] = None
            if len(callback_parts) == 3:
                _, view_key, currency_raw = callback_parts
                trade_view = _trade_view_bucket(context).get(view_key)
                if isinstance(trade_view, dict) and isinstance(trade_view.get("candidate"), dict):
                    tradec_candidate = trade_view["candidate"]
            elif len(callback_parts) == 2:
                _, currency_raw = callback_parts
            else:
                await query.edit_message_text("Invalid currency selection.", parse_mode="HTML")
                return
            if not isinstance(tradec_candidate, dict):
                tradec_candidate = _trade_context_candidate(context)
            if not isinstance(tradec_candidate, dict):
                await query.edit_message_text("No active trade context. Please select a market first.", parse_mode="HTML")
                return
            currency = _normalize_text(currency_raw).upper()
            if currency not in {"NGN", "USD"}:
                await query.edit_message_text("Invalid currency selection.", parse_mode="HTML")
                return
            state = _active_trade_order_state(context) or {}
            selected_trade = _active_trade_selection(context)
            outcome_id = _first_string(state.get("outcome_id") if isinstance(state, dict) else "", default="")
            outcome_label = _first_string(state.get("outcome_label") if isinstance(state, dict) else (selected_trade.get("outcome_label") if isinstance(selected_trade, dict) else ""), default="")
            side = _normalize_text(state.get("side") if isinstance(state, dict) else (selected_trade.get("side") if isinstance(selected_trade, dict) else "")).lower()
            _set_active_market_context(context, tradec_candidate)
            _set_trade_order_state(context, tradec_candidate, outcome_id=outcome_id, outcome_label=outcome_label, side=side, currency=currency, stage="amount")
            prompt = f"Active market: {_safe_html(tradec_candidate.get('event_title') or '')} · {_safe_html(tradec_candidate.get('market_title') or '')}\nCurrency: {_safe_html(currency)}\nSend the amount now."
            _set_pending_interaction(context, "trade_amount", prompt=prompt)
            await query.edit_message_text(prompt, parse_mode="HTML")
            return

        if prefix == "quote":
            if selected == "refresh":
                term = context.user_data.get("quote_search_term")
                if not term:
                    await query.edit_message_text("No quote search is active yet.", parse_mode="HTML")
                    return
                result = build_quote_command(client, f"quote {term}")
                if not result.ok:
                    await query.edit_message_text(result.text, parse_mode="HTML")
                    return
                candidates = result.raw.get("quote_candidates", []) if result.raw else []
                context.user_data["quote_candidates"] = candidates
                context.user_data["quote_search_term"] = term
                await query.edit_message_text(result.text, reply_markup=_quote_keyboard(candidates), parse_mode="HTML")
                return

            try:
                index = int(selected)
            except ValueError:
                await query.edit_message_text("Invalid quote selection.", parse_mode="HTML")
                return

            candidates = context.user_data.get("quote_candidates", []) or []
            if index < 0 or index >= len(candidates):
                await query.edit_message_text("That quote selection is no longer available.", parse_mode="HTML")
                return

            candidate = candidates[index]
            _set_active_market_context(context, candidate)
            context.user_data["active_quote"] = candidate

            quote_response = None
            market_id = candidate.get("market_id")
            if market_id:
                try:
                    quote_payload = client.get_ticker(market_id)
                    quote_response = QuoteResponse.from_dict(quote_payload)
                except Exception:
                    quote_response = None

            details = _selected_quote_text(candidate, quote_response)
            preview, keyboard = _prepare_detail_view(
                context,
                prefix="quote",
                identifier=_first_string(candidate.get("market_id"), candidate.get("event_id"), default=str(index)),
                full_text=details,
                back_callback="quote:refresh",
                back_label="Back to search",
            )
            await query.edit_message_text(preview, reply_markup=keyboard, parse_mode="HTML")
            return

        if selected == "refresh":
            result = build_watchlist_command(client, text=context.user_data.get("watch_query", ""))
            if not result.ok:
                await query.edit_message_text(result.text, parse_mode="HTML")
                return
            events = result.raw.get("events", []) if result.raw else []
            await query.edit_message_text(result.text, reply_markup=_watchlist_keyboard(events), parse_mode="HTML")
            return

        if prefix in {"fund", "withdraw"}:
            asset = selected.upper()
            result = build_fund_command(client, asset) if prefix == "fund" else build_withdraw_command(client, asset)
            if result.ok:
                _clear_pending_interaction(context)
            await query.edit_message_text(result.text, parse_mode="HTML")
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
        markets = _event_markets(event)
        candidate = _candidate_from_event_market(event, markets[0]) if markets else None
        if candidate:
            _set_active_market_context(context, candidate)
            _clear_trade_selection(context)
            details = _trade_selection_text(candidate)
            view_key = _trade_view_key(candidate)
            _trade_view_bucket(context)[view_key] = {"candidate": candidate}
            trade_rows = _trade_keyboard_rows(candidate, view_key=view_key)
            preview, keyboard = _prepare_detail_view(
                context,
                prefix="watch",
                identifier=_first_string(event.get("id"), event.get("eventId"), default=selected),
                full_text=details,
                back_callback="watch:refresh",
                back_label="Refresh list",
                extra_rows=trade_rows,
            )
        else:
            context.user_data["active_event"] = event
            context.user_data["active_market"] = None
            context.user_data["active_market_candidate"] = None
            details = _event_details_text(event, heading="Watching")
            preview, keyboard = _prepare_detail_view(
                context,
                prefix="watch",
                identifier=_first_string(event.get("id"), event.get("eventId"), default=selected),
                full_text=details,
                back_callback="watch:refresh",
                back_label="Refresh list",
            )
        await query.edit_message_text(preview, reply_markup=keyboard, parse_mode="HTML")
        direction = _event_direction(event)
        if direction:
            await send_scenario_sticker(query.message, direction)

    return handler
