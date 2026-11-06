"""
Price any bet, on any app, against our fair probability.

Every venue boils down to "pay X now, receive Y if it hits". This module turns
each venue's price format into a net decimal payout, then computes expected
value and a conservative stake.

Supported price strings (parse_price):
    -115, +120          American odds (sportsbooks)
    1.87                decimal odds
    x1.85, 1.85x        pick'em per-leg multiplier (Sleeper, Underdog, ...)
    52c, 0.52           prediction-market contract price (Kalshi) — fee applied

Backtest (2023-24, best price across books, blend fit on later seasons):
bets with EV >= 4% returned +7.5% ROI (95% CI +4.7% to +10.5%, 5,818 bets).
"""

import math
import re

# Kalshi taker fee: ceil_to_cent(0.07 × contracts × P × (1 − P)). Verify against
# kalshi.com/fee-schedule — some series carry different multipliers.
KALSHI_FEE_RATE = 0.07

MIN_EV = 0.04           # existing candidate-return policy; not independent model validation
KELLY_FRACTION = 0.25   # quarter Kelly — the fair probability itself has error
MAX_STAKE_PCT = 0.02    # never more than 2% of bankroll on one bet


def all_in_decimal(total_cost: float, winning_payout: float) -> float:
    """Use an actual order quote: cost includes every fee, payout is gross.

    This is the authoritative contract break-even basis, unlike a sportsbook
    price or the generic Kalshi fee estimate below.
    """
    if not (math.isfinite(total_cost) and math.isfinite(winning_payout)
            and 0 < total_cost < winning_payout):
        raise ValueError('All-in cost must be positive and below winning payout')
    return winning_payout / total_cost


def american_to_decimal(odds: float) -> float:
    if not math.isfinite(odds) or abs(odds) < 100:
        raise ValueError('American odds must be finite and have magnitude >= 100')
    return 1 + (odds / 100 if odds > 0 else 100 / -odds)


def kalshi_decimal(price: float, contracts: int = 100) -> float:
    """Effective decimal odds for buying a Kalshi contract at `price` (0-1), fee included.
    Fee is rounded up per order, so it's computed for a typical order size."""
    if not math.isfinite(price) or not 0 < price < 1 or not isinstance(contracts, int) or contracts <= 0:
        raise ValueError('Invalid contract price or order quantity')
    fee = math.ceil(KALSHI_FEE_RATE * contracts * price * (1 - price) * 100) / 100
    cost = price * contracts + fee
    return contracts / cost


def parse_price(s: str) -> tuple[float, str]:
    """Return (decimal odds incl. fees, venue kind) for a price string."""
    t = str(s).strip().lower().replace(' ', '')
    if re.fullmatch(r'[+-]\d{3,5}', t):
        return american_to_decimal(float(t)), 'american'
    m = re.fullmatch(r'x(\d+(\.\d+)?)|(\d+(\.\d+)?)x', t)
    if m:
        v = float(m.group(1) or m.group(3))
        if not math.isfinite(v) or v <= 1:
            raise ValueError('Multiplier must be finite and > 1')
        return v, 'multiplier'
    m = re.fullmatch(r'(\d{1,2}(\.\d+)?)c', t)
    if m:
        return kalshi_decimal(float(m.group(1)) / 100), 'kalshi'
    v = float(t)
    if 0 < v < 1:
        return kalshi_decimal(v), 'kalshi'
    if math.isfinite(v) and v > 1:
        return v, 'decimal'
    raise ValueError(f"unrecognized price: {s}")


def breakeven(decimal_odds: float) -> float:
    if not math.isfinite(decimal_odds) or decimal_odds <= 1:
        raise ValueError('Decimal odds must be finite and > 1')
    return 1 / decimal_odds


def ev(p: float, decimal_odds: float) -> float:
    """Expected profit per $1 staked."""
    breakeven(decimal_odds)
    if not math.isfinite(p) or not 0 <= p <= 1:
        raise ValueError('Probability must be finite and in [0, 1]')
    return p * decimal_odds - 1


def stake_pct(p: float, decimal_odds: float) -> float:
    """Fraction of bankroll: quarter Kelly, capped. 0 when -EV."""
    ev(p, decimal_odds)
    b = decimal_odds - 1
    if b <= 0:
        return 0.0
    k = (p * decimal_odds - 1) / b
    return max(0.0, min(k * KELLY_FRACTION, MAX_STAKE_PCT))


def entry_ev(probs: list[float], total_decimal: float) -> tuple[float, float]:
    """All-legs-must-hit entry (parlay / pick'em power play).
    Returns (P(all hit), EV). Assumes independent legs — same-game legs are
    correlated and this will misstate them."""
    p_all = math.prod(probs)
    return p_all, ev(p_all, total_decimal)
