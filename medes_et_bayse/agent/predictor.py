"""Predict step: SLM probability, optionally blended with a Tavily+LLM view."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..slm.features import logit, sigmoid
from ..slm.model import BayseSLM
from . import research as research_mod
from .markets import MarketView


@dataclass
class Prediction:
    p_yes: float
    p_slm: float
    p_llm: Optional[float]
    source: str
    notes: dict


class Predictor:
    def __init__(self, slm: Optional[BayseSLM] = None, *, llm_blend_weight: float = 0.25) -> None:
        self.slm = slm or BayseSLM.load()
        self.llm_blend_weight = llm_blend_weight

    def predict_many(self, views: list[MarketView], now: datetime) -> list[float]:
        return self.slm.predict_proba([v.snapshot(now) for v in views])

    def refine(self, view: MarketView, p_slm: float) -> Prediction:
        """Research + optional LLM blend for a shortlisted market."""
        notes = research_mod.research(view.label) if research_mod.tavily_enabled() else {"enabled": False}
        p_llm = research_mod.llm_probability(view.label, view.yes_price, notes) if research_mod.llm_enabled() else None
        if p_llm is None:
            return Prediction(p_slm, p_slm, None, f"slm:{self.slm.version}", notes)
        w = self.llm_blend_weight
        p = sigmoid((1 - w) * logit(p_slm) + w * logit(p_llm))
        return Prediction(p, p_slm, p_llm, f"slm+llm(w={w})", notes)
