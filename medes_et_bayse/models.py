from __future__ import annotations

import dataclasses
from typing import Any, Dict, Mapping, Optional


def _unwrap_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        for key in ("data", "result", "quote", "order"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                return value
    return payload


def _coerce_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


@dataclasses.dataclass(frozen=True)
class BayseError:
    code: Optional[str]
    message: str
    details: Optional[Dict[str, Any]] = None
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "BayseError":
        base = _unwrap_payload(payload)
        return cls(
            code=_coerce_str(base.get("code") or base.get("errorCode") or base.get("statusCode")),
            message=_coerce_str(base.get("message") or base.get("error") or base.get("detail") or "Unknown Bayse error") or "Unknown Bayse error",
            details=base.get("details") if isinstance(base.get("details"), dict) else None,
            raw=dict(payload),
        )


@dataclasses.dataclass(frozen=True)
class Quote:
    symbol: Optional[str]
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    mark: Optional[float] = None
    midpoint: Optional[float] = None
    timestamp: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Quote":
        base = _unwrap_payload(payload)
        return cls(
            symbol=_coerce_str(base.get("symbol") or base.get("ticker") or base.get("instrument") or base.get("asset")),
            bid=_coerce_float(base.get("bid") or base.get("bestBid") or base.get("bidPrice")),
            ask=_coerce_float(base.get("ask") or base.get("bestAsk") or base.get("askPrice")),
            last=_coerce_float(base.get("last") or base.get("lastPrice") or base.get("price") or base.get("tradePrice")),
            mark=_coerce_float(base.get("mark") or base.get("markPrice")),
            midpoint=_coerce_float(base.get("midpoint") or base.get("mid") or base.get("midPrice")),
            timestamp=_coerce_str(base.get("timestamp") or base.get("ts") or base.get("time") or base.get("updatedAt")),
            raw=dict(payload),
        )


@dataclasses.dataclass(frozen=True)
class Order:
    order_id: Optional[str]
    client_order_id: Optional[str] = None
    symbol: Optional[str] = None
    event_id: Optional[str] = None
    market_id: Optional[str] = None
    outcome_id: Optional[str] = None
    side: Optional[str] = None
    order_type: Optional[str] = None
    status: Optional[str] = None
    quantity: Optional[float] = None
    amount: Optional[float] = None
    limit_price: Optional[float] = None
    filled_quantity: Optional[float] = None
    average_fill_price: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Order":
        base = _unwrap_payload(payload)
        amount = _coerce_float(base.get("amount") or base.get("qty") or base.get("size"))
        quantity = _coerce_float(base.get("quantity") or base.get("qty") or base.get("size") or base.get("amount"))
        return cls(
            order_id=_coerce_str(base.get("orderId") or base.get("id") or base.get("order_id")),
            client_order_id=_coerce_str(base.get("clientOrderId") or base.get("client_order_id") or base.get("clientId")),
            symbol=_coerce_str(base.get("symbol") or base.get("ticker") or base.get("instrument")),
            event_id=_coerce_str(base.get("eventId") or base.get("event_id")),
            market_id=_coerce_str(base.get("marketId") or base.get("market_id")),
            outcome_id=_coerce_str(base.get("outcomeId") or base.get("outcome_id")),
            side=_coerce_str(base.get("side") or base.get("direction")),
            order_type=_coerce_str(base.get("type") or base.get("orderType") or base.get("order_type")),
            status=_coerce_str(base.get("status") or base.get("state")),
            quantity=quantity,
            amount=amount,
            limit_price=_coerce_float(base.get("limitPrice") or base.get("price") or base.get("limit_price")),
            filled_quantity=_coerce_float(base.get("filledQuantity") or base.get("filledQty") or base.get("filled_quantity") or base.get("filled")),
            average_fill_price=_coerce_float(base.get("averageFillPrice") or base.get("avgFillPrice") or base.get("average_fill_price")),
            created_at=_coerce_str(base.get("createdAt") or base.get("created_at") or base.get("filledAt")),
            updated_at=_coerce_str(base.get("updatedAt") or base.get("updated_at")),
            raw=dict(payload),
        )


@dataclasses.dataclass(frozen=True)
class QuoteResponse:
    quote: Quote
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QuoteResponse":
        return cls(quote=Quote.from_dict(payload), raw=dict(payload))


@dataclasses.dataclass(frozen=True)
class OrderResponse:
    order: Order
    raw: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OrderResponse":
        return cls(order=Order.from_dict(payload), raw=dict(payload))
