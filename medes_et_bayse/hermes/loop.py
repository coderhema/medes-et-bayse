from __future__ import annotations

from dataclasses import dataclass, asdict
import logging
import threading
import time
from typing import Any, Optional

from ..client import BayseClient
from .db import HermesDatabase
from .predict import Prediction, predict
from .reflect import Reflection, reflect
from .trade import TradeResult, execute_trade


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HermesLoopConfig:
    cycle_interval_seconds: float = 300.0
    bankroll: float = 100.0
    trade_fraction: float = 0.05
    min_confidence: float = 0.58
    max_events: int = 20
    currency: str = "USD"
    dry_run: bool = True

    @classmethod
    def from_env(cls) -> "HermesLoopConfig":
        import os

        return cls(
            cycle_interval_seconds=float(os.getenv("HERMES_CYCLE_INTERVAL_SECONDS", "300")),
            bankroll=float(os.getenv("HERMES_BANKROLL", os.getenv("BANKROLL", "100"))),
            trade_fraction=float(os.getenv("HERMES_TRADE_FRACTION", "0.05")),
            min_confidence=float(os.getenv("HERMES_MIN_CONFIDENCE", "0.58")),
            max_events=int(os.getenv("HERMES_MAX_EVENTS", "20")),
            currency=os.getenv("HERMES_CURRENCY", "USD"),
            dry_run=os.getenv("HERMES_DRY_RUN", os.getenv("DRY_RUN", "true")).lower() == "true",
        )


@dataclass
class HermesCycleResult:
    run_id: str
    prediction: Prediction
    trade_result: TradeResult
    reflection: Reflection

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "prediction": self.prediction.to_dict(),
            "trade_result": self.trade_result.to_dict(),
            "reflection": self.reflection.to_dict(),
        }


class HermesAgent:
    def __init__(self, client: BayseClient, store: HermesDatabase, config: Optional[HermesLoopConfig] = None) -> None:
        self.client = client
        self.store = store
        self.config = config or HermesLoopConfig.from_env()

    def cycle(self) -> HermesCycleResult:
        run_id = self.store.start_run(metadata=asdict(self.config))
        self.store.log_event("loop", "starting hermes cycle", level="info", run_id=run_id, payload=asdict(self.config))
        try:
            prediction = predict(
                self.client,
                self.store,
                max_events=self.config.max_events,
                min_confidence=self.config.min_confidence,
                run_id=run_id,
            )
            trade_result = execute_trade(
                self.client,
                self.store,
                prediction,
                bankroll=self.config.bankroll,
                trade_fraction=self.config.trade_fraction,
                dry_run=self.config.dry_run,
                currency=self.config.currency,
                run_id=run_id,
            )
            reflection = reflect(self.store, prediction, trade_result, run_id=run_id)
            result = HermesCycleResult(run_id=run_id, prediction=prediction, trade_result=trade_result, reflection=reflection)
            self.store.finish_run(run_id, status="completed", summary=reflection.summary, metadata=result.to_dict())
            self.store.log_event("loop", "finished hermes cycle", level="info", run_id=run_id, payload=result.to_dict())
            return result
        except Exception as exc:
            self.store.finish_run(run_id, status="failed", summary=str(exc))
            self.store.log_event("loop", f"hermes cycle failed: {exc}", level="error", run_id=run_id)
            raise

    def run_forever(self, *, stop_event: Optional[threading.Event] = None) -> None:
        stop_event = stop_event or threading.Event()
        logger.info("Hermes loop started with interval=%ss", self.config.cycle_interval_seconds)
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.cycle()
            except Exception:
                logger.exception("Hermes cycle failed")
            elapsed = time.monotonic() - started
            wait_for = max(0.0, self.config.cycle_interval_seconds - elapsed)
            stop_event.wait(wait_for)

    def run_once(self) -> HermesCycleResult:
        return self.cycle()
