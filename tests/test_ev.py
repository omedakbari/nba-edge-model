"""Price parsing and EV math (src/betting/ev.py)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.betting import ev as E


@pytest.mark.parametrize("s,dec,kind", [
    ("-110", 1.9091, 'american'), ("+150", 2.5, 'american'), ("1.87", 1.87, 'decimal'),
    ("x1.85", 1.85, 'multiplier'), ("1.85x", 1.85, 'multiplier'),
])
def test_parse_price(s, dec, kind):
    d, k = E.parse_price(s)
    assert k == kind and d == pytest.approx(dec, abs=1e-3)


def test_kalshi_fee_raises_breakeven_above_price():
    d, k = E.parse_price("52c")
    assert k == 'kalshi' and 0.52 < E.breakeven(d) < 0.54


def test_ev_and_stake_zero_when_negative():
    assert E.ev(0.5, 1.9091) < 0
    assert E.stake_pct(0.5, 1.9091) == 0.0


def test_stake_is_capped():
    assert E.stake_pct(0.9, 3.0) == E.MAX_STAKE_PCT


def test_entry_ev_all_must_hit():
    p, v = E.entry_ev([0.6, 0.6], 3.0)
    assert p == pytest.approx(0.36) and v == pytest.approx(0.08)
