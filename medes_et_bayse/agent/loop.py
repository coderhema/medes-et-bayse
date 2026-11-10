"""The trading agent loop.

One cycle = SCAN -> FILTER -> PREDICT (SLM) -> RESEARCH (optional Tavily/LLM on the shortlist)
-> EDGE (post-fee) -> SIZE (fractional Kelly + caps) -> RISK (daily loss budget, positions)
-> EXECUTE (dry-run by default) -> LOG (SQLite) -> REPORT (Telegram) ; plus SETTLE of past trades.
"""
from __future__ import annotations

import html
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from ..client import BayseClient
from .config import AgentConfig
from .markets import MarketView, attach_history, parse_events, utcnow
from .predictor import Predictor
from .risk import RiskManager, market_filter_reason
from .store import AgentStore
from .strategy import decide, settle_pnl

logger = logging.getLogger(__name__)


@dataclass
class CycleReport:
    cycle_id: int
    scanned: int = 0
    tradeable: int = 0
    skipped: dict = field(default_factory=dict)
    shortlisted: int = 0
    trades: list = field(default_factory=list)
    settled: int = 0
    errors: list = field(default_factory=list)
    top_candidates: list = field(default_factory=list)

    def summary_html(self, cfg: AgentConfig) -> str:
        mode = "DRY RUN" if cfg.dry_run else "LIVE"
        lines = [f"<b>Cycle #{self.cycle_id}</b> ({mode})",
                 f"Markets scanned: {self.scanned} | tradeable: {self.tradeable} | shortlisted: {self.shortlisted}"]
        for t in self.trades:
            lines.append(f"{'🧪' if cfg.dry_run else '💰'} BUY {t['outcome']} {html.escape(t['title'][:80])}\n"
                         f"   price {t['price']:.3f} (all-in {t['effective_price']:.3f}) | p_win {t['p_win']:.3f} | "
                         f"edge {t['net_edge']:+.3f} | stake {t['stake']:.2f} {cfg.currency} | {t['status']}")
        if not self.trades:
            lines.append("No market cleared the post-fee edge filter.")
        if self.settled:
            lines.append(f"Settled positions: {self.settled}")
        if self.errors:
            lines.append("Errors: " + html.escape("; ".join(self.errors)[:300]))
        return "\n".join(lines)


def normalize_exclusive(all_views: list[MarketView], tradeable: list[MarketView], probs: list[float]) -> list[float]:
    """Keep probabilities coherent inside mutually exclusive multi-market events (e.g. elections).

    If an event's YES prices sum to ~1, the modelled markets are rescaled so that, together with
    the market prices of the siblings we did not model, the event sums to 1.
    """
    out = list(probs)
    modelled = {v.market_id: i for i, v in enumerate(tradeable)}
    groups: dict[str, list[MarketView]] = {}
    for v in all_views:
        groups.setdefault(v.event_id, []).append(v)
    for group in groups.values():
        if len(group) < 2 or not (0.95 <= sum(v.yes_price for v in group) <= 1.05):
            continue
        idx = [modelled[v.market_id] for v in group if v.market_id in modelled]
        if not idx:
            continue
        rest = sum(v.yes_price for v in group if v.market_id not in modelled)
        target, total = max(0.0, 1.0 - rest), sum(out[i] for i in idx)
        if total > 0 and target > 0:
            for i in idx:
                out[i] = min(0.99, max(0.01, out[i] * target / total))
    return out


