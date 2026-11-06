"""Sanity tests for market-anchored prop probabilities (src/models/market_blend.py)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.models import market_blend as mb
from src.data.espn_props import no_vig_consensus

pytestmark = pytest.mark.skipif(not mb.PARAMS_PATH.exists(), reason="market_blend.json not fit")


def test_shift_same_line_is_identity():
    assert mb.shift_line('pts', 20.5, 0.55, 20.5) == pytest.approx(0.55)


def test_shift_higher_line_lowers_p_over():
    base = mb.shift_line('pts', 20.5, 0.50, 20.5)
    assert mb.shift_line('pts', 20.5, 0.50, 21.5) < base < mb.shift_line('pts', 20.5, 0.50, 19.5)


def test_blend_stays_near_market_when_model_disagrees_wildly():
    # Model says 95% over, market says 50%: the honest answer is close to the market.
    p = mb.blend('pts', 0.50, 0.95)
    assert 0.50 < p < 0.62


def test_blend_agreement_returns_roughly_market():
    assert mb.blend('reb', 0.58, 0.58) == pytest.approx(0.58, abs=0.03)


def test_no_vig_consensus_pairs_sides_and_takes_modal_line():
    rows = [
        # (eid, date, provider, athlete, stat, side, line_o, price_o, line_c, price_c, updated)
        ('e', 'd', 'A', '1', 'pts', 'over', 20.5, 1.91, None, None, ''),
        ('e', 'd', 'A', '1', 'pts', 'under', 20.5, 1.91, None, None, ''),
        ('e', 'd', 'B', '1', 'pts', 'over', 20.5, 1.80, None, None, ''),
        ('e', 'd', 'B', '1', 'pts', 'under', 20.5, 2.02, None, None, ''),
        ('e', 'd', 'C', '1', 'pts', 'over', 21.5, 2.00, None, None, ''),
        ('e', 'd', 'C', '1', 'pts', 'under', 21.5, 1.83, None, None, ''),
        ('e', 'd', 'D', '1', 'pts', 'over', 19.5, 1.91, None, None, ''),   # unpaired → ignored
    ]
    out = no_vig_consensus(rows, use_current=False)[('1', 'pts')]
    assert out['line'] == 20.5 and out['n_books'] == 2
    assert 0.5 < out['p_over'] < 0.54          # book B leans over
