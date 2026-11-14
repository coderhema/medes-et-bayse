"""Leakage-safe features for the Bayse prediction SLM.

Everything here is computable at decision time *t* from data the agent can see live:
the current YES price, the public YES price history up to t, the event's static metadata
(category, closing date, creation date, liquidity, fee, number of markets) and the
question text. Training and inference share this module so the two never drift apart.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

EPS = 1e-3
CATEGORIES = ("politics", "sports", "entertainment", "economy", "culture", "crypto", "finance")

NUMERIC_FEATURES = (
    "logit_p",
    "p",
    "extremity",
    "mom_12h",
    "mom_72h",
    "drift_from_open",
    "log_price_changes",
    "log_hours_to_close",
    "log_age_hours",
    "log_liquidity",
    "fee",
    "multi_market",
    "log_n_markets",
) + tuple(f"cat_{c}" for c in CATEGORIES) + ("cat_other",)


def logit(p: float) -> float:
    p = min(1 - EPS, max(EPS, float(p)))
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def price_at(history: Sequence[dict], t_ms: int) -> Optional[float]:
    """Last YES price at or before t_ms from a Bayse priceHistory list of {"e": ms, "p": price}."""
    best = None
    for pt in history:
        if pt.get("e", 0) <= t_ms:
            best = pt.get("p")
        else:
            break
    return best


@dataclass
class MarketSnapshot:
    """What the agent knows about one binary market at decision time."""

    yes_price: float
    as_of: datetime
    category: str = ""
    event_title: str = ""
    market_title: str = ""
    closing_date: Optional[datetime] = None
    created_at: Optional[datetime] = None
    liquidity: float = 0.0
    fee_pct: float = 10.0
    n_markets: int = 1
    history: list = field(default_factory=list)  # YES priceHistory points, any order

    @property
    def text(self) -> str:
        if self.market_title and self.market_title.strip().lower() not in {"yes", self.event_title.strip().lower()}:
            return f"{self.event_title} | {self.market_title}"
        return self.event_title


def numeric_features(s: MarketSnapshot) -> list[float]:
    t_ms = int(s.as_of.timestamp() * 1000)
    hist = sorted((pt for pt in s.history if pt.get("e", 0) <= t_ms and pt.get("p") is not None), key=lambda pt: pt["e"])
    lp = logit(s.yes_price)

    def lag(hours: float) -> float:
        prev = price_at(hist, t_ms - int(hours * 3600 * 1000))
        return lp - logit(prev) if prev is not None and 0 < prev < 1 else 0.0

    first = next((pt["p"] for pt in hist if 0 < pt["p"] < 1), None)
    changes = sum(1 for a, b in zip(hist, hist[1:]) if abs(a["p"] - b["p"]) > 1e-9)
    hours_to_close = max(0.0, (s.closing_date - s.as_of).total_seconds() / 3600) if s.closing_date else 24 * 30
    age_hours = max(0.0, (s.as_of - s.created_at).total_seconds() / 3600) if s.created_at else 0.0
    cat = (s.category or "").lower()
    feats = [
        lp,
        s.yes_price,
        abs(s.yes_price - 0.5),
        lag(12),
        lag(72),
        lp - logit(first) if first is not None else 0.0,
        math.log1p(changes),
        math.log1p(hours_to_close),
        math.log1p(age_hours),
        math.log1p(max(0.0, float(s.liquidity or 0))),
        float(s.fee_pct or 10) / 10.0,
        1.0 if (s.n_markets or 1) > 1 else 0.0,
        math.log(max(1, int(s.n_markets or 1))),
    ]
    feats += [1.0 if cat == c else 0.0 for c in CATEGORIES]
    feats.append(0.0 if cat in CATEGORIES else 1.0)
    return feats
