"""Kelly Criterion strategy for Bayse Markets prediction bot.

The Kelly Criterion tells you what fraction of your bankroll to bet
given a known edge:

    f* = (b*p - q) / b

where:
    f* = fraction of bankroll to wager
    b  = net odds received on the wager (payout / stake - 1)
    p  = estimated probability the bet wins
    q  = 1 - p (probability the bet loses)

Bayse Markets uses AMM pricing so the current share price IS the
implied probability. If we believe the true probability differs from
the market price, that difference is our edge.

Edge = our_estimate - market_implied_probability
"""

from __future__ import annotations
import numpy as np
from loguru import logger
from bot.utils.bayesian import BayesianEstimator


class KellyStrategy:
    name = "Kelly Criterion"

    def __init__(
        self,
        bankroll: float,
        min_edge: float = 0.03,
        max_fraction: float = 0.05,
        use_fractional_kelly: float = 0.5,
    ):
        """
        Args:
            bankroll: Total capital in USDC.
            min_edge: Minimum required edge to trade (e.g., 0.03 = 3%).
            max_fraction: Hard cap on position size as fraction of bankroll.
            use_fractional_kelly: Multiply Kelly fraction by this (0.5 = half-Kelly).
                                  Reduces variance at cost of slightly lower EV.
        """
        self.bankroll = bankroll
        self.min_edge = min_edge
        self.max_fraction = max_fraction
        self.fractional = use_fractional_kelly
        self.estimator = BayesianEstimator()

    def _kelly_fraction(self, p_win: float, payout_ratio: float) -> float:
        """Compute the optimal Kelly bet fraction.

        Args:
            p_win: Our estimated probability of winning.
            payout_ratio: b in Kelly formula. For a $1 share that pays $1,
                          payout_ratio = (1 / share_price) - 1.

        Returns:
            Fraction of bankroll to bet (capped at max_fraction).
        """
        p_lose = 1.0 - p_win
        if payout_ratio <= 0:
            return 0.0
        kelly_f = (payout_ratio * p_win - p_lose) / payout_ratio
        kelly_f = max(0.0, kelly_f)  # Never short via Kelly
        kelly_f *= self.fractional     # Apply fractional Kelly
        return min(kelly_f, self.max_fraction)

    def scan(self, events: list[dict]) -> list[dict]:
        """Scan events for Kelly-positive opportunities.

        Returns a list of trade signals.
        """
        signals = []

        for event in events:
            event_id = event.get("id") or event.get("eventId")
            title = event.get("title") or event.get("name", "Unknown event")

            # Market-implied probabilities from share prices
            yes_price = float(event.get("yesPrice") or event.get("yes_price") or 0.5)
            no_price = float(event.get("noPrice") or event.get("no_price") or 0.5)

            if yes_price <= 0 or yes_price >= 1:
                continue

            # Use Bayesian estimator to form our belief about true probability
            # (In production: feed in historical data, news sentiment, etc.)
            our_yes_prob = self.estimator.estimate(yes_price, event)

            yes_edge = our_yes_prob - yes_price
            no_edge = (1 - our_yes_prob) - no_price

            best_side = None
            best_edge = 0.0

            if yes_edge > no_edge and yes_edge >= self.min_edge:
                best_side = "yes"
                best_edge = yes_edge
                p_win = our_yes_prob
                share_price = yes_price
            elif no_edge >= self.min_edge:
                best_side = "no"
                best_edge = no_edge
                p_win = 1 - our_yes_prob
                share_price = no_price

            if best_side is None:
                continue

            # Payout ratio: if you pay X for a share that pays $1 on win
            payout_ratio = (1.0 / share_price) - 1.0
            fraction = self._kelly_fraction(p_win, payout_ratio)

            if fraction <= 0:
                continue

            stake = round(self.bankroll * fraction, 2)
            if stake < 1.0:  # Bayse minimum trade is $1
                continue

            signals.append({
                "strategy": self.name,
                "event_id": event_id,
                "event_title": title,
                "side": best_side,
                "edge": best_edge,
                "our_prob": round(our_yes_prob if best_side == "yes" else 1 - our_yes_prob, 4),
                "market_prob": round(share_price, 4),
                "kelly_fraction": round(fraction, 4),
                "stake": stake,
            })
            logger.debug(
                f"[Kelly] {title} | {best_side.upper()} | edge={best_edge:.2%} | stake=${stake}"
            )

        return sorted(signals, key=lambda s: s["edge"], reverse=True)
