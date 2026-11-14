"""Agent settings, all from environment variables (see .env.example)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MIN_EDGE_FLOOR = 0.08  # post-fee edge floor; Bayse fees are ~5-7% of trade value


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, "") or default))
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw in (None, "") else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentConfig:
    dry_run: bool = True
    bankroll: float = 100.0
    currency: str = "USD"
    min_edge: float = MIN_EDGE_FLOOR
    kelly_multiplier: float = 0.25
    max_position_fraction: float = 0.05
    max_daily_loss_fraction: float = 0.10
    max_open_positions: int = 10
    max_trades_per_cycle: int = 3
    min_orders: int = 10
    min_liquidity: float = 0.0
    min_hours_to_close: float = 6.0
    max_days_to_close: float = 7.0
    scan_pages: int = 3
    scan_categories: tuple = ("politics", "sports", "entertainment", "economy", "culture")
    page_size: int = 20
    cycle_interval_seconds: int = 300
    database_path: str = "data/medes.db"
    research_top_n: int = 3
    llm_blend_weight: float = 0.25

    @classmethod
    def from_env(cls) -> "AgentConfig":
        min_edge = _f("MIN_EDGE", MIN_EDGE_FLOOR)
        if min_edge < MIN_EDGE_FLOOR:
            logger.warning("MIN_EDGE=%.3f is below the fee-aware floor; using %.2f", min_edge, MIN_EDGE_FLOOR)
            min_edge = MIN_EDGE_FLOOR
        kelly = min(0.5, max(0.25, _f("KELLY_FRACTION", 0.25)))
        return cls(
            dry_run=_b("DRY_RUN", True),
            bankroll=_f("BANKROLL", 100.0),
            currency=(os.getenv("CURRENCY") or "USD").upper(),
            min_edge=min_edge,
            kelly_multiplier=kelly,
            max_position_fraction=min(0.05, max(0.0, _f("MAX_POSITION_FRACTION", 0.05))),
            max_daily_loss_fraction=_f("MAX_DAILY_LOSS_FRACTION", 0.10),
            max_open_positions=_i("MAX_OPEN_POSITIONS", 10),
            max_trades_per_cycle=_i("MAX_TRADES_PER_CYCLE", 3),
            min_orders=_i("MIN_ORDERS", 10),
            min_liquidity=_f("MIN_LIQUIDITY", 0.0),
            min_hours_to_close=_f("MIN_HOURS_TO_CLOSE", 6.0),
            max_days_to_close=_f("MAX_DAYS_TO_CLOSE", 7.0),
            scan_pages=_i("SCAN_PAGES", 3),
            scan_categories=tuple(c.strip().lower() for c in (os.getenv("SCAN_CATEGORIES") or "politics,sports,entertainment,economy,culture").split(",") if c.strip()),
            page_size=_i("SCAN_PAGE_SIZE", 20),
            cycle_interval_seconds=_i("CYCLE_INTERVAL_SECONDS", 300),
            database_path=os.getenv("DATABASE_PATH") or "data/medes.db",
            research_top_n=_i("RESEARCH_TOP_N", 3),
            llm_blend_weight=min(0.5, max(0.0, _f("LLM_BLEND_WEIGHT", 0.25))),
        )
