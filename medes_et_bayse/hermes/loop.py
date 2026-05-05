from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import os
import threading
import time
from typing import Any, Optional

from ..client import BayseClient
from .db import HermesDatabase
from .predict import Prediction, predict
from .reflect import Reflection, reflect
from .trade import TradeResult, execute_trade

try:
    from run_agent import AIAgent
except Exception as exc:  # pragma: no cover
    AIAgent = None
    _HERMES_IMPORT_ERROR = exc
else:
    _HERMES_IMPORT_ERROR = None


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
    framework_model: str = "gpt-4.1-mini"
    framework_base_url: Optional[str] = None
    framework_api_key: Optional[str] = None
    framework_max_iterations: int = 6
    framework_skip_memory: bool = True

    @classmethod
    def from_env(cls) -> "HermesLoopConfig":
        brain_url = os.getenv("POKE_BRAIN_URL", "").strip() or os.getenv("POKE_API_BRAIN_URL", "").strip() or None
        poke_api_key = os.getenv("POKE_API_KEY", "").strip() or None
        framework_model = (
            os.getenv("HERMES_MODEL", "").strip()
            or os.getenv("POKE_MODEL", "").strip()
            or "gpt-4.1-mini"
        )
        return cls(
            cycle_interval_seconds=float(os.getenv("HERMES_CYCLE_INTERVAL_SECONDS", "300")),
            bankroll=float(os.getenv("HERMES_BANKROLL", os.getenv("BANKROLL", "100"))),
            trade_fraction=float(os.getenv("HERMES_TRADE_FRACTION", "0.05")),
            min_confidence=float(os.getenv("HERMES_MIN_CONFIDENCE", "0.58")),
            max_events=int(os.getenv("HERMES_MAX_EVENTS", "20")),
            currency=os.getenv("HERMES_CURRENCY", "USD"),
            dry_run=os.getenv("HERMES_DRY_RUN", os.getenv("DRY_RUN", "true")).lower() == "true",
            framework_model=framework_model,
            framework_base_url=brain_url,
            framework_api_key=poke_api_key,
            framework_max_iterations=int(os.getenv("HERMES_FRAMEWORK_MAX_ITERATIONS", "6")),
            framework_skip_memory=os.getenv("HERMES_FRAMEWORK_SKIP_MEMORY", "true").lower() == "true",
        )


@dataclass
class HermesCycleResult:
    run_id: str
    framework_response: str
    prediction: Prediction
    trade_result: TradeResult
    reflection: Reflection

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "framework_response": self.framework_response,
            "prediction": self.prediction.to_dict(),
            "trade_result": self.trade_result.to_dict(),
            "reflection": self.reflection.to_dict(),
        }


class HermesAgent:
    def __init__(self, client: BayseClient, store: HermesDatabase, config: Optional[HermesLoopConfig] = None) -> None:
        self.client = client
        self.store = store
        self.config = config or HermesLoopConfig.from_env()
        self.framework = self._build_framework_agent()

    def _build_framework_agent(self) -> Any:
        if AIAgent is None:  # pragma: no cover
            raise RuntimeError(f"hermes-agent is required to run the embedded framework: {_HERMES_IMPORT_ERROR}")

        return AIAgent(
            model=self.config.framework_model,
            api_key=self.config.framework_api_key,
            base_url=self.config.framework_base_url,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=self.config.framework_skip_memory,
            max_iterations=self.config.framework_max_iterations,
        )

    @staticmethod
    def _parse_memory_value(value: str) -> Any:
        try:
            return json.loads(value)
        except Exception:
            return value

    def _framework_snapshot(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "config": asdict(self.config),
            "memory": {
                "last_prediction": [
                    self._parse_memory_value(entry.value)
                    for entry in self.store.recall("hermes", "last_prediction")[:1]
                ],
                "last_trade": [
                    self._parse_memory_value(entry.value)
                    for entry in self.store.recall("hermes", "last_trade")[:1]
                ],
                "last_reflection": [
                    self._parse_memory_value(entry.value)
                    for entry in self.store.recall("hermes", "last_reflection")[:1]
                ],
                "last_framework_response": [
                    self._parse_memory_value(entry.value)
                    for entry in self.store.recall("hermes", "last_framework_response")[:1]
                ],
            },
            "recent_logs": [
                {
                    "id": entry.id,
                    "level": entry.level,
                    "category": entry.category,
                    "message": entry.message,
                    "created_at": entry.created_at,
                }
                for entry in self.store.recent_logs(limit=8)
            ],
        }

    def _framework_note(self, run_id: str) -> str:
        snapshot = self._framework_snapshot(run_id)
        result = self.framework.run_conversation(
            user_message=(
                "Review the current Hermes trading cycle state and return one concise operational note. "
                "Use the SQLite snapshot as context, do not invent market data, and do not call tools."
            ),
            system_message=(
                "You are Hermes Agent embedded inside the medes-et-bayse repository. "
                "Your job is to produce a short framework note that stays grounded in the provided SQLite snapshot. "
                "Keep the response concise and practical."
            ),
            conversation_history=[
                {
                    "role": "system",
                    "content": json.dumps(snapshot, ensure_ascii=False, default=str, indent=2),
                }
            ],
            task_id=run_id,
        )
        final_response = result.get("final_response") if isinstance(result, dict) else None
        note = str(final_response or "").strip()
        return note or "Hermes framework completed the cycle state review."

    def cycle(self) -> HermesCycleResult:
        run_id = self.store.start_run(metadata=asdict(self.config))
        self.store.log_event("loop", "starting hermes cycle", level="info", run_id=run_id, payload=asdict(self.config))
        try:
            framework_response = self._framework_note(run_id)
            self.store.log_event("framework", framework_response, level="info", run_id=run_id, payload={"run_id": run_id})
            self.store.remember("hermes", "last_framework_response", framework_response, run_id=run_id)

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
            result = HermesCycleResult(
                run_id=run_id,
                framework_response=framework_response,
                prediction=prediction,
                trade_result=trade_result,
                reflection=reflection,
            )
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
