"""Regression tests for medes_et_bayse.telegram_handlers."""

from types import SimpleNamespace

from medes_et_bayse import telegram_handlers as handlers


class StubClient:
    def __init__(self):
        self.calls = []

    def place_order(self, event_id, market_id, *, outcome_id, side, amount, currency, order_type="MARKET", price=None):
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


def _make_candidate_with_market(event_id="evt_123", market_id="mkt_456"):
    """Build a candidate dict as produced by _candidate_from_event_market."""
    return {
        "event_id": event_id,
        "eventId": event_id,
        "eventid": event_id,
        "market_id": market_id,
        "marketId": market_id,
        "marketid": market_id,
        "event_title": "Test event",
        "market_title": "Test market",
        "event": {"id": event_id},
        "market": {
            "id": market_id,
            "outcomes": [
                {"outcomeId": "out_yes", "name": "Yes"},
                {"outcomeId": "out_no", "name": "No"},
            ],
        },
        "currency": "NGN",
        "yes_price": "0.6",
        "no_price": "0.4",
    }


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


# ── ID alias sync ──────────────────────────────────────────────────────────────

def test_sync_candidate_ids_adds_missing_aliases():
    candidate = {"event_id": "evt_abc", "market_id": "mkt_xyz"}
    handlers._sync_candidate_ids(candidate)
    assert candidate["eventId"] == "evt_abc"
    assert candidate["eventid"] == "evt_abc"
    assert candidate["marketId"] == "mkt_xyz"
    assert candidate["marketid"] == "mkt_xyz"


def test_sync_candidate_ids_prefers_snake_case_source():
    # snake_case takes precedence (listed first in _first_string)
    candidate = {"event_id": "snake_id", "eventId": "camel_id", "market_id": "snake_mid"}
    handlers._sync_candidate_ids(candidate)
    assert candidate["event_id"] == "snake_id"
    assert candidate["eventId"] == "snake_id"
    assert candidate["market_id"] == "snake_mid"
    assert candidate["marketId"] == "snake_mid"


def test_set_active_market_context_syncs_aliases():
    context = SimpleNamespace(user_data={})
    candidate = {"event_id": "evt_001", "market_id": "mkt_002", "event": {}, "market": {}}
    handlers._set_active_market_context(context, candidate)
    stored = context.user_data["active_market_candidate"]
    assert stored["eventId"] == "evt_001"
    assert stored["eventid"] == "evt_001"
    assert stored["marketId"] == "mkt_002"
    assert stored["marketid"] == "mkt_002"


def test_quote_candidates_from_events_includes_all_id_aliases():
    event = {"id": "evt_q1", "markets": [{"id": "mkt_q1"}]}
    candidates = handlers._quote_candidates_from_events([event])
    assert len(candidates) == 1
    c = candidates[0]
    assert c["event_id"] == "evt_q1"
    assert c["eventId"] == "evt_q1"
    assert c["eventid"] == "evt_q1"
    assert c["market_id"] == "mkt_q1"
    assert c["marketId"] == "mkt_q1"
    assert c["marketid"] == "mkt_q1"


# ── Callback-to-reply flow ─────────────────────────────────────────────────────

def test_set_trade_order_state_keeps_ids_canonical():
    """After _set_trade_order_state, all ID aliases must be in sync."""
    context = SimpleNamespace(user_data={})
    candidate = _make_candidate_with_market("evt_canon", "mkt_canon")
    handlers._set_trade_order_state(context, candidate, side="buy", currency="NGN", stage="currency")
    state = context.user_data["trade_order_state"]
    assert state["event_id"] == "evt_canon"
    assert state["eventId"] == "evt_canon"
    assert state["eventid"] == "evt_canon"
    assert state["market_id"] == "mkt_canon"
    assert state["marketId"] == "mkt_canon"
    assert state["marketid"] == "mkt_canon"


def test_tradec_currency_selection_propagates_to_trade_amount_state():
    """Simulates the tradec callback: currency is saved and pending interaction set to trade_amount."""
    context = SimpleNamespace(user_data={})
    candidate = _make_candidate_with_market()
    # Simulate earlier state set by tradeo/trades callbacks
    handlers._set_trade_order_state(context, candidate, outcome_id="out_yes", outcome_label="Yes", side="buy", stage="currency")
    handlers._set_active_market_context(context, candidate)

    # Mimic tradec callback logic: set currency and advance to trade_amount
    state = handlers._active_trade_order_state(context) or {}
    selected_trade = handlers._active_trade_selection(context)
    outcome_id = handlers._first_string(state.get("outcome_id") if isinstance(state, dict) else "", default="")
    outcome_label = handlers._first_string(state.get("outcome_label") if isinstance(state, dict) else "", default="")
    side = str(state.get("side", "")).lower()
    handlers._set_active_market_context(context, candidate)
    handlers._set_trade_order_state(context, candidate, outcome_id=outcome_id, outcome_label=outcome_label, side=side, currency="NGN", stage="amount")
    handlers._set_pending_interaction(context, "trade_amount", prompt="Send the amount now.")

    updated_state = context.user_data["trade_order_state"]
    assert updated_state["currency"] == "NGN"
    assert updated_state["stage"] == "amount"
    assert updated_state["event_id"] == "evt_123"
    assert updated_state["market_id"] == "mkt_456"
    assert context.user_data["pending_interaction"]["kind"] == "trade_amount"


