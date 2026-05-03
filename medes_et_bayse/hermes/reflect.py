from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from .db import HermesDatabase
from .predict import Prediction
from .trade import TradeResult


@dataclass(frozen=True)
class Reflection:
    summary: str
    lessons: list[str]
    recent_logs: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def reflect(store: HermesDatabase, prediction: Prediction, trade_result: TradeResult, *, run_id: Optional[str] = None) -> Reflection:
    recent_logs = [
        {
            "id": entry.id,
            "level": entry.level,
            "category": entry.category,
            "message": entry.message,
            "created_at": entry.created_at,
        }
        for entry in store.recent_logs(limit=10)
    ]
    lessons: list[str] = []
    if prediction.signal != "trade":
        lessons.append("Hold when no market clears the confidence threshold.")
    elif trade_result.status == "dry_run":
        lessons.append("Dry-run orders should still be recorded before live execution.")
    elif trade_result.status == "submitted":
        lessons.append("Track fills and revisit the thesis before the next cycle.")
    else:
        lessons.append("Recover from execution errors by keeping the next cycle conservative.")

    summary = f"Reviewed {prediction.market_title or 'the current market'} and finished the cycle with status {trade_result.status}."
    reflection = Reflection(summary=summary, lessons=lessons, recent_logs=recent_logs)
    store.log_event("reflect", summary, level="info", payload=reflection.to_dict(), run_id=run_id)
    store.remember("hermes", "last_reflection", reflection.to_dict(), run_id=run_id)
    return reflection
