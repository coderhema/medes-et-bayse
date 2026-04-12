"""Telegram bot handler for medes-et-bayse.

Provides outbound notifications and inbound commands:
  /start, /help, /status, /balance, /portfolio, /events, /quote, /order
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from typing import Any, Optional
from urllib import error, request

from loguru import logger

try:
    from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError as exc:
    raise ImportError("python-telegram-bot is required. Install it with: pip install python-telegram-bot") from exc

DEFAULT_CHAT_ID = "6433282551"
DEFAULT_SUCCESS_STICKER_SET = "MedesEtBayse"


SMART_TRADE_SIDE_RE = re.compile(r"\b(buy|sell)\b", re.IGNORECASE)
SMART_TRADE_AMOUNT_RE = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)(?!\w)")
SMART_TRADE_CURRENCY_RE = re.compile(r"\b(NGN|USD)\b", re.IGNORECASE)
DETAIL_PREVIEW_LINE_LIMIT = 6


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _outcome_label(side_or_label: str) -> str:
    """Normalize a side or outcome label to 'YES' or 'NO'."""
    normalized = _normalize_text(side_or_label).upper()
    return "YES" if normalized in {"YES", "BUY", "LONG"} else "NO"


def _detail_store(context: Any) -> dict[str, str]:
    if context is None:
        return {}
    data = getattr(context, "user_data", None)
    if not isinstance(data, dict):
        return {}
    store = data.get("detail_views")
    if not isinstance(store, dict):
        store = {}
        data["detail_views"] = store
    return store


def _detail_key(prefix: str, identifier: str) -> str:
    return hashlib.sha1(f"{prefix}:{identifier}".encode("utf-8")).hexdigest()[:12]


def _detail_preview(full_text: str, limit_lines: int = DETAIL_PREVIEW_LINE_LIMIT) -> str:
    lines = (full_text or "").split(chr(10))
    if len(lines) <= limit_lines:
        return full_text
    preview = lines[:limit_lines]
    preview.append("Tap View more for the full details.")
    return chr(10).join(preview)


def _detail_keyboard(view_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("View more", callback_data=f"more:{view_key}")]])


def _brain_parse_trade_intent(text: str, active_context: dict[str, Any]) -> dict[str, Any]:
    brain_url = os.getenv("POKE_BRAIN_URL", "").strip() or os.getenv("POKE_API_BRAIN_URL", "").strip()
    api_key = os.getenv("POKE_API_KEY", "").strip()
    prompt = {
        "task": "parse_short_trade_reply",
        "text": text,
        "active_context": active_context,
        "expected_shape": {
            "side": "buy|sell",
            "amount": 700,
            "currency": "NGN|USD",
            "outcome_id": "optional",
            "normalized_currency": "market currency if conversion is needed",
        },
    }

    if brain_url:
        try:
            body = json.dumps(prompt).encode("utf-8")
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            req = request.Request(brain_url, data=body, headers=headers, method="POST")
            with request.urlopen(req, timeout=12) as resp:
                payload = resp.read().decode("utf-8")
            if payload:
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    result = parsed.get("result")
                    if isinstance(result, dict):
                        return result
                    return parsed
        except Exception:
            pass

    normalized = _normalize_text(text).lower()
    side_match = SMART_TRADE_SIDE_RE.search(normalized)
    amount_match = SMART_TRADE_AMOUNT_RE.search(normalized)
    currency_match = SMART_TRADE_CURRENCY_RE.search(normalized)
    if not side_match or not amount_match:
        return {}
    side = side_match.group(1).lower()
    amount = float(amount_match.group(1))
    currency = currency_match.group(1).upper() if currency_match else _normalize_text(active_context.get("currency") or "USD").upper()
    return {"side": side, "amount": amount, "currency": currency, "outcome_id": active_context.get("outcome_id")}


class TelegramHandler:
    def __init__(
        self,
        token: str,
        chat_id: str = DEFAULT_CHAT_ID,
        bayse_client=None,
        bot_status_callback=None,
        success_sticker_set: str = DEFAULT_SUCCESS_STICKER_SET,
        success_sticker_file_id: Optional[str] = None,
    ):
        self.token = token
        self.chat_id = chat_id
        self.bayse_client = bayse_client
        self._bot_status_callback = bot_status_callback
        self.success_sticker_set = success_sticker_set.strip()
        self.success_sticker_file_id = success_sticker_file_id.strip() if success_sticker_file_id else None
        self._sticker_cache: dict[str, str] = {}
        self._app: Optional[Application] = None

    def attach_bayse_client(self, bayse_client) -> None:
        self.bayse_client = bayse_client

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        try:
            bot = Bot(token=self.token)
            async with bot:
                await bot.send_message(chat_id=self.chat_id, text=text, parse_mode=parse_mode)
            logger.info(f"Telegram message sent: {text[:80]}..." if len(text) > 80 else f"Telegram message sent: {text}")
            return True
        except Exception as e:
            logger.error(f"Telegram send_message failed: {e}")
            return False

    def send_message_sync(self, text: str, parse_mode: str = "HTML") -> bool:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.send_message(text, parse_mode))
                return True
            return loop.run_until_complete(self.send_message(text, parse_mode))
        except RuntimeError:
            return asyncio.run(self.send_message(text, parse_mode))

    async def _resolve_sticker_file_id(self, sticker_file_id: Optional[str] = None, sticker_set_name: Optional[str] = None) -> Optional[str]:
        if sticker_file_id:
            return sticker_file_id

        sticker_set_name = (sticker_set_name or self.success_sticker_set or '').strip()
        if not sticker_set_name:
            return None

        cached = self._sticker_cache.get(sticker_set_name)
        if cached:
            return cached

        try:
            bot = Bot(token=self.token)
            async with bot:
                sticker_set = await bot.get_sticker_set(sticker_set_name)
            stickers = getattr(sticker_set, 'stickers', None) or []
            if not stickers:
                logger.warning(f'No stickers found in pack: {sticker_set_name}')
                return None
            resolved = stickers[0].file_id
            self._sticker_cache[sticker_set_name] = resolved
            return resolved
        except Exception as e:
            logger.warning(f'Failed to resolve sticker pack {sticker_set_name}: {e}')
            return None

    async def send_sticker(self, sticker_file_id: Optional[str] = None, sticker_set_name: Optional[str] = None) -> bool:
        try:
            resolved = await self._resolve_sticker_file_id(sticker_file_id=sticker_file_id, sticker_set_name=sticker_set_name)
            if not resolved:
                return False
            bot = Bot(token=self.token)
            async with bot:
                await bot.send_sticker(chat_id=self.chat_id, sticker=resolved)
            logger.info('Telegram sticker sent successfully')
            return True
        except Exception as e:
            logger.error(f'Telegram send_sticker failed: {e}')
            return False

    def send_sticker_sync(self, sticker_file_id: Optional[str] = None, sticker_set_name: Optional[str] = None) -> bool:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.send_sticker(sticker_file_id=sticker_file_id, sticker_set_name=sticker_set_name))
                return True
            return loop.run_until_complete(self.send_sticker(sticker_file_id=sticker_file_id, sticker_set_name=sticker_set_name))
        except RuntimeError:
            return asyncio.run(self.send_sticker(sticker_file_id=sticker_file_id, sticker_set_name=sticker_set_name))

    async def send_notification(self, text: str, level: str = 'info', parse_mode: str = 'HTML') -> bool:
        message_ok = await self.send_message(text, parse_mode=parse_mode)
        sticker_ok = False
        if level == 'success':
            sticker_ok = await self.send_sticker()
        return message_ok or sticker_ok

    def send_notification_sync(self, text: str, level: str = 'info', parse_mode: str = 'HTML') -> bool:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.send_notification(text, level=level, parse_mode=parse_mode))
                return True
            return loop.run_until_complete(self.send_notification(text, level=level, parse_mode=parse_mode))
        except RuntimeError:
            return asyncio.run(self.send_notification(text, level=level, parse_mode=parse_mode))

    async def send_signal(self, event_title: str, side: str, edge: float, stake: float, dry_run: bool = False) -> bool:
        label = "[DRY RUN] " if dry_run else ""
        text = (
            f"<b>{label}Trading Signal</b>\n"
            f"Market: {event_title}\n"
            f"Side: <b>{side}</b>\n"
            f"Edge: {edge:.2%}\n"
            f"Stake: ${stake:.2f}"
        )
        return await self.send_message(text)

    async def send_alert(self, message: str, level: str = "info") -> bool:
        emoji = {"info": "ℹ️", "success": "✅", "error": "❌"}.get(level, "🔔")
        text = f"{emoji} <b>medes-et-bayse</b>\n{message}"
        return await self.send_notification(text, level=level)

    def _require_client(self):
        if self.bayse_client is None:
            raise RuntimeError("Bayse client not configured")
        return self.bayse_client

    def _store_active_context(self, context: Any, *, event_id: str, market_id: str, outcome_id: str, currency: str, side: str = "BUY") -> None:
        if context is None:
            return
        data = getattr(context, "user_data", None)
        if not isinstance(data, dict):
            return
        ctx = {
            "event_id": event_id,
            "market_id": market_id,
            "outcome_id": outcome_id,
            "currency": currency,
            "side": side.upper(),
        }
        ctx["eventId"] = event_id
        ctx["marketId"] = market_id
        ctx["outcomeId"] = outcome_id
        ctx["normalizedCurrency"] = currency
        data["active_trade_context"] = ctx

    def _active_context(self, context: Any) -> dict[str, Any]:
        data = getattr(context, "user_data", None)
        if not isinstance(data, dict):
            return {}
        value = data.get("active_trade_context")
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _sync_trade_context_aliases(ctx: dict[str, Any]) -> None:
        """Keep snake_case and camelCase identifiers aligned for all state machine hops."""
        alias_map = {
            "event_id": "eventId",
            "market_id": "marketId",
            "outcome_id": "outcomeId",
            "currency": "normalizedCurrency",
            "side": "tradeSide",
        }
        for snake_key, camel_key in alias_map.items():
            value = ctx.get(snake_key)
            if value not in (None, ""):
                ctx[camel_key] = value
        if ctx.get("eventId") not in (None, "") and not ctx.get("event_id"):
            ctx["event_id"] = ctx["eventId"]
        if ctx.get("marketId") not in (None, "") and not ctx.get("market_id"):
            ctx["market_id"] = ctx["marketId"]
        if ctx.get("outcomeId") not in (None, "") and not ctx.get("outcome_id"):
            ctx["outcome_id"] = ctx["outcomeId"]
        if ctx.get("normalizedCurrency") not in (None, "") and not ctx.get("currency"):
            ctx["currency"] = ctx["normalizedCurrency"]
        if ctx.get("tradeSide") not in (None, "") and not ctx.get("side"):
            ctx["side"] = ctx["tradeSide"]

    def _normalize_trade_context(self, context: Any) -> dict[str, Any]:
        ctx = dict(self._active_context(context))
        if not ctx:
            return {}
        self._sync_trade_context_aliases(ctx)
        data = getattr(context, "user_data", None)
        if isinstance(data, dict):
            data["active_trade_context"] = ctx
        return ctx

    def _resolve_trade_context(self, context: Any, **overrides: Any) -> dict[str, Any]:
        """Return the active trade context with any non-empty overrides merged in."""
        ctx = self._normalize_trade_context(context)
        for key, value in overrides.items():
            if value not in (None, ""):
                ctx[key] = value
        if ctx:
            self._sync_trade_context_aliases(ctx)
            data = getattr(context, "user_data", None)
            if isinstance(data, dict):
                data["active_trade_context"] = ctx
        return ctx

    @staticmethod
    def _trade_context_ready(ctx: dict[str, Any]) -> bool:
        return all(_normalize_text(ctx.get(key)) for key in ("event_id", "market_id", "outcome_id", "currency"))

    def _update_active_context(self, context: Any, **kwargs: Any) -> None:
        """Update individual fields of the active trade context without replacing the whole dict."""
        data = getattr(context, "user_data", None)
        if not isinstance(data, dict):
            return
        ctx = data.get("active_trade_context")
        if not isinstance(ctx, dict):
            ctx = {}
            data["active_trade_context"] = ctx
        ctx.update(kwargs)
        self._sync_trade_context_aliases(ctx)

    @staticmethod
    def _event_markets(event: dict) -> list[dict]:
        """Extract the markets list from an event dict, handling varied response shapes."""
        for key in ("markets", "market"):
            val = event.get(key)
            if isinstance(val, list):
                return val
        return []

    @staticmethod
    def _market_outcomes(market: dict) -> list[dict]:
        """Extract the outcomes list from a market dict, handling varied response shapes."""
        for key in ("outcomes", "outcome", "options"):
            val = market.get(key)
            if isinstance(val, list):
                return val
        return []

    def _get_event_cache(self, context: Any) -> dict[str, dict]:
        """Return the cached event data dict from user_data."""
        ud = getattr(context, "user_data", {})
        return ud.get("_event_cache") or {}

    def _format_with_view_more(self, context: Any, text: str, *, view_key: str) -> tuple[str, Optional[InlineKeyboardMarkup]]:
        preview = _detail_preview(text)
        if preview == text:
            return text, None
        store = _detail_store(context)
        store[view_key] = text
        return preview, _detail_keyboard(view_key)

    @staticmethod
    def _parse_tokens(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
        named: dict[str, str] = {}
        positional: list[str] = []
        for token in tokens:
            if "=" in token:
                key, value = token.split("=", 1)
                named[key.strip().lower().replace("-", "_")] = value.strip()
            else:
                positional.append(token)
        return named, positional

    @staticmethod
    def _first_value(named: dict[str, str], positional: list[str], keys: list[str], index: int, default: str = "") -> str:
        for key in keys:
            value = named.get(key, "").strip()
            if value:
                return value
        if index < len(positional):
            return positional[index]
        return default

    @staticmethod
    def _fmt_money(value) -> str:
        try:
            return f"{float(value):,.2f}"
        except Exception:
            return str(value)

    @staticmethod
    def _fmt_float(value) -> str:
        try:
            return f"{float(value):,.4f}"
        except Exception:
            return str(value)

    def _format_events(self, events: list[dict], limit: int = 10) -> str:
        if not events:
            return "I couldn’t find any open markets right now. Try /events again in a bit, or ask me to search for a keyword like 'bitcoin'."
        lines = []
        for event in events[:limit]:
            title = event.get("title") or event.get("name") or "Untitled market"
            event_id = event.get("id", "unknown")
            status = event.get("status", "open")
            lines.append(f"• {title}\n  id: {event_id}\n  status: {status}")
        return "\n".join(lines)

    def _format_balance(self, assets: list[dict]) -> str:
        if not assets:
            return "No wallet assets found."
        lines = []
        for asset in assets:
            symbol = asset.get("symbol", "?")
            available = self._fmt_money(asset.get("availableBalance", 0))
            pending = self._fmt_money(asset.get("pendingBalance", 0))
            network = asset.get("network", "n/a")
            lines.append(f"• {symbol}: available {available}, pending {pending} ({network})")
        return "\n".join(lines)

    def _format_portfolio(self, portfolio) -> str:
        if isinstance(portfolio, dict):
            positions = portfolio.get("outcomeBalances") or portfolio.get("data") or portfolio.get("positions") or []
            total_cost = portfolio.get("portfolioCost")
            total_value = portfolio.get("portfolioCurrentValue")
            pct_change = portfolio.get("portfolioPercentageChange")
        else:
            positions = portfolio or []
            total_cost = total_value = pct_change = None

        if not positions:
            return "No open positions found."

        lines = []
        for pos in positions:
            market = pos.get("market", {})
            event = market.get("event", {})
            title = event.get("title") or market.get("title") or "Unknown market"
            outcome = pos.get("outcome", pos.get("outcomeId", "?"))
            balance = self._fmt_money(pos.get("balance", 0))
            current_value = self._fmt_money(pos.get("currentValue", 0))
            avg_price = self._fmt_float(pos.get("averagePrice", 0))
            lines.append(f"• {title} [{outcome}]\n  balance: {balance}\n  avg price: {avg_price}\n  current value: {current_value}")

        summary = []
        if total_cost is not None:
            summary.append(f"Cost: {self._fmt_money(total_cost)}")
        if total_value is not None:
            summary.append(f"Value: {self._fmt_money(total_value)}")
        if pct_change is not None:
            summary.append(f"PnL: {self._fmt_money(pct_change)}%")
        if summary:
            lines.append("\n" + " | ".join(summary))
        return "\n".join(lines)


    @staticmethod
    def _market_query_terms(text: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        stop_words = {
            "a",
            "an",
            "and",
            "are",
            "can",
            "check",
            "do",
            "for",
            "from",
            "get",
            "got",
            "have",
            "hey",
            "hi",
            "how",
            "is",
            "list",
            "looking",
            "me",
            "of",
            "on",
            "open",
            "please",
            "search",
            "show",
            "there",
            "the",
            "to",
            "up",
            "want",
            "what",
            "which",
        }
        market_words = {
            "event",
            "events",
            "market",
            "markets",
            "bitcoin",
            "btc",
            "ethereum",
            "eth",
            "solana",
            "doge",
            "dogecoin",
            "crypto",
            "usdc",
            "usdt",
        }
        return [token for token in tokens if token not in stop_words and token not in market_words]

    @staticmethod
    def _looks_like_market_intent(text: str) -> bool:
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        if not tokens:
            return False
        trigger_words = {
            "any",
            "browse",
            "check",
            "event",
            "events",
            "find",
            "is",
            "list",
            "market",
            "markets",
            "open",
            "search",
            "show",
            "there",
            "what",
            "which",
        }
        topic_words = {
            "bitcoin",
            "btc",
            "ethereum",
            "eth",
            "solana",
            "doge",
            "dogecoin",
            "crypto",
            "usdc",
            "usdt",
        }
        return any(token in trigger_words or token in topic_words for token in tokens)

    @staticmethod
    def _event_search_blob(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, default=str).lower()
        except Exception:
            return str(value).lower()

    def _format_market_results(self, events: list[dict], query: str = "") -> tuple[str, Optional[InlineKeyboardMarkup]]:
        query = query.strip()
        query_terms = self._market_query_terms(query) if query else []
        lines: list[str] = []
        rows: list[list[InlineKeyboardButton]] = []
        total_matches = 0

        for event in events:
            if not isinstance(event, dict):
                continue
            title = event.get("title") or event.get("name") or "Untitled market"
            event_id = str(event.get("id") or event.get("eventId") or "").strip()
            blob = self._event_search_blob(event)
            if query_terms and not any(term in blob for term in query_terms):
                continue

            total_matches += 1
            if len(lines) < 10:
                matched_markets: list[str] = []
                if query_terms:
                    for market in self._event_markets(event):
                        market_blob = self._event_search_blob(market)
                        if any(term in market_blob for term in query_terms):
                            market_title = market.get("title") or market.get("name") or market.get("label") or market.get("id") or "Untitled market"
                            matched_markets.append(str(market_title))

                line = f"• {title}\n  id: {event_id or 'unknown'}"
                if query_terms and matched_markets:
                    line += f"\n  matching markets: {', '.join(matched_markets[:3])}"
                lines.append(line)
                if event_id:
                    rows.append([InlineKeyboardButton(str(title), callback_data=f"event:{event_id}")])

        if not lines:
            if query:
                return (
                    f"I couldn’t find any open markets for '{query}' right now. Try /events to browse everything, or send another keyword.",
                    None,
                )
            return (
                "I couldn’t find any open markets right now. Try /events again in a bit, or ask me to search for a keyword like 'bitcoin'.",
                None,
            )

        header = (
            f"I found {total_matches} open market(s) mentioning '{query}':"
            if query
            else "Here are the open markets I found:"
        )
        if total_matches > len(lines):
            header += f"\nShowing the first {len(lines)} results."
        return header + "\n" + "\n".join(lines), InlineKeyboardMarkup(rows) if rows else None

    async def _show_market_catalog(self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str = "") -> None:
        client = self._require_client()
        query = query.strip()
        try:
            events = await asyncio.to_thread(client.get_open_events, 1, 50)
            text, keyboard = self._format_market_results(events, query=query)
            await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception as e:
            if query:
                fallback = f'I hit a snag while searching for "{query}". Try /events again in a moment.'
            else:
                fallback = "I hit a snag while checking open markets. Try /events again in a moment."
            await update.message.reply_text(fallback, parse_mode="HTML")

    async def _cmd_markets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = " ".join(context.args).strip() if context.args else ""
        await self._show_market_catalog(update, context, query=query)

    @staticmethod
    def _format_quote(quote: dict, event_id: str, market_id: str, side: str, outcome_id: str, amount: float, currency: str) -> str:
        return (
            f"<b>Quote</b>\n"
            f"Event: {event_id}\n"
            f"Market: {market_id}\n"
            f"Side: <b>{side.upper()}</b>\n"
            f"Outcome: {outcome_id}\n"
            f"Amount: {amount:.2f} {currency}\n"
            f"Price: {float(quote.get('price', 0)):.4f}\n"
            f"Current market price: {float(quote.get('currentMarketPrice', 0)):.4f}\n"
            f"Quantity: {float(quote.get('quantity', 0)):.2f}\n"
            f"Cost of shares: {float(quote.get('costOfShares', 0)):.2f}\n"
            f"Fee: {float(quote.get('fee', 0)):.2f}\n"
            f"Complete fill: {bool(quote.get('completeFill', False))}"
        )

    @staticmethod
    def _format_order(result: dict) -> str:
        order = result.get("order", result)
        engine = result.get("engine", "unknown")
        return (
            f"<b>Order placed</b>\n"
            f"Engine: {engine}\n"
            f"Order ID: {order.get('id', 'unknown')}\n"
            f"Status: {order.get('status', 'unknown')}\n"
            f"Side: {order.get('side', 'unknown')}\n"
            f"Type: {order.get('type', 'unknown')}\n"
            f"Outcome: {order.get('outcome', 'unknown')}\n"
            f"Price: {float(order.get('price', 0)):.4f}\n"
            f"Quantity: {float(order.get('quantity', 0)):.2f}\n"
            f"Amount: {float(order.get('amount', 0)):.2f} {order.get('currency', 'USD')}"
        )

    def _usage_quote(self) -> str:
        return (
            "Usage:\n"
            "/quote event_id=&lt;uuid&gt; market_id=&lt;uuid&gt; side=BUY outcome_id=&lt;uuid&gt; amount=100 currency=USD\n"
            "or positional: /quote <event_id> <market_id> <side> <outcome_id> <amount> [currency]"
        )

    def _usage_order(self) -> str:
        return (
            "Usage:\n"
            "/order event_id=&lt;uuid&gt; market_id=&lt;uuid&gt; side=BUY outcome_id=&lt;uuid&gt; amount=100 currency=USD type=MARKET price=0.72\n"
            "Optional: time_in_force=GTC post_only=false max_slippage=0.02 expires_at=2026-03-28T12:00:00Z\n"
            "or positional: /order <event_id> <market_id> <side> <outcome_id> <amount> [currency] [type] [price]"
        )

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "👋 <b>medes-et-bayse bot online.</b>\n"
            "Type /help for the available Bayse commands.",
            parse_mode="HTML",
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "<b>Commands</b>\n"
            "/status — bot status\n"
            "/balance — wallet balances\n"
            "/portfolio — open positions\n"
            "/events — active markets\n"
            "/markets — search open markets by keyword\n"
            "/quote — price quote before an order\n"
            "/order — place a Bayse order\n"
            "You can also say things like 'show me events' or 'is there any bitcoin market'.\n\n"
            "Examples:\n"
            "/quote event_id=&lt;uuid&gt; market_id=&lt;uuid&gt; side=BUY outcome_id=&lt;uuid&gt; amount=100 currency=USD\n"
            "/order event_id=&lt;uuid&gt; market_id=&lt;uuid&gt; side=BUY outcome_id=&lt;uuid&gt; amount=100 type=MARKET currency=USD",
            parse_mode="HTML",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._bot_status_callback:
            try:
                status_text = self._bot_status_callback()
            except Exception as e:
                status_text = f"Error fetching status: {e}"
        else:
            status_text = "Bot is running. Telegram commands and Bayse polling are enabled."
        await update.message.reply_text(f"<b>Status</b>\n{status_text}", parse_mode="HTML")

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        client = self._require_client()
        try:
            assets = await asyncio.to_thread(client.get_balance)
            text = self._format_balance(assets if isinstance(assets, list) else [assets])
        except Exception as e:
            text = f"Error fetching balance: {e}"
        await update.message.reply_text(f"<b>Balance</b>\n{text}", parse_mode="HTML")

    async def _cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("NGN", callback_data="portfolio:NGN"),
                InlineKeyboardButton("USD", callback_data="portfolio:USD"),
            ]]
        )
        await update.message.reply_text(
            "<b>Portfolio</b>\nSelect currency to view your wallet balance:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def _cmd_events(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = " ".join(context.args).strip() if context.args else ""
        await self._show_market_catalog(update, context, query=query)

    async def _cmd_markets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = " ".join(context.args).strip() if context.args else ""
        await self._show_market_catalog(update, context, query=query)

    async def _cmd_quote(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        client = self._require_client()
        named, positional = self._parse_tokens(context.args)
        event_id = self._first_value(named, positional, ["event_id", "eventid"], 0)
        market_id = self._first_value(named, positional, ["market_id", "marketid"], 1)
        side = self._first_value(named, positional, ["side"], 2, "BUY")
        outcome_id = self._first_value(named, positional, ["outcome_id", "outcomeid"], 3)
        amount_raw = self._first_value(named, positional, ["amount"], 4)
        currency = self._first_value(named, positional, ["currency"], 5, "USD")

        if not all([event_id, market_id, side, outcome_id, amount_raw]):
            await update.message.reply_text(self._usage_quote(), parse_mode="HTML")
            return

        try:
            amount = float(amount_raw)
            quote = await asyncio.to_thread(client.get_quote, event_id, market_id, side, outcome_id, amount, currency)
            text = self._format_quote(quote, event_id, market_id, side, outcome_id, amount, currency)
            self._store_active_context(context, event_id=event_id, market_id=market_id, outcome_id=outcome_id, currency=currency, side=side)
            view_key = _detail_key("quote", f"{event_id}:{market_id}:{outcome_id}:{amount}:{currency}")
            text, keyboard = self._format_with_view_more(context, text, view_key=view_key)
        except Exception as e:
            text = f"Error fetching quote: {e}\n\n{self._usage_quote()}"
            keyboard = None
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

    async def _cmd_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        client = self._require_client()
        named, positional = self._parse_tokens(context.args)
        event_id = self._first_value(named, positional, ["event_id", "eventid"], 0)
        market_id = self._first_value(named, positional, ["market_id", "marketid"], 1)
        side = self._first_value(named, positional, ["side"], 2, "BUY")
        outcome_id = self._first_value(named, positional, ["outcome", "outcome_id", "outcomeid"], 3)
        amount_raw = self._first_value(named, positional, ["amount"], 4)
        currency = self._first_value(named, positional, ["currency"], 5, "USD")
        order_type = self._first_value(named, positional, ["type", "order_type"], 6, "MARKET")
        price_raw = self._first_value(named, positional, ["price"], 7)
        time_in_force = self._first_value(named, positional, ["time_in_force", "tif"], 8)
        post_only_raw = self._first_value(named, positional, ["post_only", "postonly"], 9)
        max_slippage_raw = self._first_value(named, positional, ["max_slippage", "maxslippage"], 10)
        expires_at = self._first_value(named, positional, ["expires_at", "expiresat"], 11)

        if not all([event_id, market_id, side, outcome_id, amount_raw]):
            await update.message.reply_text(self._usage_order(), parse_mode="HTML")
            return

        try:
            amount = float(amount_raw)
            price = float(price_raw) if price_raw else None
            post_only = None
            if post_only_raw:
                post_only = post_only_raw.lower() in {"1", "true", "yes", "y"}
            max_slippage = float(max_slippage_raw) if max_slippage_raw else None
            outcome = _outcome_label(outcome_id or side)

            result = await asyncio.to_thread(
                client.place_order,
                event_id,
                market_id,
                side,
                outcome,
                amount,
                currency,
                order_type,
                price,
                time_in_force or None,
                post_only,
                max_slippage,
                expires_at or None,
            )
            text = self._format_order(result)
            self._store_active_context(context, event_id=event_id, market_id=market_id, outcome_id=outcome_id, currency=currency, side=side)
            view_key = _detail_key("order", f"{event_id}:{market_id}:{outcome}:{amount}:{currency}:{order_type}")
            text, keyboard = self._format_with_view_more(context, text, view_key=view_key)
        except Exception as e:
            text = f"Error placing order: {e}\n\n{self._usage_order()}"
            keyboard = None
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

    async def _cmd_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message or update.message
        if message is None:
            return
        text = getattr(message, "text", "") or ""
        if text.startswith("/"):
            return

        ud = getattr(context, "user_data", {})
        pending = ud.get("pending_action")

        if pending == "awaiting_amount":
            amount_match = SMART_TRADE_AMOUNT_RE.search(text.strip())
            if not amount_match:
                await message.reply_text("Please enter a numeric amount, e.g. 500", parse_mode="HTML")
                return
            amount = float(amount_match.group(1))
            active = self._resolve_trade_context(context)
            if not self._trade_context_ready(active):
                if isinstance(ud, dict):
                    ud.pop("pending_action", None)
                await message.reply_text(
                    "Context lost. Please start again with /events.", parse_mode="HTML"
                )
                return
            if isinstance(ud, dict):
                ud.pop("pending_action", None)
            side = active.get("side", "BUY").upper()
            try:
                result = await asyncio.to_thread(
                    self._require_client().place_order,
                    active["event_id"],
                    active["market_id"],
                    side,
                    _outcome_label(side),
                    amount,
                    active["currency"],
                    "MARKET",
                    None, None, None, None, None,
                )
                reply_text = self._format_order(result)
                view_key = _detail_key(
                    "buy",
                    f"{active['event_id']}:{active['market_id']}:{_outcome_label(side)}:{amount}:{active['currency']}",
                )
                reply_text, keyboard = self._format_with_view_more(context, reply_text, view_key=view_key)
            except Exception as e:
                reply_text, keyboard = f"Error placing order: {e}", None
            await message.reply_text(reply_text, reply_markup=keyboard, parse_mode="HTML")
            return

        if self._looks_like_market_intent(text):
            query = " ".join(self._market_query_terms(text)).strip()
            await self._show_market_catalog(update, context, query=query)
            return

        active = self._resolve_trade_context(context)
        parsed = _brain_parse_trade_intent(text, active)
        if not parsed:
            return
        if not self._trade_context_ready(active):
            await message.reply_text(
                "Pick a market first with /quote, then send something like ‘Buy, 700 NGN’.", parse_mode="HTML"
            )
            return
        side = _normalize_text(parsed.get("side") or active.get("side") or "buy").upper()
        try:
            amount = float(parsed.get("amount"))
        except (TypeError, ValueError):
            await message.reply_text(
                "I couldn’t read the amount. Try something like ‘Buy, 700 NGN’.", parse_mode="HTML"
            )
            return
        currency = _normalize_text(parsed.get("currency") or parsed.get("normalized_currency") or active.get("currency") or "USD").upper()
        outcome = _outcome_label(parsed.get("outcome") or parsed.get("side") or active.get("side") or side)
        try:
            result = await asyncio.to_thread(
                self._require_client().place_order,
                active["event_id"],
                active["market_id"],
                side,
                outcome,
                amount,
                currency,
                "MARKET",
                None,
                None,
                None,
                None,
                None,
            )
            text = self._format_order(result)
            self._store_active_context(context, event_id=active["event_id"], market_id=active["market_id"], outcome_id=outcome, currency=currency, side=side)
            view_key = _detail_key("smart-trade", f"{active['event_id']}:{active['market_id']}:{outcome_id}:{amount}:{currency}:{side}")
            text, keyboard = self._format_with_view_more(context, text, view_key=view_key)
        except Exception as e:
            text, keyboard = f"Error placing smart trade: {e}", None
        await message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

    async def _cb_event(self, query: Any, context: ContextTypes.DEFAULT_TYPE, event_id: str) -> None:
        """Handle user tapping an event button — load markets and show as buttons."""
        client = self._require_client()
        self._update_active_context(context, event_id=event_id)
        try:
            event = await asyncio.to_thread(client.get_event, event_id)
            ud = getattr(context, "user_data", {})
            if isinstance(ud, dict):
                ud.setdefault("_event_cache", {})[event_id] = event
            markets = self._event_markets(event)
            if not markets:
                await query.edit_message_text("No markets found for this event.", parse_mode="HTML")
                return
            title = event.get("title") or event.get("name") or event_id
            keyboard_rows = []
            for market in markets:
                market_id = market.get("id", "")
                market_title = market.get("title") or market.get("name") or market_id
                if market_id:
                    keyboard_rows.append([InlineKeyboardButton(market_title, callback_data=f"market:{event_id}:{market_id}")])
            keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
            await query.edit_message_text(
                f"<b>{title}</b>\nSelect a market:",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        except Exception as e:
            await query.edit_message_text(f"Error fetching event: {e}", parse_mode="HTML")

    async def _cb_market(self, query: Any, context: ContextTypes.DEFAULT_TYPE, market_id: str, event_id: str = "") -> None:
        """Handle user tapping a market button — show all outcomes as buttons."""
        event_cache = self._get_event_cache(context)
        resolved_event_id = _normalize_text(event_id)
        market: Optional[dict] = None

        if resolved_event_id and resolved_event_id in event_cache:
            for m in self._event_markets(event_cache[resolved_event_id]):
                if m.get("id") == market_id:
                    market = m
                    break

        if market is None:
            for ev in event_cache.values():
                for m in self._event_markets(ev):
                    if m.get("id") == market_id:
                        market = m
                        resolved_event_id = resolved_event_id or _normalize_text(ev.get("id", ""))
                        break
                if market:
                    break

        if market is None:
            await query.edit_message_text(
                "Market data not found. Please use /events to start fresh.", parse_mode="HTML"
            )
            return

        if not resolved_event_id:
            resolved_event_id = _normalize_text(market.get("event_id") or market.get("eventId") or "")

        active = self._resolve_trade_context(context, event_id=resolved_event_id, market_id=market_id)
        market_title = market.get("title") or market.get("name") or market_id
        outcomes = self._market_outcomes(market)
        if not outcomes:
            await query.edit_message_text(
                "I found the event, but it doesn’t have any markets I can show right now. Try /events to go back to the main list.",
                parse_mode="HTML",
            )
            return

        keyboard_rows = []
        for outcome in outcomes:
            outcome_id = outcome.get("id", "")
            outcome_title = (
                outcome.get("title") or outcome.get("name") or outcome.get("label") or outcome_id
            )
            if outcome_id:
                keyboard_rows.append([InlineKeyboardButton(outcome_title, callback_data=f"outcome:{active.get('event_id', resolved_event_id)}:{market_id}:{outcome_id}")])
        keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
        await query.edit_message_text(
            f"<b>{market_title}</b>\nSelect an outcome:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def _cb_outcome(self, query: Any, context: ContextTypes.DEFAULT_TYPE, outcome_id: str, event_id: str = "", market_id: str = "") -> None:
        """Handle user tapping an outcome button — show currency selection."""
        active = self._resolve_trade_context(context, event_id=event_id, market_id=market_id, outcome_id=outcome_id, side="BUY")
        if not self._trade_context_ready({**active, "currency": _normalize_text(active.get("currency") or "USD")}):
            await query.edit_message_text(
                "Context lost. Please use /events to start fresh.", parse_mode="HTML"
            )
            return

        resolved_event_id = _normalize_text(active.get("event_id"))
        resolved_market_id = _normalize_text(active.get("market_id"))
        resolved_outcome_id = _normalize_text(active.get("outcome_id"))

        outcome_title: Optional[str] = None
        for ev in self._get_event_cache(context).values():
            for m in self._event_markets(ev):
                if m.get("id") == resolved_market_id:
                    for o in self._market_outcomes(m):
                        if o.get("id") == resolved_outcome_id:
                            outcome_title = o.get("title") or o.get("name") or o.get("label")
                            break
                    break
            if outcome_title:
                break

        label = outcome_title or resolved_outcome_id
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("NGN", callback_data=f"currency:{resolved_event_id}:{resolved_market_id}:{resolved_outcome_id}:NGN"),
            InlineKeyboardButton("USD", callback_data=f"currency:{resolved_event_id}:{resolved_market_id}:{resolved_outcome_id}:USD"),
        ]])
        await query.edit_message_text(
            f"Outcome: <b>{label}</b>\nSelect currency:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def _cb_currency(self, query: Any, context: ContextTypes.DEFAULT_TYPE, currency: str, event_id: str = "", market_id: str = "", outcome_id: str = "") -> None:
        """Handle currency selection — prompt for amount."""
        currency = currency.upper()
        active = self._resolve_trade_context(context, event_id=event_id, market_id=market_id, outcome_id=outcome_id, currency=currency)
        if not self._trade_context_ready(active):
            ud = getattr(context, "user_data", {})
            if isinstance(ud, dict):
                ud.pop("pending_action", None)
            await query.edit_message_text(
                "Context lost. Please use /events to start fresh.",
                parse_mode="HTML",
            )
            return

        ud = getattr(context, "user_data", {})
        if isinstance(ud, dict):
            ud["pending_action"] = "awaiting_amount"
        await query.edit_message_text(
            f"Currency: <b>{currency}</b>\nHow much?",
            parse_mode="HTML",
        )

    async def _cb_portfolio(self, query: Any, context: ContextTypes.DEFAULT_TYPE, currency: str) -> None:
        """Handle portfolio currency selection — show wallet balance for that currency."""
        client = self._require_client()
        currency_up = currency.upper()
        try:
            assets = await asyncio.to_thread(client.get_balance)
            asset_list = assets if isinstance(assets, list) else [assets]
            matching = [a for a in asset_list if _normalize_text(a.get("symbol", "")).upper() == currency_up]
            text = self._format_balance(matching if matching else asset_list)
        except Exception as e:
            text = f"Error fetching balance: {e}"
        await query.edit_message_text(f"<b>Wallet Balance ({currency_up})</b>\n{text}", parse_mode="HTML")

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not getattr(query, "data", None):
            return
        data = str(query.data)
        await query.answer()
        if data.startswith("more:"):
            key = data.split(":", 1)[1]
            store = _detail_store(context)
            full = store.get(key)
            if not full:
                await query.edit_message_text("That expanded view is no longer available.", parse_mode="HTML")
                return
            await query.edit_message_text(full, parse_mode="HTML")
        elif data.startswith("event:"):
            await self._cb_event(query, context, data[len("event:"):])
        elif data.startswith("market:"):
            payload = data[len("market:"):]
            parts = payload.split(":")
            if len(parts) >= 2:
                await self._cb_market(query, context, parts[-1], event_id=parts[0])
            else:
                await self._cb_market(query, context, payload)
        elif data.startswith("outcome:"):
            payload = data[len("outcome:"):]
            parts = payload.split(":")
            if len(parts) >= 3:
                await self._cb_outcome(query, context, parts[-1], event_id=parts[0], market_id=parts[1])
            else:
                await self._cb_outcome(query, context, payload)
        elif data.startswith("currency:"):
            payload = data[len("currency:"):]
            parts = payload.split(":")
            if len(parts) >= 4:
                await self._cb_currency(query, context, parts[-1], event_id=parts[0], market_id=parts[1], outcome_id=parts[2])
            else:
                await self._cb_currency(query, context, payload)
        elif data.startswith("portfolio:"):
            await self._cb_portfolio(query, context, data[len("portfolio:"):])

    def build_application(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("balance", self._cmd_balance))
        app.add_handler(CommandHandler("portfolio", self._cmd_portfolio))
        app.add_handler(CommandHandler("events", self._cmd_events))
        app.add_handler(CommandHandler("markets", self._cmd_markets))
        app.add_handler(CommandHandler("quote", self._cmd_quote))
        app.add_handler(CommandHandler("order", self._cmd_order))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._cmd_text))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        self._app = app
        return app

    async def _run_background(self) -> None:
        app = self.build_application()
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Telegram bot polling started.")
        await asyncio.Event().wait()

    def start_background_polling(self) -> threading.Thread:
        thread = threading.Thread(target=lambda: asyncio.run(self._run_background()), daemon=True)
        thread.start()
        return thread

    def run_polling(self) -> None:
        app = self.build_application()
        logger.info("Telegram bot polling started.")
        app.run_polling(stop_signals=None)


def build_telegram_handler_from_env(bot_status_callback=None) -> Optional["TelegramHandler"]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID)
    success_sticker_set = os.getenv("TELEGRAM_SUCCESS_STICKER_SET", DEFAULT_SUCCESS_STICKER_SET)
    success_sticker_file_id = os.getenv("TELEGRAM_SUCCESS_STICKER_FILE_ID", "").strip() or None

    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram notifications disabled.")
        return None

    return TelegramHandler(
        token=token,
        chat_id=chat_id,
        bot_status_callback=bot_status_callback,
        success_sticker_set=success_sticker_set,
        success_sticker_file_id=success_sticker_file_id,
    )
