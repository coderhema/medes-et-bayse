"""Tests for the v2 agent: parsing, fees, sizing, risk, order payload, loop, SLM runtime."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from medes_et_bayse.agent import AgentConfig, TradingAgent, parse_events
from medes_et_bayse.agent.loop import normalize_exclusive
from medes_et_bayse.agent.store import AgentStore
from medes_et_bayse.agent.strategy import choose_side, decide, effective_price, fee_fraction, kelly_fraction
from medes_et_bayse.client import BayseClient, BayseClientError
from medes_et_bayse.slm import BayseSLM, MarketSnapshot, NUMERIC_FEATURES, numeric_features

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def _event(event_id="evt1", yes=0.30, orders=50, hours_to_close=48, category="sports", markets=None):
    closing = (NOW + timedelta(hours=hours_to_close)).isoformat().replace("+00:00", "Z")
    market = {
        "id": f"{event_id}-m1", "title": "Yes", "status": "open", "feePercentage": 10, "totalOrders": orders,
        "yesBuyPrice": 0, "noBuyPrice": 0, "outcome1Price": yes, "outcome2Price": round(1 - yes, 4),
        "outcome1Id": f"{event_id}-yes", "outcome1Label": "Yes", "outcome2Id": f"{event_id}-no", "outcome2Label": "No",
    }
    return {"id": event_id, "title": f"Event {event_id}?", "category": category.upper(), "status": "open",
            "engine": "AMM", "liquidity": 1000, "closingDate": closing, "createdAt": "2026-11-01T00:00:00Z",
            "markets": markets or [market]}


# ---------------------------------------------------------------- imports / config
def test_package_imports_without_any_keys():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BAYSE_", "TELEGRAM_"))}
    code = "import medes_et_bayse, main; from medes_et_bayse.config import runtime_config; print(runtime_config.has_credentials)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("False")


def test_no_hardcoded_identity_in_config():
    import re

    text = (ROOT / "medes_et_bayse" / "config.py").read_text()
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "no email addresses in config.py"
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text), "no hard-coded user ids"


def test_min_edge_floor_and_kelly_clamp(monkeypatch):
    monkeypatch.setenv("MIN_EDGE", "0.02")
    monkeypatch.setenv("KELLY_FRACTION", "1.0")
    cfg = AgentConfig.from_env()
    assert cfg.min_edge == pytest.approx(0.08)
    assert cfg.kelly_multiplier == pytest.approx(0.5)
    assert cfg.dry_run is True


# ---------------------------------------------------------------- parsing
def test_parse_events_uses_outcome_prices_not_zero_buy_prices():
    views = parse_events({"events": [_event(yes=0.7043)]})
    assert len(views) == 1
    v = views[0]
    assert v.yes_price == pytest.approx(0.7043) and v.no_price == pytest.approx(0.2957)
    assert (v.event_id, v.market_id) == ("evt1", "evt1-m1")
    assert (v.yes_outcome_id, v.no_outcome_id) == ("evt1-yes", "evt1-no")
    assert v.category == "sports"


def test_parse_events_skips_markets_without_prices():
    ev = _event()
    ev["markets"][0]["outcome1Price"] = 0
    ev["markets"][0]["outcome2Price"] = 0
    assert parse_events({"events": [ev]}) == []


# ---------------------------------------------------------------- fees / edge / sizing
@pytest.mark.parametrize("price,expected", [(0.30, 0.07), (0.50, 0.05), (0.70, 0.05), (0.90, 0.05)])
def test_fee_fraction_matches_bayse_docs(price, expected):
    assert fee_fraction(price, 10) == pytest.approx(expected)


def test_bearish_view_is_buy_no_not_sell():
    side = choose_side(0.20, 0.50, 0.50, fee_pct=10, min_edge=0.08)
    assert side is not None and side[0] == "NO"
    assert side[4] == pytest.approx(0.80 - effective_price(0.50, 10))


def test_edge_must_clear_fees():
    # 6 points of raw edge at P=0.5 is eaten by the 5% fee -> no trade at min_edge 0.08
    assert choose_side(0.56, 0.50, 0.50, fee_pct=10, min_edge=0.08) is None


def test_fractional_kelly_is_capped():
    assert kelly_fraction(0.9, 0.5, fraction=0.25, cap=0.05) == pytest.approx(0.05)
    full = kelly_fraction(0.6, 0.5, fraction=1.0, cap=1.0)
    assert kelly_fraction(0.6, 0.5, fraction=0.25, cap=1.0) == pytest.approx(full * 0.25)
    d = decide(0.95, 0.60, bankroll=100, fee_pct=10, min_edge=0.08, kelly_multiplier=0.25, max_position_fraction=0.05)
    assert d is not None and d.stake <= 5.0 and d.outcome == "YES"


# ---------------------------------------------------------------- client payload
def test_place_order_payload_matches_docs(monkeypatch):
    client = BayseClient(api_key="pk", api_secret="sk")
    captured = {}
    monkeypatch.setattr(client, "_request", lambda method, path, **kw: captured.update(method=method, path=path, **kw) or {"ok": True})
    client.place_order("e1", "m1", outcome_id="o-no", side="buy", amount=5, currency="usd")
    body = captured["json_body"]
    assert captured["path"] == "/pm/events/e1/markets/m1/orders" and captured["auth"] == "write"
    assert body == {"side": "BUY", "outcomeId": "o-no", "amount": 5.0, "type": "MARKET", "currency": "USD"}
    with pytest.raises(BayseClientError):
        client.place_order("e1", "m1", outcome_id="o", amount=5, order_type="LIMIT")
    with pytest.raises(BayseClientError):
        client.place_order("e1", "m1", outcome_id="", amount=5)


# ---------------------------------------------------------------- loop
class FakeClient:
    def __init__(self, events):
        self.events, self.orders = events, []

    def list_events(self, *, page=1, size=20, params=None):
        cat = (params or {}).get("category")
        evs = [e for e in self.events if not cat or e["category"].lower() == cat]
        return {"events": evs if page == 1 else []}

    def get_price_history(self, event_id, **kw):
        ms = int((NOW - timedelta(hours=72)).timestamp() * 1000)
        markets = []
        for e in self.events:
            if e["id"] == event_id:
                for m in e["markets"]:
                    p = m["outcome1Price"]
                    markets.append({"marketId": m["id"], "priceHistory": [{"e": ms, "p": p}, {"e": ms + 43200000, "p": p}]})
        return {"markets": markets}

    def get_event(self, event_id):
        return {"id": event_id, "markets": []}

    def place_order(self, *a, **kw):  # must never be called in dry-run
        self.orders.append((a, kw))
        raise AssertionError("place_order called in dry run")


class FixedSLM:
    version = "fixed"
    uses_text = False

    def __init__(self, p):
        self.p = p

    def predict_proba(self, snaps):
        return [self.p for _ in snaps]


def _agent(events, p_yes, **cfg_kw):
    from medes_et_bayse.agent.predictor import Predictor

    cfg = AgentConfig(database_path=":memory:", **cfg_kw)
    client = FakeClient(events)
    reports = []
    agent = TradingAgent(client, cfg, store=AgentStore(":memory:"), predictor=Predictor(FixedSLM(p_yes)), reporter=reports.append)
    return agent, client, reports


def test_loop_dry_run_buys_no_and_never_calls_place_order():
    agent, client, reports = _agent([_event(yes=0.60)], p_yes=0.25)
    report = agent.cycle(now=NOW)
    assert report.errors == []
    assert len(report.trades) == 1 and report.trades[0]["outcome"] == "NO" and report.trades[0]["status"] == "dry_run"
    assert client.orders == []
    trade = agent.store.recent_trades(1)[0]
    assert trade["outcome_id"] == "evt1-no" and trade["market_id"] == "evt1-m1" and trade["dry_run"] == 1
    assert reports and "DRY RUN" in reports[0]


def test_loop_filters_low_activity_and_closing_soon():
    events = [_event("a", orders=1), _event("b", hours_to_close=2), _event("c", hours_to_close=24 * 30), _event("d", category="crypto")]
    agent, _, _ = _agent(events, p_yes=0.05)
    report = agent.cycle(now=NOW)
    assert report.tradeable == 0 and report.trades == []
    assert {"low_activity", "closes_too_soon", "closes_too_far"} <= set(report.skipped)


def test_daily_loss_limit_blocks_further_trades():
    events = [_event(f"e{i}", yes=0.60) for i in range(5)]
    agent, _, _ = _agent(events, p_yes=0.10, bankroll=100, max_daily_loss_fraction=0.06, max_trades_per_cycle=5)
    report = agent.cycle(now=NOW)
    staked = sum(t["stake"] for t in report.trades)
    assert staked <= 6.0 + 1e-9
    reasons = [r["reason"] for r in agent.store._exec("SELECT reason FROM predictions").fetchall()]
    assert "daily_loss_limit" in reasons


def test_markets_without_price_history_are_not_predicted():
    agent, client, _ = _agent([_event(yes=0.60)], p_yes=0.10)
    client.get_price_history = lambda event_id, **kw: {"markets": []}
    report = agent.cycle(now=NOW)
    assert report.trades == [] and report.skipped.get("no_price_history") == 1


def test_normalize_exclusive_event_sums_to_one():
    markets = []
    for i, price in enumerate([0.70, 0.20, 0.10]):
        markets.append({"id": f"m{i}", "title": f"cand{i}", "status": "open", "feePercentage": 10, "totalOrders": 50,
                        "outcome1Price": price, "outcome2Price": 1 - price, "outcome1Id": f"y{i}", "outcome1Label": "Yes",
                        "outcome2Id": f"n{i}", "outcome2Label": "No"})
    views = parse_events({"events": [_event(markets=markets)]})
    probs = normalize_exclusive(views, views[:2], [0.95, 0.30])
    assert probs[0] + probs[1] + 0.10 == pytest.approx(1.0)


# ---------------------------------------------------------------- SLM runtime
def test_slm_artifact_loads_and_predicts_probability():
    slm = BayseSLM.load(use_text=False)
    assert len(slm.head["coef"]) == len(NUMERIC_FEATURES)
    snap = MarketSnapshot(yes_price=0.3, as_of=NOW, category="sports", event_title="ARS vs CHE: Who Will Win?",
                          closing_date=NOW + timedelta(hours=24), created_at=NOW - timedelta(days=3),
                          history=[{"e": int((NOW - timedelta(hours=48)).timestamp() * 1000), "p": 0.35}])
    assert len(numeric_features(snap)) == len(NUMERIC_FEATURES)
    p = slm.predict_one(snap)
    assert 0.0 < p < 1.0
    # monotone in price: a higher market price should not lower the model's probability
    hi = slm.predict_one(MarketSnapshot(**{**snap.__dict__, "yes_price": 0.7}))
    assert hi > p