def test_tradec_currency_selection_does_not_overwrite_canonical_event_id():
    """Currency step must preserve the original canonical IDs even if candidate aliases drift."""
    context = SimpleNamespace(user_data={})
    candidate = _make_candidate_with_market("evt_origin", "mkt_origin")
    handlers._set_trade_order_state(context, candidate, outcome_id="out_yes", outcome_label="Yes", side="buy", stage="currency")
    handlers._set_active_market_context(context, candidate)

    drifted_candidate = _make_candidate_with_market("Event label should not be event_id", "Market label should not be market_id")
    handlers._set_trade_order_state(
        context,
        drifted_candidate,
        outcome_id="out_yes",
        outcome_label="Yes",
        side="buy",
        currency="USD",
        stage="amount",
    )

    updated_state = context.user_data["trade_order_state"]
    assert updated_state["event_id"] == "evt_origin"
    assert updated_state["market_id"] == "mkt_origin"
    assert updated_state["currency"] == "USD"


def test_trade_amount_reply_uses_canonical_ids_to_place_order():
    """After tradec sets currency, a trade_amount reply must place the order with the correct IDs."""
    client = StubClient()
    candidate = _make_candidate_with_market()
    context = SimpleNamespace(user_data={})
    handlers._set_active_market_context(context, candidate)
    handlers._set_trade_order_state(
        context,
        candidate,
        outcome_id="out_yes",
        outcome_label="Yes",
        side="buy",
        currency="NGN",
        stage="amount",
    )
    handlers._set_pending_interaction(context, "trade_amount", prompt="Send the amount now.")

    result = handlers._route_pending_interaction(client, context, "500")

    assert result is not None
    assert result.ok is True
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["event_id"] == "evt_123"
    assert call["market_id"] == "mkt_456"
    assert call["outcome_id"] == "out_yes"
    assert call["side"] == "buy"
    assert call["amount"] == 500.0
    assert call["currency"] == "NGN"


def test_order_placement_preserves_event_and_market_ids_from_context():
    """build_order_command must use the event_id and market_id from the active context, not fall back."""
    client = StubClient()
    candidate = _make_candidate_with_market("evt_preserve", "mkt_preserve")
    context = SimpleNamespace(user_data={})
    handlers._set_active_market_context(context, candidate)
    handlers._set_trade_order_state(
        context,
        candidate,
        outcome_id="out_yes",
        outcome_label="Yes",
        side="buy",
        currency="USD",
        stage="ready",
    )

    result = handlers.build_order_command(client, "100 USD", context=context)

    assert result is not None
    assert result.ok is True
    call = client.calls[0]
    assert call["event_id"] == "evt_preserve"
    assert call["market_id"] == "mkt_preserve"


def test_candidate_from_state_rebuilds_all_id_aliases():
    """_candidate_from_state must reconstruct a candidate with all ID aliases."""
    state = {
        "event_id": "evt_state",
        "market_id": "mkt_state",
        "event": {},
        "market": {},
        "currency": "USD",
    }
    candidate = handlers._candidate_from_state(state)
    assert candidate is not None
    assert candidate["event_id"] == "evt_state"
    assert candidate["eventId"] == "evt_state"
    assert candidate["eventid"] == "evt_state"
    assert candidate["market_id"] == "mkt_state"
    assert candidate["marketId"] == "mkt_state"
    assert candidate["marketid"] == "mkt_state"


# ── Empty/placeholder order response suppression ───────────────────────────────

def test_is_empty_order_response_returns_true_for_empty_payload():
    """_is_empty_order_response must return True when all critical fields are absent."""
    from medes_et_bayse.models import OrderResponse
    response = OrderResponse.from_dict({})
    assert handlers._is_empty_order_response(response) is True


def test_is_empty_order_response_returns_false_when_order_id_present():
    """_is_empty_order_response must return False when the order has an ID."""
    from medes_et_bayse.models import OrderResponse
    response = OrderResponse.from_dict({"orderId": "ord_abc"})
    assert handlers._is_empty_order_response(response) is False


def test_is_empty_order_response_returns_false_when_status_present():
    """_is_empty_order_response must return False when the order has a status."""
    from medes_et_bayse.models import OrderResponse
    response = OrderResponse.from_dict({"status": "open"})
    assert handlers._is_empty_order_response(response) is False


