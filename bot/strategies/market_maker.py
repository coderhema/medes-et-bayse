"""Market-making strategy for Bayse Markets.

Market makers profit by capturing the bid-ask spread — quoting both
buy and sell prices and profiting when trades hit both sides.

On Bayse (an AMM), we adapt this as follows:
  - Find markets where the spread between YES and NO is wide
  - Take a position on the under-priced side
  - Set a limit-style exit when the price mean-reverts

Market-making edge comes from:
  1. Mean-reversion: prices oscillate around true probability
  2. Time decay: as the event approaches, prices converge toward 0 or 1
  3. Liquidity premium: thin markets have wider spreads you can capture

Best for markets with:
  - High volume (many traders)
  - Long time horizon (days/weeks until resolution)
  - No single sharp information event imminent
"""

from loguru import logger


class MarketMakerStrategy:
    name = "Market Making"

    def __init__(
        self,
        bankroll: float,
        min_edge: float = 0.04,
        spread_threshold: float = 0.10,
        max_position_fraction: float = 0.03,
    ):
        """
        Args:
            bankroll: Total capital.
            min_edge: Minimum deviation from 0.5 to trade.
            spread_threshold: Flag market if |yes - (1-no)| > this value.
            max_position_fraction: Max position per market.
        """
        self.bankroll = bankroll
        self.min_edge = min_edge
        self.spread_threshold = spread_threshold
        self.max_position_fraction = max_position_fraction

    def scan(self, events: list[dict]) -> list[dict]:
        """Find markets with wide spreads suitable for mean-reversion trades."""
        signals = []

        for event in events:
            event_id = event.get("id") or event.get("eventId")
            title = event.get("title") or event.get("name", "Unknown event")

            yes_price = float(event.get("yesPrice") or event.get("yes_price") or 0.5)
            no_price = float(event.get("noPrice") or event.get("no_price") or 0.5)

            if yes_price <= 0 or no_price <= 0:
                continue

            # Mid-price: the fair value implied by the market
            yes_implied_by_no = 1.0 - no_price
            spread = abs(yes_price - yes_implied_by_no)

            if spread < self.spread_threshold:
                continue

            # Trade the cheaper side (which is under-valued relative to its complement)
            if yes_price < yes_implied_by_no:
                side = "yes"
                edge = yes_implied_by_no - yes_price
                price = yes_price
            else:
                side = "no"
                edge = yes_price - yes_implied_by_no  # i.e. no is cheap
                price = no_price

            if edge < self.min_edge:
                continue

            stake = round(self.bankroll * self.max_position_fraction, 2)

            signals.append({
                "strategy": self.name,
                "event_id": event_id,
                "event_title": title,
                "side": side,
                "edge": round(edge, 4),
                "spread": round(spread, 4),
                "yes_price": yes_price,
                "no_price": no_price,
                "stake": stake,
                "note": "Mean-reversion / market-making entry",
            })
            logger.debug(
                f"[MM] {title} | side={side} | spread={spread:.2%} | edge={edge:.2%}"
            )

        return sorted(signals, key=lambda s: s["edge"], reverse=True)
