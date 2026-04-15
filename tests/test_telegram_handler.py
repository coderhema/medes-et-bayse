"""Regression tests for Telegram trade-context handling."""

from types import SimpleNamespace

from bot.telegram_handler import TelegramHandler
from medes_et_bayse import telegram_handlers as package_handlers


def make_context(active_trade_context=None):
    return SimpleNamespace(user_data={"active_trade_context": active_trade_context or {}})


def test_resolve_trade_context_merges_currency_without_losing_ids():
    handler = TelegramHandler(token="test-token")
    context = make_context(
        {
            "event_id": "evt_123",
            "market_id": "mkt_456",
            "outcome_id": "out_789",
            "side": "BUY",
        }
    )

    resolved = handler._resolve_trade_context(context, currency="USD")

    assert resolved["event_id"] == "evt_123"
    assert resolved["market_id"] == "mkt_456"
    assert resolved["outcome_id"] == "out_789"
    assert resolved["currency"] == "USD"
    assert handler._trade_context_ready(resolved)
    assert context.user_data["active_trade_context"]["eventId"] == "evt_123"
    assert context.user_data["active_trade_context"]["marketId"] == "mkt_456"


def test_resolve_trade_context_ignores_blank_overrides():
    handler = TelegramHandler(token="test-token")
    context = make_context(
        {
            "event_id": "evt_111",
            "market_id": "mkt_222",
            "outcome_id": "out_333",
            "currency": "NGN",
            "side": "BUY",
        }
    )

    resolved = handler._resolve_trade_context(context, event_id="", market_id="", outcome_id="")

    assert resolved["event_id"] == "evt_111"
    assert resolved["market_id"] == "mkt_222"
    assert resolved["outcome_id"] == "out_333"
    assert resolved["currency"] == "NGN"
    assert handler._trade_context_ready(resolved)


def test_should_suppress_debug_message_matches_debug_phrases():
    assert package_handlers._should_suppress_debug_message("No signals this cycle")
    assert not package_handlers._should_suppress_debug_message("fresh update")