def test_is_empty_order_response_returns_false_for_filled_order():
    """_is_empty_order_response must return False for a fully filled order."""
    from medes_et_bayse.models import OrderResponse
    response = OrderResponse.from_dict({
        "orderId": "ord_filled",
        "status": "FILLED",
        "side": "BUY",
        "amount": 100.0,
    })
    assert handlers._is_empty_order_response(response) is False


def test_build_order_command_suppresses_empty_response():
    """build_order_command must suppress an all-empty API response and not return ok=True."""

    class EmptyResponseClient:
        calls = []

        def place_order(self, event_id, market_id, *, outcome_id, side, amount, currency, order_type="MARKET", price=None):
            self.calls.append({})
            return {}  # completely empty response

    client = EmptyResponseClient()
    context = make_context()

    result = handlers.build_order_command(client, "250", context=context)

    assert result is not None
    assert result.ok is False
    assert isinstance(result.raw, dict)
    assert result.raw.get("suppressed") is True


def test_build_order_command_suppresses_na_only_response():
    """n/a-only placeholder payloads must be suppressed instead of formatted as an update."""

    class PlaceholderResponseClient:
        calls = []

        def place_order(self, event_id, market_id, *, outcome_id, side, amount, currency, order_type="MARKET", price=None):
            self.calls.append({})
            return {
                "status": "n/a",
                "side": "n/a",
                "order": {"status": "n/a", "side": "n/a"},
            }

    client = PlaceholderResponseClient()
    context = make_context()

    result = handlers.build_order_command(client, "250", context=context)

    assert result is not None
    assert result.ok is False
    assert isinstance(result.raw, dict)
    assert result.raw.get("suppressed") is True


def test_build_order_command_blocks_suspicious_event_id():
    """Orders must not be sent when event_id looks suspicious or non-canonical."""
    client = StubClient()
    context = SimpleNamespace(
        user_data={
            "active_market_candidate": _make_candidate_with_market("Market label instead of event id", "mkt_good"),
            "trade_order_state": {
                "event_id": "Market label instead of event id",
                "market_id": "mkt_good",
                "outcome_id": "out_yes",
                "side": "buy",
                "currency": "USD",
                "outcome_label": "Yes",
                "stage": "ready",
            },
        }
    )

    result = handlers.build_order_command(client, "100", context=context)

    assert result is not None
    assert result.ok is False
    assert "couldn't verify the active event ID" in result.text
    assert client.calls == []


def test_build_order_command_returns_ok_for_response_with_order_id():
    """build_order_command must return ok=True when the API response contains an order ID."""

    class FullResponseClient:
        def place_order(self, event_id, market_id, *, outcome_id, side, amount, currency, order_type="MARKET", price=None):
            return {
                "orderId": "ord_xyz",
                "status": "submitted",
                "side": side,
                "amount": amount,
            }

    client = FullResponseClient()
    context = make_context()

    result = handlers.build_order_command(client, "250", context=context)

    assert result is not None
    assert result.ok is True
    assert "Order" in result.text


def test_build_order_command_uses_filled_receipt_for_filled_status():
    """build_order_command must use the confirmed receipt header when status is FILLED."""

    class FilledResponseClient:
        def place_order(self, event_id, market_id, *, outcome_id, side, amount, currency, order_type="MARKET", price=None):
            return {
                "orderId": "ord_filled",
                "status": "filled",
                "side": side,
                "amount": amount,
            }

    client = FilledResponseClient()
    context = make_context()

    result = handlers.build_order_command(client, "500", context=context)

    assert result is not None
    assert result.ok is True
    assert "confirmed" in result.text.lower()


def test_is_suppressed_order_result_identifies_suppressed_result():
    """_is_suppressed_order_result must return True only for suppressed results."""
    suppressed = handlers.CommandResult(False, "", raw={"suppressed": True, "reason": "empty_order_response", "raw": {}})
    normal_error = handlers.CommandResult(False, "Some error message")
    ok_result = handlers.CommandResult(True, "Order update")

    assert handlers._is_suppressed_order_result(suppressed) is True
    assert handlers._is_suppressed_order_result(normal_error) is False
    assert handlers._is_suppressed_order_result(ok_result) is False


def test_format_filled_receipt_produces_confirmed_header():
    """_format_filled_receipt must produce a confirmed header and include key fields."""
    from medes_et_bayse.models import OrderResponse
    response = OrderResponse.from_dict({
        "orderId": "ord_conf",
        "status": "FILLED",
        "side": "BUY",
        "amount": 200.0,
        "filledQuantity": 200.0,
        "averageFillPrice": 0.65,
    })
    text = handlers._format_filled_receipt(response)
    assert "confirmed" in text.lower()
    assert "FILLED" in text
    assert "BUY" in text
