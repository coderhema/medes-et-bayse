"""Regression tests for medes_et_bayse.telegram_handlers."""

from types import SimpleNamespace

from medes_et_bayse import telegram_handlers as handlers


class StubClient:
    def __init__(self):
        self.calls = []

    def place_order(self, event_id, market_id, *, outcome_id, side, amount, currency, order_type="LIMIT", price=None):
        self.calls.append(
            {
                "event_id": event_id,
                "market_id": market_id,
                "outcome_id": outcome_id,
                "side": side,
                "amount": amount,
                "currency": currency,
                "order_type": order_type,
                "price": price,
            }
        )
        return {
            "orderId": "ord_123",
            "eventId": event_id,
            "marketId": market_id,
            "outcomeId": outcome_id,
            "side": side,
            "type": order_type,
            "status": "submitted",
            "amount": amount,
            "price": price,
        }


def make_context():
    candidate = {
        "event_id": "evt_123",
        "market_id": "mkt_456",
        "event_title": "Bitcoin rally",
        "market_title": "BTC above 100k",
        "market": {
            "outcomes": [
                {"outcomeId": "out_yes", "name": "Yes"},
                {"outcomeId": "out_no", "name": "No"},
            ]
        },
    }
    return SimpleNamespace(
        user_data={
            "active_market_candidate": candidate,
            "trade_order_state": {
                "event_id": "evt_123",
                "market_id": "mkt_456",
                "side": "buy",
                "currency": "NGN",
                "outcome_label": "YES",
            },
        }
    )


def test_should_suppress_debug_message_matches_expected_phrase():
    assert handlers._should_suppress_debug_message("No signals this cycle")
    assert not handlers._should_suppress_debug_message("fresh update")


def test_build_order_command_returns_result_instead_of_none():
    client = StubClient()
    context = make_context()

    result = handlers.build_order_command(client, "250", context=context)

    assert result is not None
    assert result.ok is True
    assert client.calls == [
        {
            "event_id": "evt_123",
            "market_id": "mkt_456",
            "outcome_id": "out_yes",
            "side": "buy",
            "amount": 250.0,
            "currency": "NGN",
            "order_type": "MARKET",
            "price": None,
        }
    ]
    assert "Order update" in result.text
