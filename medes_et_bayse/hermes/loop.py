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

MAX_CONTEXT_CHARS = 5000
MAX_HISTORY_LOGS = 4
MAX_CONTEXT_VALUE_CHARS = 260
MAX_LIST_ITEMS = 4


def _truncate_text(value: Any, limit: int = MAX_CONTEXT_VALUE_CHARS) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _compact_fields(payload: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    compact: dict[str, Any] = {}
    for field in fields:
        value = payload.get(field)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            compact[field] = _truncate_text(value)
        elif isinstance(value, list):
            compact[field] = [
                _truncate_text(item) if isinstance(item, str) else item
                for item in value[:MAX_LIST_ITEMS]
            ]
        elif isinstance(value, dict):
            compact[field] = {
                key: _truncate_text(item) if isinstance(item, str) else item
                for key, item in list(value.items())[:MAX_LIST_ITEMS]
                if item not in (None, "", [], {})
            }
        else:
            compact[field] = value
    return compact


def _summarize_memory_value(key: str, value: Any) -> Any:
    if key == "last_prediction":
        return _compact_fields(
            value,
            (
                "event_id",
                "event_title",
                "market_id",
                "market_title",
                "side",
                "outcome",
                "price",
                "confidence",
                "signal",
                "currency",
                "rationale",
            ),
        )
    if key == "last_trade":
        summary = _compact_fields(
            value,
            (
                "attempted",
                "dry_run",
                "status",
                "message",
                "notional",
                "price",
                "side",
                "outcome",
            ),
        )
        order = _compact_fields(value.get("order") if isinstance(value, dict) else None, ("event_id", "market_id", "side", "outcome", "amount", "currency", "price"))
        if order:
            summary["order"] = order
        return summary
    if key == "last_reflection":
        summary = _compact_fields(value, ("summary", "lessons"))
        lessons = summary.get("lessons")
        if isinstance(lessons, list):
            summary["lessons"] = lessons[:MAX_LIST_ITEMS]
        return summary
    if key == "last_framework_response":
        return _truncate_text(value, limit=MAX_CONTEXT_VALUE_CHARS)
    if isinstance(value, dict):
        return _compact_fields(value, tuple(list(value.keys())[:MAX_LIST_ITEMS]))
    if isinstance(value, list):
        return [
            _truncate_text(item) if isinstance(item, str) else item
            for item in value[:MAX_LIST_ITEMS]
        ]
    return _truncate_text(value)


def _format_log_entry(entry: Any) -> dict[str, Any]:
    compact = {
        "id": getattr(entry, "id", None),
        "level": getattr(entry, "level", None),
        "category": getattr(entry, "category", None),
        "message": _truncate_text(getattr(entry, "message", ""), limit=200),
    }
    return {key: value for key, value in compact.items() if value not in (None, "", [], {})}


def _compact_snapshot_text(snapshot: dict[str, Any]) -> str:
    lines = [
        f'run_id={snapshot.get("run_id", "")}',
        "config=" + json.dumps(snapshot.get("config", {}), ensure_ascii=False, separators=(",", ":")),
    ]

    memory = snapshot.get("memory", {})
    for key in ("last_prediction", "last_trade", "last_reflection", "last_framework_response"):
        value = memory.get(key)
        if value in (None, "", [], {}):
            continue
        lines.append(f'{key}=' + json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    recent_logs = snapshot.get("recent_logs", [])
    if recent_logs:
        lines.append("recent_logs=")
        for log in recent_logs[:MAX_HISTORY_LOGS]:
            parts = [
                f"[{log.get('level', 'info')}]",
                _truncate_text(log.get('category', ''), limit=40),
                _truncate_text(log.get('message', ''), limit=160),
            ]
            lines.append("- " + " ".join(part for part in parts if part.strip()))

    text = "\n".join(lines).strip()
    if len(text) > MAX_CONTEXT_CHARS:
        return text[: MAX_CONTEXT_CHARS - 1].rstrip() + "…"
    return text

def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


@dataclass(frozen=True)
class HermesLoopConfig:
    cycle_interval_seconds: float = 300.0
    bankroll: float = 100.0
    trade_fraction: float = 0.05
    min_confidence: float = 0.58
    max_events: int = 20
    currency: str = "USD"
    dry_run: bool = True
    framework_model: str = "llama-3.1-8b-instant"
    framework_base_url: Optional[str] = None
    framework_api_key: Optional[str] = None
    framework_max_iterations: int = 6
    framework_skip_memory: bool = True

    @classmethod
    def from_env(cls) -> "HermesLoopConfig":
        groq_base_url = os.getenv("GROQ_BASE_URL", "").strip() or "https://api.groq.com/openai/v1"
        groq_api_key = os.getenv("GROQ_API_KEY", "").strip() or None
        framework_model = (
            os.getenv("GROQ_MODEL", "").strip()
            or os.getenv("HERMES_MODEL", "").strip()
            or os.getenv("POKE_MODEL", "").strip()
            or "llama-3.1-8b-instant"
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
            framework_base_url=groq_base_url,
            framework_api_key=groq_api_key,
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

        groq_api_key = (self.config.framework_api_key or "").strip()
        if not groq_api_key:
            raise RuntimeError("GROQ_API_KEY is required to initialize the Hermes framework agent.")

        groq_base_url = (self.config.framework_base_url or "").strip() or "https://api.groq.com/openai/v1"
        groq_model = (self.config.framework_model or "").strip() or "llama-3.1-8b-instant"

        return AIAgent(
            model=groq_model,
            provider="groq",
            api_key=groq_api_key,
            base_url=groq_base_url,
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
        def latest_memory_value(key: str) -> Any:
            entries = self.store.recall("hermes", key)[:1]
            if not entries:
                return None
            return self._parse_memory_value(entries[0].value)

        return {
            "run_id": run_id,
            "config": asdict(self.config),
            "memory": {
                "last_prediction": self._summarize_memory_value("last_prediction", latest_memory_value("last_prediction")),
                "last_trade": self._summarize_memory_value("last_trade", latest_memory_value("last_trade")),
                "last_reflection": self._summarize_memory_value("last_reflection", latest_memory_value("last_reflection")),
                "last_framework_response": self._summarize_memory_value("last_framework_response", latest_memory_value("last_framework_response")),
            },
            "recent_logs": [
                _format_log_entry(entry)
                for entry in self.store.recent_logs(limit=MAX_HISTORY_LOGS)
            ],
        }

    def _framework_context_text(self, run_id: str) -> str:
        snapshot = self._framework_snapshot(run_id)
        return _compact_snapshot_text(snapshot)

    def _framework_note(self, run_id: str) -> str:
        context_text = self._framework_context_text(run_id)
        result = self.framework.run_conversation(
            user_message=(
                "Review the current Hermes trading cycle state and return one concise operational note. "
                "Use the runtime snapshot as context, do not invent market data, and do not call tools."
            ),
            system_message=(
                "You are Hermes Agent embedded inside the medes-et-bayse repository. "
                "Your job is to produce a short framework note that stays grounded in the provided runtime snapshot. "
                "Keep the response concise and practical."
            ),
            conversation_history=[
                {
                    "role": "system",
                    "content": context_text,
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
