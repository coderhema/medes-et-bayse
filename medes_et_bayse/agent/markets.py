"""Parse Bayse /v1/pm/events payloads into per-market views (prices + canonical IDs)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..slm.features import MarketSnapshot, parse_ts


def _price(value: Any) -> Optional[float]:
    """Bayse returns 0 for unset fields (e.g. yesBuyPrice); treat 0/1/garbage as missing."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if 0.0 < v < 1.0 else None


@dataclass
class MarketView:
    event_id: str
    market_id: str
    event_title: str
    market_title: str
    category: str
    yes_price: float
    no_price: float
    yes_outcome_id: str
    no_outcome_id: str
    fee_pct: float
    liquidity: float
    orders: int
    n_markets: int
    engine: str
    status: str
    closing_date: Optional[datetime]
    created_at: Optional[datetime]
    history: list = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.market_title and self.market_title.lower() not in {"yes", self.event_title.lower()}:
            return f"{self.event_title} — {self.market_title}"
        return self.event_title

    def hours_to_close(self, now: datetime) -> Optional[float]:
        return None if self.closing_date is None else (self.closing_date - now).total_seconds() / 3600

    def snapshot(self, now: datetime) -> MarketSnapshot:
        return MarketSnapshot(
            yes_price=self.yes_price, as_of=now, category=self.category, event_title=self.event_title,
            market_title=self.market_title, closing_date=self.closing_date, created_at=self.created_at,
            liquidity=self.liquidity, fee_pct=self.fee_pct, n_markets=self.n_markets, history=self.history,
        )


def _outcome_ids(market: dict) -> tuple[str, str]:
    yes_id = no_id = ""
    pairs = [(market.get("outcome1Label"), market.get("outcome1Id")), (market.get("outcome2Label"), market.get("outcome2Id"))]
    for label, oid in pairs:
        lab = str(label or "").strip().lower()
        if lab == "yes":
            yes_id = str(oid or "")
        elif lab == "no":
            no_id = str(oid or "")
    if not yes_id and not no_id:  # fall back to positional convention outcome1=YES
        yes_id, no_id = str(market.get("outcome1Id") or ""), str(market.get("outcome2Id") or "")
    return yes_id, no_id


def parse_events(payload: Any) -> list[MarketView]:
    events = payload.get("events", []) if isinstance(payload, dict) else (payload or [])
    out: list[MarketView] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        markets = [m for m in ev.get("markets") or [] if isinstance(m, dict)]
        for m in markets:
            yes = _price(m.get("outcome1Price")) or _price(m.get("yesBuyPrice"))
            no = _price(m.get("outcome2Price")) or _price(m.get("noBuyPrice"))
            if yes is None and no is not None:
                yes = round(1.0 - no, 4)
            if yes is None:
                continue
            if no is None:
                no = round(1.0 - yes, 4)
            yes_id, no_id = _outcome_ids(m)
            out.append(MarketView(
                event_id=str(ev.get("id") or ""), market_id=str(m.get("id") or ""),
                event_title=str(ev.get("title") or ""), market_title=str(m.get("title") or ""),
                category=str(ev.get("category") or "").lower(), yes_price=yes, no_price=no,
                yes_outcome_id=yes_id, no_outcome_id=no_id, fee_pct=float(m.get("feePercentage") or 10),
                liquidity=float(ev.get("liquidity") or 0), orders=int(m.get("totalOrders") or 0),
                n_markets=len(markets), engine=str(ev.get("engine") or ""), status=str(m.get("status") or ev.get("status") or ""),
                closing_date=parse_ts(ev.get("closingDate")), created_at=parse_ts(ev.get("createdAt")),
            ))
    return out


def attach_history(views: Iterable[MarketView], payload: Any) -> None:
    by_market = {}
    for m in (payload or {}).get("markets", []) if isinstance(payload, dict) else []:
        by_market[m.get("marketId")] = sorted(m.get("priceHistory") or [], key=lambda pt: pt.get("e", 0))
    for v in views:
        v.history = by_market.get(v.market_id, [])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
