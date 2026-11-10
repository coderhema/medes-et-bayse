"""Fee-aware edge filter and fractional-Kelly sizing for Bayse binary markets."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def fee_fraction(price: float, fee_pct: float = 10.0) -> float:
    """Bayse fee as a fraction of trade value: feeRate * max(1 - P, 0.5) (docs: concepts/fees)."""
    return (float(fee_pct or 10.0) / 100.0) * max(1.0 - price, 0.5)


def effective_price(price: float, fee_pct: float = 10.0) -> float:
    """All-in cost per $1-payout share once the fee is included."""
    return min(0.999, price * (1.0 + fee_fraction(price, fee_pct)))


@dataclass(frozen=True)
class Decision:
    outcome: str  # "YES" or "NO" (we only ever BUY)
    price: float  # market price of the outcome we buy
    effective_price: float
    p_win: float  # model probability that this outcome wins
    net_edge: float  # p_win - effective_price
    kelly_fraction: float  # fraction of bankroll after fractional Kelly + cap
    stake: float


def choose_side(
    p_yes: float,
    yes_price: float,
    no_price: Optional[float] = None,
    *,
    fee_pct: float = 10.0,
    min_edge: float = 0.08,
) -> Optional[tuple[str, float, float, float, float]]:
    """Return (outcome, price, effective_price, p_win, net_edge) for the best side, or None.

    The edge is measured *after fees*: p_win - P * (1 + fee). Only BUY orders are produced:
    a bearish view is expressed as BUY NO, never as SELL of shares we do not hold.
    """
    no_price = (1.0 - yes_price) if no_price is None else no_price
    best = None
    for outcome, price, p_win in (("YES", yes_price, p_yes), ("NO", no_price, 1.0 - p_yes)):
        if not (0.0 < price < 1.0):
            continue
        eff = effective_price(price, fee_pct)
        edge = p_win - eff
        if edge >= min_edge and (best is None or edge > best[4]):
            best = (outcome, price, eff, p_win, edge)
    return best


def kelly_fraction(p_win: float, effective_cost: float, *, fraction: float = 0.25, cap: float = 0.05) -> float:
    """Fractional Kelly for a binary share bought at effective_cost paying $1."""
    if not (0.0 < effective_cost < 1.0):
        return 0.0
    b = 1.0 / effective_cost - 1.0
    f = (b * p_win - (1.0 - p_win)) / b
    return max(0.0, min(cap, f * fraction))


def decide(
    p_yes: float,
    yes_price: float,
    no_price: Optional[float] = None,
    *,
    bankroll: float,
    fee_pct: float = 10.0,
    min_edge: float = 0.08,
    kelly_multiplier: float = 0.25,
    max_position_fraction: float = 0.05,
    min_stake: float = 1.0,
) -> Optional[Decision]:
    side = choose_side(p_yes, yes_price, no_price, fee_pct=fee_pct, min_edge=min_edge)
    if side is None:
        return None
    outcome, price, eff, p_win, edge = side
    frac = kelly_fraction(p_win, eff, fraction=kelly_multiplier, cap=max_position_fraction)
    stake = round(bankroll * frac, 2)
    if stake < min_stake:
        return None
    return Decision(outcome, price, eff, p_win, edge, frac, stake)


def settle_pnl(stake: float, effective_cost: float, won: bool) -> float:
    return stake * (1.0 / effective_cost - 1.0) if won else -stake
