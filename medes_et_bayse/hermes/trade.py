from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from ..client import BayseClient, BayseClientError
from .db import HermesDatabase
from .predict import Prediction


@dataclass(frozen=True)
class TradeResult:
    attempted: bool
    dry_run: bool
    status: str
    message: str
    order: Optional[dict[str, Any]] = None
    notional: float = 0.0
    price: float = 0.0
    side: str = "hold"
    outcome: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def execute_trade(
    client: BayseClient,
    store: HermesDatabase,
    prediction: Prediction,
    *,
    bankroll: float = 100.0,
    trade_fraction: float = 0.05,
    dry_run: bool = True,
    currency: Optional[str] = None,
    run_id: Optional[str] = None,
) -> TradeResult:
    if prediction.signal != "trade" or prediction.side not in {"buy", "sell"}:
        result = TradeResult(attempted=False, dry_run=dry_run, status="skipped", message="No trade signal was produced.")
        store.log_event("trade", result.message, level="info", payload=result.to_dict(), run_id=run_id)
        store.remember("hermes", "last_trade", result.to_dict(), run_id=run_id)
        return result

    notional = max(1.0, round(bankroll * trade_fraction, 2))
    trade_currency = (currency or prediction.currency or "USD").upper()
    outcome = prediction.outcome or ("YES" if prediction.side == "buy" else "NO")
    price = float(prediction.price or 0.0)

    if dry_run:
        result = TradeResult(
            attempted=True,
            dry_run=True,
            status="dry_run",
            message=f"Dry-run trade prepared for {prediction.market_title}.",
            order={
                "event_id": prediction.event_id,
                "market_id": prediction.market_id,
                "side": prediction.side.upper(),
                "outcome": outcome,
                "amount": notional,
                "currency": trade_currency,
                "price": price,
            },
            notional=notional,
            price=price,
            side=prediction.side,
            outcome=outcome,
        )
        store.log_event("trade", result.message, level="info", payload=result.to_dict(), run_id=run_id)
        store.remember("hermes", "last_trade", result.to_dict(), run_id=run_id)
        return result

    try:
        order = client.place_order(
            prediction.event_id,
            prediction.market_id,
            outcome=outcome,
            side=prediction.side,
            amount=notional,
            currency=trade_currency,
            price=price if price > 0 else None,
        )
        result = TradeResult(
            attempted=True,
            dry_run=False,
            status="submitted",
            message=f"Trade submitted for {prediction.market_title}.",
            order=order,
            notional=notional,
            price=price,
            side=prediction.side,
            outcome=outcome,
        )
        store.log_event("trade", result.message, level="info", payload=result.to_dict(), run_id=run_id)
        store.remember("hermes", "last_trade", result.to_dict(), run_id=run_id)
        return result
    except BayseClientError as exc:
        result = TradeResult(attempted=True, dry_run=False, status="error", message=str(exc), notional=notional, price=price, side=prediction.side, outcome=outcome)
        store.log_event("trade", result.message, level="error", payload=result.to_dict(), run_id=run_id)
        store.remember("hermes", "last_trade", result.to_dict(), run_id=run_id)
        return result
    except Exception as exc:
        result = TradeResult(attempted=True, dry_run=False, status="error", message=str(exc), notional=notional, price=price, side=prediction.side, outcome=outcome)
        store.log_event("trade", result.message, level="error", payload=result.to_dict(), run_id=run_id)
        store.remember("hermes", "last_trade", result.to_dict(), run_id=run_id)
        return result
