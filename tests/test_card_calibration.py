"""src/models/calibration.py: fit, time split, selected-subset report, reliability math, ledger offset."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models import calibration as C  # noqa: E402


def synthetic_replay(n_per_day=600, seed=0) -> pd.DataFrame:
    """Market mid is the truth; the hit-rate history is pure noise around it (like the real replay)."""
    rng = np.random.default_rng(seed)
    days = [f'2026-11-{d:02d}' for d in range(11, 24)]
    rows = []
    for day in days:
        mid = rng.uniform(0.2, 0.9, n_per_day)
        hist = np.clip(mid + rng.normal(0, 0.25, n_per_day), 0.02, 0.98)
        l10_n = np.full(n_per_day, 10)
        l10_hits = np.clip(np.round(hist * 11 - 0.5), 0, 10).astype(int)
        h = (l10_hits + 0.5) / (l10_n + 1)
        pk = 0.35 * h + 0.65 * mid
        half = rng.uniform(0.02, 0.08, n_per_day)
        yes, no = np.clip(mid + half, 0.01, 0.99), np.clip(1 - mid + half, 0.01, 0.99)
        yes_won = rng.random(n_per_day) < mid
        line = 10.5
        for i in range(n_per_day):
            side = 'YES' if pk[i] >= 0.5 else 'NO'
            sel_ok = [(s, p, pr) for s, p, pr in (('YES', pk[i], yes[i]), ('NO', 1 - pk[i], no[i]))
                      if p - pr >= 0.05 and pr <= 0.85 and p >= 0.68]
            sel = max(sel_ok, key=lambda t: t[1] - t[2]) if sel_ok else None
            rows.append({'date': day, 'game_id': i // 20, 'player_id': i, 'stat': ['pts', 'reb', 'ast', 'fg3m', 'pra'][i % 5],
                         'line': line, 'strike': 11, 'quote_kind': 'intraday', 'quote_age_min': 10,
                         'yes_ask': yes[i], 'no_ask': no[i], 'mid': mid[i], 'l10_hits': l10_hits[i], 'l10_n': 10,
                         'pk': pk[i], 'side': side, 'p_raw': max(pk[i], 1 - pk[i]),
                         'price': yes[i] if side == 'YES' else no[i],
                         'outcome': int(yes_won[i]) if side == 'YES' else int(not yes_won[i]),
                         'actual': 11.0 if yes_won[i] else 10.0, 'selected': int(sel is not None),
                         'sel_side': sel[0] if sel else '', 'sel_p': sel[1] if sel else '',
                         'sel_price': sel[2] if sel else '',
                         'sel_outcome': (int(yes_won[i]) if sel[0] == 'YES' else int(not yes_won[i])) if sel else ''})
    return pd.DataFrame(rows)


@pytest.fixture(scope='module')
def art():
    ledger = pd.DataFrame({'platform': ['sleeper'] * 50 + ['kalshi'] * 10, 'stat': ['pts'] * 60,
                           'p': [0.65] * 50 + [0.77] * 10, 'y': [1] * 30 + [0] * 20 + [1] * 7 + [0] * 3,
                           'mid': [None] * 60})
    return C.fit_all(synthetic_replay(), ledger)


def test_sides_doubles_rows_and_sets_side_outcomes():
    r = synthetic_replay(n_per_day=5).head(5)
    a = C.sides(r)
    assert len(a) == 10
    yes, no = a[a.side_c == 'YES'], a[a.side_c == 'NO']
    assert np.allclose(yes.p.values + no.p.values, 1) and ((yes.y.values + no.y.values) == 1).all()
    assert np.allclose(yes.mid_s.values + no.mid_s.values, 1)


def test_time_split_and_pooled_logit2(art):
    k = art['kalshi']
    assert k['time_split']['fit'][1] < C.TEST_FROM <= k['time_split']['test'][0]
    assert k['maps']['_pooled']['method'] == 'logit2'
    assert all(k['maps'][s]['method'] == 'pooled' for s in C.STATS)      # < MIN_FIT rows each
    assert k['maps']['_pooled']['b_mid'] == pytest.approx(1.0, abs=0.25)  # mid is the truth here


def test_calibration_fixes_the_selected_subset(art):
    sel = next(r for r in art['report']['kalshi_test']
               if r['subset'] == 'card selected (raw rules)' and r['stat'] == 'all')
    raw, cal = sel['raw']['overall'], sel['calibrated']['overall']
    assert raw['pred'] - raw['hit'] > 0.05                       # raw rule picks are overconfident
    assert abs(cal['pred'] - cal['hit']) < abs(raw['pred'] - raw['hit'])
    assert cal['brier'] <= raw['brier']
    assert sel['clears_card_rules_calibrated'] < sel['clears_card_rules_raw']
    subsets = {r['subset'] for r in art['report']['kalshi_test']}
    # 'card selected (calibrated rules)' appears only when some test leg passes the calibrated rules
    assert {'card region (raw p - ask > 0)', 'card selected (raw rules)'} <= subsets
    assert subsets <= {'card region (raw p - ask > 0)', 'card selected (raw rules)', 'card selected (calibrated rules)'}


def test_sleeper_offset_is_shrunk(art):
    s = art['sleeper']
    assert s['method'] == 'offset' and s['raw_offset'] == pytest.approx(0.6 - 0.65)
    assert s['offset'] == pytest.approx(-0.05 * 50 / (50 + C.SHRINK_K), abs=1e-5)


def test_ledger_without_mid_reports_raw_only(art):
    led = {r['venue']: r for r in art['report']['ledger']}
    assert led['kalshi']['calibrated'] is None and led['kalshi']['raw']['overall']['n'] == 10
    assert led['sleeper']['calibrated']['overall']['n'] == 50


def test_reliability_math():
    out = C.reliability([0.55, 0.55, 0.85, 0.95], [1, 0, 1, 1])
    assert out['overall']['n'] == 4 and out['overall']['hit'] == 0.75
    b = {(x['lo'], x['n']) for x in out['buckets']}
    assert b == {(0.55, 2), (0.85, 1), (0.9, 1)}
    first = next(x for x in out['buckets'] if x['lo'] == 0.55)
    assert first['brier'] == pytest.approx((0.45 ** 2 + 0.55 ** 2) / 2, abs=1e-4)
    assert first['logloss'] == pytest.approx(-(np.log(0.55) + np.log(0.45)) / 2, abs=1e-4)


def test_apply_map_methods():
    assert C.apply_map({'method': 'offset', 'offset': -0.05}, [0.7])[0] == pytest.approx(0.65)
    assert C.apply_map({'method': 'identity'}, [0.7])[0] == pytest.approx(0.7)
    with pytest.raises(ValueError):
        C.apply_map({'method': 'nope'}, [0.7])


def test_save_and_note_parsing(tmp_path, art):
    p = C.save(art, tmp_path / 'a.json')
    assert p.exists() and 'fit_date' in p.read_text()
    assert C._note_float('on the line, p_raw=0.8443, mid=0.7100', 'mid=') == 0.71
    assert C._p_raw_from_note('market disagrees') is None and C._p_raw_from_note(None) is None
