"""Quant Advisory: fair-value opinion layer for Telegram users.

Reuses the fair-value and spread helpers from market_maker.py to produce
a concise edge/confidence readout that Telegram users see before placing
a trade.  The module is intentionally pure-Python with no I/O so that it
can be unit-tested without a live API connection.
"""

from __future__ import annotations

from typing import Any, Optional

from bot.strategies.market_maker import compute_fair_value, compute_half_spread


class QuantAdvisory:
    """Generates a quant advisory opinion for a Bayse market event.

    Parameters
    ----------
    min_edge:
        Minimum absolute edge (|fair_value - market_price|) required before
        the verdict recommends a directional trade.  Defaults to 0.03 (3 ¢).
    """

    def __init__(self, min_edge: float = 0.03) -> None:
        self.min_edge = min_edge

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_opinion(self, event: dict) -> dict:
        """Return a structured quant opinion for *event*.

        The returned dict always contains an ``"available"`` key.  If
        ``available`` is ``False`` a ``"reason"`` key explains why.

        When ``available`` is ``True`` the dict contains:

        * ``fair_value``       – blended fair-value probability [0.01, 0.99]
        * ``market_price``     – observed mid-price (or ``None``)
        * ``edge``             – fair_value − market_price (signed)
        * ``edge_pct``         – edge expressed as a percentage
        * ``half_spread``      – half the observed bid/ask spread
        * ``spread``           – full bid/ask spread
        * ``risk_reward``      – |edge| / half_spread (higher is better)
        * ``confidence``       – float in [0, 1] derived from spread width
        * ``confidence_label`` – "high" | "moderate" | "low"
        * ``verdict_signal``   – "BUY YES" | "BUY NO" | "HOLD"
        * ``verdict``          – human-readable one-liner
        * ``volume``           – raw volume figure from the event (or ``None``)
        """
        live_quote = self._live_quote(event)
        fair_value = compute_fair_value(event, live_quote)

        if fair_value is None:
            return {"available": False, "reason": "insufficient price data"}

        market_price = self._market_price(event, live_quote)
        edge = (fair_value - market_price) if market_price is not None else 0.0

        half_spread = compute_half_spread(fair_value, live_quote)
        spread = half_spread * 2.0

        # Risk/Reward: potential gain in spread-units per unit of spread risk.
        risk_reward = abs(edge) / half_spread if half_spread > 0 else 0.0

        # Confidence: narrow spread → liquid market → higher confidence.
        # A spread of 0.10 (10 ¢) or wider is treated as zero confidence.
        max_uncertain_spread = 0.10
        confidence = max(0.0, min(1.0, 1.0 - spread / max_uncertain_spread))

        confidence_label = self._confidence_label(confidence)

        # Directional verdict.
        if abs(edge) < self.min_edge:
            verdict_signal = "HOLD"
        elif edge > 0:
            verdict_signal = "BUY YES"
        else:
            verdict_signal = "BUY NO"

        verdict = f"{verdict_signal} — {confidence_label} confidence"

        volume = self._safe_float(
            event.get("volume")
            or event.get("totalVolume")
            or event.get("openInterest")
        )

        return {
            "available": True,
            "fair_value": round(fair_value, 4),
            "market_price": round(market_price, 4) if market_price is not None else None,
            "edge": round(edge, 4),
            "edge_pct": round(edge * 100, 2),
            "half_spread": round(half_spread, 4),
            "spread": round(spread, 4),
            "risk_reward": round(risk_reward, 2),
            "confidence": round(confidence, 3),
            "confidence_label": confidence_label,
            "verdict_signal": verdict_signal,
            "verdict": verdict,
            "volume": volume,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _live_quote(event: dict) -> dict[str, Any]:
        live = event.get("liveQuote")
        return live if isinstance(live, dict) else {}

    def _market_price(self, event: dict, live_quote: dict[str, Any]) -> Optional[float]:
        """Determine the current observed mid-price for the market."""
        mid = self._safe_float(live_quote.get("midpoint"))
        if mid is None:
            bid = self._safe_float(live_quote.get("bid"))
            ask = self._safe_float(live_quote.get("ask"))
            if bid is not None and ask is not None and ask >= bid:
                mid = (bid + ask) / 2.0
        if mid is not None:
            return mid

        yes_price = self._safe_float(event.get("yesPrice") or event.get("yes_price"))
        if yes_price is not None:
            return yes_price

        return self._safe_float(
            event.get("market_prob") or event.get("probability") or event.get("price")
        )

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _confidence_label(confidence: float) -> str:
        if confidence >= 0.75:
            return "high"
        if confidence >= 0.40:
            return "moderate"
        return "low"


# ------------------------------------------------------------------
# Telegram-friendly formatter
# ------------------------------------------------------------------

def format_quant_opinion(opinion: dict, title: str = "") -> str:
    """Format a quant opinion dict as a Telegram HTML snippet.

    Parameters
    ----------
    opinion:
        Dict returned by :meth:`QuantAdvisory.generate_opinion`.
    title:
        Optional market/event title to include in the header.
    """
    if not opinion.get("available"):
        reason = opinion.get("reason", "insufficient data")
        return f"🔬 <b>Quant Opinion</b>: {reason}"

    fv: float = opinion["fair_value"]
    mp: Optional[float] = opinion.get("market_price")
    edge: float = opinion["edge"]
    edge_pct: float = opinion["edge_pct"]
    spread: float = opinion["spread"]
    rr: float = opinion["risk_reward"]
    confidence_label: str = opinion["confidence_label"]
    verdict: str = opinion["verdict"]

    header = "🔬 <b>Quant Opinion</b>"
    if title:
        header += f": {title}"

    arrow = "↑" if edge > 0 else ("↓" if edge < 0 else "→")
    edge_sign = "+" if edge >= 0 else ""

    lines = [
        header,
        f"Fair Value: <b>{fv:.4f}</b> ({fv * 100:.1f}%)",
    ]
    if mp is not None:
        lines.append(f"Market Price: {mp:.4f} ({mp * 100:.1f}%)")
        lines.append(f"Edge: <b>{edge_sign}{edge_pct:.2f}%</b> {arrow}")
    lines += [
        f"Spread: {spread:.4f} | R/R: {rr:.2f}:1",
        f"Confidence: {confidence_label}",
        f"Verdict: <b>{verdict}</b>",
    ]
    return "\n".join(lines)
