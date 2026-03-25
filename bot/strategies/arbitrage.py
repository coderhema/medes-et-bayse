"""Arbitrage detection strategy for Bayse Markets.

On an efficient binary prediction market, Yes price + No price = $1.00.
When Yes_price + No_price < 1.00, there is a riskless arbitrage:
  - Buy both Yes AND No shares
  - Guaranteed payout = $1.00 per pair
  - Cost = Yes_price + No_price < $1.00
  - Profit = $1.00 - (Yes_price + No_price) per pair

This arises due to:
  1. AMM slippage / low liquidity
  2. Slow price updates after a sharp news event
  3. Market-maker withdrawal creating temporary imbalance

Note: Bayse does NOT allow holding both sides of the same market
(per their FAQ). This strategy flags the opportunity for the operator
to decide whether to exploit it through multiple accounts or simply
use it as a signal to take the more mispriced side.
"""

from loguru import logger


class ArbitrageStrategy:
    name = "Arbitrage"

    def __init__(self, bankroll: float, min_edge: float = 0.02):
        self.bankroll = bankroll
        self.min_edge = min_edge  # Minimum arbitrage gap to flag

    def scan(self, events: list[dict]) -> list[dict]:
        """Scan for markets where Yes + No implied probabilities < 1.0."""
        signals = []

        for event in events:
            event_id = event.get("id") or event.get("eventId")
            title = event.get("title") or event.get("name", "Unknown event")

            yes_price = float(event.get("yesPrice") or event.get("yes_price") or 0.5)
            no_price = float(event.get("noPrice") or event.get("no_price") or 0.5)

            if yes_price <= 0 or no_price <= 0:
                continue

            total = yes_price + no_price
            gap = 1.0 - total  # Positive = arbitrage opportunity

            if gap < self.min_edge:
                continue

            # Determine which side is more mispriced (bigger discount)
            if yes_price < no_price:
                mispriced_side = "yes"
                implied_true_prob = 1.0 - no_price
                edge = implied_true_prob - yes_price
            else:
                mispriced_side = "no"
                implied_true_prob = 1.0 - yes_price
                edge = implied_true_prob - no_price

            # Size using a fixed fraction since this is near-riskless
            stake = round(min(self.bankroll * 0.10, 50.0), 2)  # Cap at $50 per arb

            signals.append({
                "strategy": self.name,
                "event_id": event_id,
                "event_title": title,
                "side": mispriced_side,
                "edge": round(edge, 4),
                "arb_gap": round(gap, 4),
                "yes_price": yes_price,
                "no_price": no_price,
                "total_implied": round(total, 4),
                "stake": stake,
                "note": "Bayse restricts holding both sides. Trade the more mispriced side only.",
            })
            logger.debug(
                f"[Arb] {title} | gap={gap:.2%} | best_side={mispriced_side} | edge={edge:.2%}"
            )

        return sorted(signals, key=lambda s: s["arb_gap"], reverse=True)