class TradingAgent:
    def __init__(self, client: BayseClient, cfg: Optional[AgentConfig] = None, *, store: Optional[AgentStore] = None,
                 predictor: Optional[Predictor] = None, reporter: Optional[Callable[[str], None]] = None) -> None:
        self.client = client
        self.cfg = cfg or AgentConfig.from_env()
        self.store = store or AgentStore(self.cfg.database_path)
        self.predictor = predictor or Predictor(llm_blend_weight=self.cfg.llm_blend_weight)
        self.risk = RiskManager(self.cfg, self.store)
        self.reporter = reporter
        self.last_report: Optional[CycleReport] = None

    # ---- steps ----
    def scan(self) -> list[MarketView]:
        """Open markets in the categories the SLM was trained on (deduplicated by market id)."""
        views: dict[str, MarketView] = {}
        for category in self.cfg.scan_categories:
            for page in range(1, self.cfg.scan_pages + 1):
                params = {"status": "open", "currency": self.cfg.currency}
                if category != "all":
                    params["category"] = category
                payload = self.client.list_events(page=page, size=self.cfg.page_size, params=params)
                for v in parse_events(payload):
                    views.setdefault(v.market_id, v)
                if len(payload.get("events", []) if isinstance(payload, dict) else []) < self.cfg.page_size:
                    break
        return list(views.values())

    def _load_history(self, views: list[MarketView]) -> None:
        by_event: dict[str, list[MarketView]] = {}
        for v in views:
            by_event.setdefault(v.event_id, []).append(v)
        for event_id, group in by_event.items():
            try:
                attach_history(group, self.client.get_price_history(event_id))
            except Exception as exc:
                logger.info("price history unavailable for %s: %s", event_id, exc)

    def execute(self, view: MarketView, decision, stake: float) -> dict:
        outcome_id = view.yes_outcome_id if decision.outcome == "YES" else view.no_outcome_id
        order = {"event_id": view.event_id, "market_id": view.market_id, "outcome_id": outcome_id, "side": "BUY",
                 "amount": stake, "currency": self.cfg.currency, "order_type": "MARKET"}
        if self.cfg.dry_run:
            return {"status": "dry_run", "order": order}
        try:
            resp = self.client.place_order(view.event_id, view.market_id, outcome_id=outcome_id, side="BUY",
                                           amount=stake, currency=self.cfg.currency, order_type="MARKET")
            return {"status": "submitted", "order": order, "response": resp}
        except Exception as exc:
            return {"status": "error", "order": order, "error": str(exc)}

    def settle(self, report: CycleReport) -> None:
        """Mark positions on resolved markets as settled and record PnL (works in dry-run too)."""
        open_trades = [t for t in self.store.recent_trades(200) if not t["settled"] and t["status"] in ("dry_run", "submitted", "filled")]
        for event_id in {t["event_id"] for t in open_trades}:
            try:
                ev = self.client.get_event(event_id)
            except Exception:
                continue
            ev = ev.get("event", ev) if isinstance(ev, dict) else {}
            outcomes = {m.get("id"): m.get("resolvedOutcome") for m in ev.get("markets", []) if isinstance(m, dict)}
            for t in (t for t in open_trades if t["event_id"] == event_id):
                res = outcomes.get(t["market_id"])
                if res in ("YES", "NO"):
                    pnl = settle_pnl(t["stake"], t["effective_price"], won=(res == t["outcome"]))
                    self.store._exec("UPDATE trades SET settled=1, pnl=? WHERE id=?", (pnl, t["id"]))
                    self.store._exec("UPDATE predictions SET resolved_outcome=? WHERE market_id=?", (1 if res == "YES" else 0, t["market_id"]))
                    report.settled += 1

    # ---- cycle ----
    def cycle(self, now: Optional[datetime] = None) -> CycleReport:
        now = now or utcnow()
        cid = self.store.start_cycle()
        report = CycleReport(cycle_id=cid)
        try:
            self.settle(report)
            views = self.scan()
            report.scanned = len(views)
            tradeable = []
            for v in views:
                reason = market_filter_reason(v, self.cfg, now)
                if reason:
                    report.skipped[reason] = report.skipped.get(reason, 0) + 1
                else:
                    tradeable.append(v)
            report.tradeable = len(tradeable)
            self._load_history(tradeable)
            # The SLM's activity/momentum features need price history; never predict without it.
            no_hist = [v for v in tradeable if len(v.history) < 2]
            if no_hist:
                report.skipped["no_price_history"] = len(no_hist)
                tradeable = [v for v in tradeable if len(v.history) >= 2]
            report.tradeable = len(tradeable)
            p_slm = normalize_exclusive(views, tradeable, self.predictor.predict_many(tradeable, now))
            scored = []
            for v, p in zip(tradeable, p_slm):
                d = decide(p, v.yes_price, v.no_price, bankroll=self.cfg.bankroll, fee_pct=v.fee_pct,
                           min_edge=0.0, kelly_multiplier=self.cfg.kelly_multiplier,
                           max_position_fraction=self.cfg.max_position_fraction, min_stake=0.0)
                scored.append((d.net_edge if d else -1.0, v, p))
            scored.sort(key=lambda x: x[0], reverse=True)
            report.top_candidates = [{"title": v.label[:90], "yes_price": v.yes_price, "p_slm": round(p, 4), "best_net_edge": round(e, 4)}
                                     for e, v, p in scored[:5]]
            shortlist = scored[: max(self.cfg.research_top_n, self.cfg.max_trades_per_cycle)]
            report.shortlisted = len(shortlist)
            shortlisted_ids = {v.market_id for _, v, _ in shortlist}
            for _, v, p in scored:
                if v.market_id in shortlisted_ids:
                    continue
                self.store.log_prediction(cid, event_id=v.event_id, market_id=v.market_id, title=v.label, category=v.category,
                                          yes_price=v.yes_price, p_slm=p, p_llm=None, p_yes=p, source=f"slm:{self.predictor.slm.version}",
                                          hours_to_close=v.hours_to_close(now), decision="skip", reason="not_shortlisted")
            for _, v, p in shortlist:
                pred = self.predictor.refine(v, p)
                d = decide(pred.p_yes, v.yes_price, v.no_price, bankroll=self.cfg.bankroll, fee_pct=v.fee_pct,
                           min_edge=self.cfg.min_edge, kelly_multiplier=self.cfg.kelly_multiplier,
                           max_position_fraction=self.cfg.max_position_fraction)
                decision, reason = ("skip", "edge_below_min") if d is None else ("trade", "ok")
                stake = d.stake if d else 0.0
                if d is not None and len(report.trades) >= self.cfg.max_trades_per_cycle:
                    decision, reason = "skip", "max_trades_per_cycle"
                if d is not None and decision == "trade":
                    ok, reason, stake = self.risk.check(v, d.stake, now)
                    decision = "trade" if ok and stake >= 1.0 else "skip"
                self.store.log_prediction(cid, event_id=v.event_id, market_id=v.market_id, title=v.label, category=v.category,
                                          yes_price=v.yes_price, p_slm=pred.p_slm, p_llm=pred.p_llm, p_yes=pred.p_yes,
                                          source=pred.source, hours_to_close=v.hours_to_close(now), decision=decision, reason=reason)
                if decision != "trade":
                    continue
                result = self.execute(v, d, stake)
                trade = {"title": v.label, "outcome": d.outcome, "price": d.price, "effective_price": d.effective_price,
                         "p_win": d.p_win, "net_edge": d.net_edge, "stake": stake, "status": result["status"]}
                self.store.log_trade(cid, event_id=v.event_id, market_id=v.market_id, outcome_id=result["order"]["outcome_id"],
                                     outcome=d.outcome, title=v.label, price=d.price, effective_price=d.effective_price,
                                     p_win=d.p_win, net_edge=d.net_edge, kelly_fraction=d.kelly_fraction, stake=stake,
                                     currency=self.cfg.currency, dry_run=int(self.cfg.dry_run), status=result["status"],
                                     response_json=json.dumps(result, default=str))
                if result["status"] == "error":
                    report.errors.append(result.get("error", "order error"))
                report.trades.append(trade)
            status = "completed"
        except Exception as exc:
            logger.exception("agent cycle failed")
            report.errors.append(str(exc))
            status = "failed"
        summary = report.summary_html(self.cfg)
        self.store.finish_cycle(cid, status, summary, asdict(report))
        self.last_report = report
        if self.reporter is not None:
            try:
                self.reporter(summary)
            except Exception as exc:  # pragma: no cover
                logger.warning("report failed: %s", exc)
        return report

    def run_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        stop_event = stop_event or threading.Event()
        logger.info("agent loop started (dry_run=%s, interval=%ss)", self.cfg.dry_run, self.cfg.cycle_interval_seconds)
        while not stop_event.is_set():
            started = time.monotonic()
            self.cycle()
            stop_event.wait(max(0.0, self.cfg.cycle_interval_seconds - (time.monotonic() - started)))
