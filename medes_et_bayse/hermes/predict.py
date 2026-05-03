from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

from ..client import BayseClient
from .db import HermesDatabase


@dataclass(frozen=True)
class Prediction:
    event_id: str
    event_title: str
    market_id: str
    market_title: str
    side: str
    outcome: str
    price: float
    confidence: float
    rationale: str
    currency: str = "USD"
    signal: str = "hold"
    event: Optional[dict[str, Any]] = None
    market: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any, default: str = "") -> str:
    result = str(value or "").strip()
    return result or default


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        number = float(value)
        if number != number:
            return None
        return number
    except (TypeError, ValueError):
        return None


def _extract_events(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("events", "items", "results", "data", "markets"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_events(value)
            if nested:
                return nested
    return []


def _event_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    markets = event.get("markets")
    if isinstance(markets, list):
        return [market for market in markets if isinstance(market, dict)]
    if any(key in event for key in ("yesBuyPrice", "noBuyPrice", "outcome1Price", "outcome2Price")):
        return [event]
    return []


def _market_prices(market: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    yes_price = _as_float(market.get("yesBuyPrice"))
    if yes_price is None:
        yes_price = _as_float(market.get("outcome1Price"))
    if yes_price is None:
        yes_price = _as_float(market.get("price"))

    no_price = _as_float(market.get("noBuyPrice"))
    if no_price is None:
        no_price = _as_float(market.get("outcome2Price"))
    if no_price is None and yes_price is not None:
        no_price = round(max(0.0, 1.0 - yes_price), 4)
    return yes_price, no_price


def _select_candidate(events: Iterable[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], Optional[float], Optional[float], float, str]:
    best_event: Optional[dict[str, Any]] = None
    best_market: Optional[dict[str, Any]] = None
    best_yes: Optional[float] = None
    best_no: Optional[float] = None
    best_score = -1.0
    best_side = "hold"
    for event in events:
        event_title = _text(event.get("title") or event.get("name") or event.get("question") or event.get("label") or "Untitled event")
        for market in _event_markets(event):
            market_title = _text(market.get("title") or market.get("name") or market.get("label") or event_title)
            yes_price, no_price = _market_prices(market)
            if yes_price is None and no_price is None:
                continue
            anchor = yes_price if yes_price is not None else 0.5
            spread = abs((yes_price or 0.5) - (no_price or (1.0 - anchor)))
            confidence = max(0.05, round(1.0 - abs(anchor - 0.5) * 1.9 - spread * 0.2, 4))
            side = "buy" if anchor < 0.5 else "sell"
            score = confidence - spread * 0.15
            if score > best_score:
                best_score = score
                best_event = event
                best_market = market
                best_yes = yes_price
                best_no = no_price
                best_side = side
    return best_event, best_market, best_yes, best_no, max(best_score, 0.0), best_side


def predict(client: BayseClient, store: HermesDatabase, *, max_events: int = 20, min_confidence: float = 0.58, run_id: Optional[str] = None) -> Prediction:
    payload = client.list_events(page=1, size=max_events, params={"status": "open"})
    events = _extract_events(payload)
    event, market, yes_price, no_price, score, side = _select_candidate(events)

    if event is None or market is None or score < min_confidence:
        prediction = Prediction(
            event_id="",
            event_title="",
            market_id="",
            market_title="",
            side="hold",
            outcome="",
            price=0.0,
            confidence=score,
            rationale="No market met the confidence threshold.",
            signal="hold",
        )
        store.log_event("predict", prediction.rationale, level="info", payload=prediction.to_dict(), run_id=run_id)
        store.remember("hermes", "last_prediction", prediction.to_dict(), run_id=run_id)
        return prediction

    event_id = _text(event.get("id") or event.get("eventId") or event.get("marketId"))
    market_id = _text(market.get("id") or market.get("marketId"))
    event_title = _text(event.get("title") or event.get("name") or event.get("question") or "Untitled event")
    market_title = _text(market.get("title") or market.get("name") or market.get("label") or event_title)
    currency = _text(event.get("currency") or market.get("currency") or "USD", "USD").upper()
    price = yes_price if side == "buy" else no_price
    if price is None:
        price = yes_price if yes_price is not None else 0.0
    outcome = "YES" if side == "buy" else "NO"
    rationale = f"Selected {market_title} because the implied price leans {side.upper()} and the spread is manageable."
    prediction = Prediction(
        event_id=event_id,
        event_title=event_title,
        market_id=market_id,
        market_title=market_title,
        side=side,
        outcome=outcome,
        price=float(price or 0.0),
        confidence=round(score, 4),
        rationale=rationale,
        currency=currency,
        signal="trade",
        event=event,
        market=market,
    )
    store.log_event("predict", rationale, level="info", payload=prediction.to_dict(), run_id=run_id)
    store.remember("hermes", "last_prediction", prediction.to_dict(), run_id=run_id)
    return prediction
