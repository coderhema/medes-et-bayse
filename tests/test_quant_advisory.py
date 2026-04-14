"""Tests for the QuantAdvisory module."""

from __future__ import annotations

import pytest

from bot.strategies.quant_advisory import QuantAdvisory, format_quant_opinion
from bot.strategies.market_maker import compute_fair_value, compute_half_spread


# ---------------------------------------------------------------------------
# Sample event fixtures
# ---------------------------------------------------------------------------

EVENT_WITH_PRICES = {
    "id": "evt_qa_001",
    "title": "Will Team X win?",
    "yesPrice": 0.60,
    "noPrice": 0.35,
    "status": "open",
    "volume": 2000,
}

EVENT_WITH_LIVE_QUOTE = {
    "id": "evt_qa_002",
    "title": "Will BTC hit $100k?",
    "yesPrice": 0.45,
    "status": "open",
    "liveQuote": {
        "bid": 0.42,
        "ask": 0.48,
        "midpoint": 0.45,
    },
}

EVENT_NO_DATA = {
    "id": "evt_qa_003",
    "title": "Mystery event",
    "status": "open",
}

EVENT_NARROW_SPREAD = {
    "id": "evt_qa_004",
    "title": "Highly liquid market",
    "yesPrice": 0.50,
    "status": "open",
    "liveQuote": {
        "bid": 0.499,
        "ask": 0.501,
    },
}


# ---------------------------------------------------------------------------
# QuantAdvisory.generate_opinion
# ---------------------------------------------------------------------------

class TestGenerateOpinion:
    def test_returns_unavailable_when_no_price_data(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_NO_DATA)
        assert opinion["available"] is False
        assert "reason" in opinion

    def test_returns_available_with_valid_prices(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        assert opinion["available"] is True

    def test_fair_value_is_clamped(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        fv = opinion["fair_value"]
        assert 0.01 <= fv <= 0.99

    def test_edge_is_difference_of_fair_value_and_market_price(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        expected_edge = round(opinion["fair_value"] - opinion["market_price"], 4)
        assert abs(opinion["edge"] - expected_edge) < 1e-6

    def test_spread_equals_half_spread_times_two(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        assert abs(opinion["spread"] - opinion["half_spread"] * 2) < 1e-6

    def test_risk_reward_is_edge_over_half_spread(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        expected_rr = round(abs(opinion["edge"]) / opinion["half_spread"], 2)
        assert abs(opinion["risk_reward"] - expected_rr) < 0.01

    def test_confidence_between_zero_and_one(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        assert 0.0 <= opinion["confidence"] <= 1.0

    def test_confidence_label_values(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        assert opinion["confidence_label"] in {"high", "moderate", "low"}

    def test_verdict_signal_values(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        assert opinion["verdict_signal"] in {"BUY YES", "BUY NO", "HOLD"}

    def test_hold_when_edge_below_min_edge(self):
        # min_edge=1.0 means any edge less than 100% triggers HOLD (always true
        # since fair_value is clamped to [0.01, 0.99])
        advisory = QuantAdvisory(min_edge=1.0)
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        assert opinion["verdict_signal"] == "HOLD"

    def test_live_quote_used_when_present(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_LIVE_QUOTE)
        assert opinion["available"] is True
        # Market price should be close to the live-quote midpoint
        assert abs(opinion["market_price"] - 0.45) < 0.05

    def test_narrow_spread_yields_high_confidence(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_NARROW_SPREAD)
        assert opinion["available"] is True
        # The half-spread floor (min_edge/2 = 0.015) means spread ~= 0.030,
        # giving confidence = 1 - 0.030/0.10 = 0.70.  Assert it is above 0.65.
        assert opinion["confidence"] > 0.65

    def test_volume_captured(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        assert opinion["volume"] == 2000.0

    def test_volume_none_when_absent(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_LIVE_QUOTE)
        assert opinion["volume"] is None


# ---------------------------------------------------------------------------
# format_quant_opinion
# ---------------------------------------------------------------------------

class TestFormatQuantOpinion:
    def test_unavailable_opinion(self):
        opinion = {"available": False, "reason": "no data"}
        text = format_quant_opinion(opinion)
        assert "Quant Opinion" in text
        assert "no data" in text

    def test_available_opinion_contains_key_fields(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        text = format_quant_opinion(opinion, title="Will Team X win?")
        assert "Fair Value" in text
        assert "Market Price" in text
        assert "Edge" in text
        assert "Spread" in text
        assert "R/R" in text
        assert "Verdict" in text

    def test_title_included_in_header(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        text = format_quant_opinion(opinion, title="My Market")
        assert "My Market" in text

    def test_no_title_still_renders(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        text = format_quant_opinion(opinion)
        assert "Quant Opinion" in text

    def test_edge_sign_positive(self):
        advisory = QuantAdvisory(min_edge=0.0)
        # Construct an event where fair value > market price
        event = {
            "id": "e1",
            "yesPrice": 0.40,
            "noPrice": 0.40,
            "status": "open",
        }
        opinion = advisory.generate_opinion(event)
        text = format_quant_opinion(opinion)
        if opinion["edge"] >= 0:
            assert "+" in text or "↑" in text

    def test_html_bold_tags_present(self):
        advisory = QuantAdvisory()
        opinion = advisory.generate_opinion(EVENT_WITH_PRICES)
        text = format_quant_opinion(opinion)
        assert "<b>" in text and "</b>" in text


# ---------------------------------------------------------------------------
# Module-level helpers re-exported from market_maker
# ---------------------------------------------------------------------------

class TestMarketMakerHelpers:
    def test_compute_fair_value_returns_float(self):
        fv = compute_fair_value(EVENT_WITH_PRICES, {})
        assert fv is not None
        assert isinstance(fv, float)
        assert 0.01 <= fv <= 0.99

    def test_compute_fair_value_none_without_data(self):
        fv = compute_fair_value(EVENT_NO_DATA, {})
        assert fv is None

    def test_compute_half_spread_positive(self):
        fv = compute_fair_value(EVENT_WITH_PRICES, {})
        live = {"bid": 0.55, "ask": 0.65}
        hs = compute_half_spread(fv, live)
        assert hs > 0

    def test_compute_half_spread_uses_live_quote(self):
        fv = 0.50
        live = {"bid": 0.48, "ask": 0.52}
        hs = compute_half_spread(fv, live)
        # Observed half-spread is 0.02; helper should use at least 0.75 * 0.02 = 0.015
        assert hs >= 0.015
