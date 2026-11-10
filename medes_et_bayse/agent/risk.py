"""Market filters and portfolio risk limits (daily loss budget, open positions)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..slm.features import CATEGORIES
from .config import AgentConfig
from .markets import MarketView
from .store import AgentStore


def market_filter_reason(view: MarketView, cfg: AgentConfig, now: datetime) -> Optional[str]:
    """Return why a market is skipped, or None if it is tradeable."""
    if view.status and view.status.lower() not in {"open", "active"}:
        return "not_open"
    if not view.yes_outcome_id or not view.no_outcome_id:
        return "missing_outcome_ids"
    if view.orders < cfg.min_orders:
        return "low_activity"
    if view.liquidity < cfg.min_liquidity:
        return "low_liquidity"
    htc = view.hours_to_close(now)
    if htc is None:
        return "no_closing_date"
    if htc < cfg.min_hours_to_close:
        return "closes_too_soon"
    if htc > cfg.max_days_to_close * 24:
        return "closes_too_far"
    if view.category not in CATEGORIES or view.category in {"crypto", "finance"}:
        # The SLM was not trained on short-horizon crypto/FX ladders; do not trade them.
        return "category_not_modelled"
    return None


class RiskManager:
    def __init__(self, cfg: AgentConfig, store: AgentStore) -> None:
        self.cfg, self.store = cfg, store

    @property
    def daily_loss_limit(self) -> float:
        return self.cfg.bankroll * self.cfg.max_daily_loss_fraction

    def daily_budget_left(self, now: datetime) -> float:
        """Worst-case budget: today's new stakes count as fully at risk, minus realized PnL."""
        at_risk = self.store.stakes_on_day(now) - self.store.realized_pnl_on_day(now)
        return max(0.0, self.daily_loss_limit - at_risk)

    def check(self, view: MarketView, stake: float, now: datetime) -> tuple[bool, str, float]:
        if self.store.has_open_position(view.market_id):
            return False, "already_positioned", 0.0
        if self.store.open_positions() >= self.cfg.max_open_positions:
            return False, "max_open_positions", 0.0
        budget = self.daily_budget_left(now)
        if budget < 1.0:
            return False, "daily_loss_limit", 0.0
        return True, "ok", round(min(stake, budget), 2)
