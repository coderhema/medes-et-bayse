"""Market-making strategy for Bayse Markets.

This version acts as a resting quote engine instead of a directional signal
scanner. It estimates fair value, applies an inventory-aware skew, caps risk,
and produces post-only bid/ask quotes that main.py can place into the market.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger


@dataclass(frozen=True)
class QuoteOrder:
    side: str
    price: float
    amount: float


class MarketMakerStrategy:
    name = 'Market Making'

    def __init__(
        self,
        bankroll: float,
        min_edge: float = 0.03,
        spread_threshold: float = 0.02,
        max_position_fraction: float = 0.03,
        inventory_skew: float = 0.60,
        quote_fraction: float = 0.50,
        max_spread: float = 0.12,
    ):
        self.bankroll = bankroll
        self.min_edge = min_edge
        self.spread_threshold = spread_threshold
        self.max_position_fraction = max_position_fraction
        self.inventory_skew = inventory_skew
        self.quote_fraction = quote_fraction
        self.max_spread = max_spread

    def scan(self, events: list[dict]) -> list[dict]:
        return self.generate_quotes(events)

    def generate_quotes(self, events: list[dict], portfolio: Any = None) -> list[dict]:
        quotes: list[dict] = []
        for event in events:
            plan = self._build_quote_plan(event, portfolio=portfolio)
            if plan is not None:
                quotes.append(plan)

        return sorted(
            quotes,
            key=lambda item: (
                -float(item.get('quote_notional', 0.0)),
                -float(item.get('inventory_ratio', 0.0)),
            ),
        )

    def _build_quote_plan(self, event: dict, portfolio: Any = None) -> Optional[dict]:
        event_id = str(event.get('id') or event.get('eventId') or '').strip()
        market_id = self._extract_market_id(event)
        outcome_id = self._extract_yes_outcome_id(event)
        if not event_id or not market_id or not outcome_id:
            return None

        title = str(event.get('title') or event.get('name') or 'Unknown event').strip()
        live_quote = self._live_quote(event)

        fair_value = self._compute_fair_value(event, live_quote)
        if fair_value is None:
            return None

        observed_edge = self._observed_edge(event, fair_value, live_quote)
        if observed_edge < self.min_edge:
            return None

        half_spread = self._quote_half_spread(fair_value, live_quote)
        if half_spread < self.spread_threshold / 2:
            half_spread = self.spread_threshold / 2
        half_spread = min(half_spread, self.max_spread / 2)

        inventory_units = self._inventory_units(portfolio, event_id=event_id, market_id=market_id, outcome_id=outcome_id)
        inventory_ratio = self._inventory_ratio(inventory_units, fair_value)
        skew = inventory_ratio * self.inventory_skew * half_spread

        quote_notional = self._quote_notional(inventory_ratio, fair_value)
        if quote_notional <= 0:
            return None

        bid_price = self._clamp(fair_value - half_spread - skew, 0.01, 0.99)
        ask_price = self._clamp(fair_value + half_spread - skew, 0.01, 0.99)
        if bid_price >= ask_price:
            ask_price = self._clamp(bid_price + 0.01, 0.01, 0.99)

        quote_orders = self._quote_orders(inventory_ratio, bid_price, ask_price, quote_notional)
        if not quote_orders:
            return None

        logger.debug(
            '[MM] {} | fair_value={:.4f} | bid={:.4f} | ask={:.4f} | inventory={:.4f} | notional={:.2f}',
            title,
            fair_value,
            bid_price,
            ask_price,
            inventory_units,
            quote_notional,
        )

        return {
            'strategy': self.name,
            'action': 'quote',
            'event_id': event_id,
            'market_id': market_id,
            'outcome_id': outcome_id,
            'event_title': title,
            'fair_value': round(fair_value, 4),
            'observed_edge': round(observed_edge, 4),
            'half_spread': round(half_spread, 4),
            'bid_price': round(bid_price, 4),
            'ask_price': round(ask_price, 4),
            'inventory_units': round(inventory_units, 4),
            'inventory_ratio': round(inventory_ratio, 4),
            'quote_notional': round(quote_notional, 2),
            'quote_orders': [order.__dict__ for order in quote_orders],
            'note': 'Resting quote engine with post-only bid and ask orders',
        }

    def _live_quote(self, event: dict) -> dict[str, Any]:
        live = event.get('liveQuote')
        return live if isinstance(live, dict) else {}

    def _extract_market_id(self, event: dict) -> str:
        market = event.get('market')
        if isinstance(market, dict):
            for key in ('id', 'marketId', 'market_id'):
                value = market.get(key)
                if value:
                    return str(value).strip()
        for key in ('marketId', 'market_id', 'id'):
            value = event.get(key)
            if value:
                return str(value).strip()
        return ''

    def _extract_yes_outcome_id(self, event: dict) -> str:
        for key in ('yesOutcomeId', 'yes_outcome_id', 'outcomeId', 'outcome_id'):
            value = event.get(key)
            if value:
                return str(value).strip()

        market = event.get('market')
        if isinstance(market, dict):
            for key in ('yesOutcomeId', 'yes_outcome_id', 'outcomeId', 'outcome_id'):
                value = market.get(key)
                if value:
                    return str(value).strip()
            outcomes = market.get('outcomes') or market.get('options')
            if isinstance(outcomes, list):
                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue
                    label = str(outcome.get('label') or outcome.get('name') or outcome.get('title') or '').strip().lower()
                    if label in {'yes', 'y', 'true'}:
                        outcome_id = outcome.get('id') or outcome.get('outcomeId') or outcome.get('outcome_id')
                        if outcome_id:
                            return str(outcome_id).strip()
        return ''

    def _compute_fair_value(self, event: dict, live_quote: dict[str, Any]) -> Optional[float]:
        candidates: list[float] = []

        live_mid = self._safe_float(live_quote.get('midpoint'))
        if live_mid is None:
            live_bid = self._safe_float(live_quote.get('bid'))
            live_ask = self._safe_float(live_quote.get('ask'))
            if live_bid is not None and live_ask is not None and live_ask >= live_bid:
                live_mid = (live_bid + live_ask) / 2.0
        if live_mid is not None:
            candidates.append(live_mid)

        yes_price = self._safe_float(event.get('yesPrice') or event.get('yes_price'))
        no_price = self._safe_float(event.get('noPrice') or event.get('no_price'))
        market_prob = self._safe_float(event.get('market_prob') or event.get('probability') or event.get('price'))

        if yes_price is not None:
            candidates.append(yes_price)
        if no_price is not None:
            candidates.append(1.0 - no_price)
        if market_prob is not None:
            candidates.append(market_prob)

        if not candidates:
            return None

        if live_mid is not None and len(candidates) > 1:
            static_candidates = [value for value in candidates if value != live_mid]
            static_mid = sum(static_candidates) / len(static_candidates) if static_candidates else live_mid
            fair_value = 0.65 * live_mid + 0.35 * static_mid
        else:
            fair_value = sum(candidates) / len(candidates)

        return self._clamp(fair_value, 0.01, 0.99)

    def _observed_edge(self, event: dict, fair_value: float, live_quote: dict[str, Any]) -> float:
        yes_price = self._safe_float(event.get('yesPrice') or event.get('yes_price'))
        no_price = self._safe_float(event.get('noPrice') or event.get('no_price'))
        observed = fair_value
        if yes_price is not None:
            observed = abs(observed - yes_price)
        elif no_price is not None:
            observed = abs(observed - (1.0 - no_price))
        elif live_quote:
            last = self._safe_float(live_quote.get('last'))
            midpoint = self._safe_float(live_quote.get('midpoint'))
            observed = abs(fair_value - (midpoint if midpoint is not None else last if last is not None else fair_value))
        return observed

    def _quote_half_spread(self, fair_value: float, live_quote: dict[str, Any]) -> float:
        live_bid = self._safe_float(live_quote.get('bid'))
        live_ask = self._safe_float(live_quote.get('ask'))
        observed_half_spread = 0.0
        if live_bid is not None and live_ask is not None and live_ask >= live_bid:
            observed_half_spread = max(0.005, (live_ask - live_bid) / 2.0)

        base = max(self.min_edge / 2.0, fair_value * 0.02, 0.01)
        return max(base, observed_half_spread * 0.75)

    def _inventory_units(self, portfolio: Any, *, event_id: str, market_id: str, outcome_id: str) -> float:
        if portfolio is None:
            return 0.0

        records: list[Any] = []
        if isinstance(portfolio, dict):
            for key in ('positions', 'outcomeBalances', 'balances', 'data', 'items'):
                value = portfolio.get(key)
                if isinstance(value, list):
                    records.extend(value)
                elif isinstance(value, dict):
                    records.extend(value.values())
            if not records:
                records.append(portfolio)
        elif isinstance(portfolio, list):
            records = list(portfolio)
        else:
            return 0.0

        total = 0.0
        for record in records:
            if not isinstance(record, dict):
                continue
            if not self._record_matches(record, event_id=event_id, market_id=market_id, outcome_id=outcome_id):
                continue
            amount = (
                self._safe_float(record.get('quantity'))
                or self._safe_float(record.get('qty'))
                or self._safe_float(record.get('amount'))
                or self._safe_float(record.get('position'))
                or self._safe_float(record.get('balance'))
                or self._safe_float(record.get('exposure'))
                or self._safe_float(record.get('units'))
                or 0.0
            )
            side = str(record.get('side') or record.get('direction') or '').strip().lower()
            outcome_label = str(record.get('outcome') or record.get('outcomeName') or record.get('label') or '').strip().lower()
            if side in {'sell', 'short', 'negative'} or outcome_label in {'no', 'short'}:
                amount *= -1.0
            elif outcome_id and str(record.get('outcomeId') or record.get('outcome_id') or '').strip() not in {'', outcome_id}:
                continue
            total += amount
        return total

    def _record_matches(self, record: dict, *, event_id: str, market_id: str, outcome_id: str) -> bool:
        for key in ('eventId', 'event_id', 'marketId', 'market_id', 'id'):
            value = record.get(key)
            if value and str(value).strip() in {event_id, market_id}:
                return True
        nested_market = record.get('market')
        if isinstance(nested_market, dict):
            for key in ('id', 'marketId', 'market_id', 'eventId', 'event_id'):
                value = nested_market.get(key)
                if value and str(value).strip() in {event_id, market_id}:
                    return True
        record_outcome = str(record.get('outcomeId') or record.get('outcome_id') or '').strip()
        if record_outcome and record_outcome == outcome_id:
            return True
        return False

    def _inventory_ratio(self, inventory_units: float, fair_value: float) -> float:
        max_notional = max(1.0, self.bankroll * self.max_position_fraction)
        max_units = max_notional / max(fair_value, 0.01)
        if max_units <= 0:
            return 0.0
        return self._clamp(inventory_units / max_units, -1.0, 1.0)

    def _quote_notional(self, inventory_ratio: float, fair_value: float) -> float:
        base = max(1.0, self.bankroll * self.max_position_fraction * self.quote_fraction)
        if abs(inventory_ratio) >= 0.95:
            base *= 0.35
        elif abs(inventory_ratio) >= 0.75:
            base *= 0.55
        elif abs(inventory_ratio) >= 0.50:
            base *= 0.75
        return round(min(base, self.bankroll * self.max_position_fraction), 2)

    def _quote_orders(self, inventory_ratio: float, bid_price: float, ask_price: float, notional: float) -> list[QuoteOrder]:
        if notional <= 0:
            return []

        if inventory_ratio >= 0.95:
            return [QuoteOrder(side='SELL', price=ask_price, amount=notional)]
        if inventory_ratio <= -0.95:
            return [QuoteOrder(side='BUY', price=bid_price, amount=notional)]
        return [
            QuoteOrder(side='BUY', price=bid_price, amount=notional),
            QuoteOrder(side='SELL', price=ask_price, amount=notional),
        ]

    def _safe_float(self, value: Any) -> Optional[float]:
        if value is None or value == '':
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _clamp(self, value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))
