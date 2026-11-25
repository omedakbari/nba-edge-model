"""src/backtest/model_backtest.py: held-out season, N+ thresholds, and NO silent row drops on null features
(the July anti-calibration fix: rows with null features must be scored, not filtered)."""
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import HistGradientBoostingRegressor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest import model_backtest as B  # noqa: E402

FEATS = ['f1', 'f2', 'pts_series_L3']


def _frame(n=400, seed=0, season='2026-27'):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({'season': season, 'game_date': pd.date_range('2026-10-21', periods=n, freq='h').astype(str),
                       'player_id': np.arange(n), 'f1': rng.normal(20, 5, n), 'f2': rng.normal(0, 1, n),
                       'pts_series_L3': np.nan, 'min_L10': rng.uniform(10, 36, n)})
    df['pts'] = np.round(np.clip(df['f1'] + rng.normal(0, 4, n), 0, None))
    df['pts_L10'] = df['f1'].round(1)
    df['pts_std_L10'] = 4.0
    df.loc[df.index[:30], 'f2'] = np.nan                     # some rows with a null feature
    df.loc[df.index[30:35], 'pts'] = np.nan                  # no box score
    df.loc[df.index[35:40], 'pts_L10'] = np.nan              # no L10 (affects the L10 line only)
    return df


@pytest.fixture
def setup(tmp_path):
    train = _frame(800, seed=1, season='2025-26').dropna(subset=['pts'])
    model = HistGradientBoostingRegressor(max_iter=30).fit(train[FEATS], train['pts'])
    models = tmp_path / 'models'
    models.mkdir()
    (models / 'pts_model.pkl').write_bytes(pickle.dumps({'model': model, 'feature_cols': FEATS,
                                                         'train_seasons': ['2024-25', '2025-26']}))
    feats = tmp_path / 'f.parquet'
    pd.concat([train, _frame()]).to_parquet(feats)
    return feats, models


def test_held_out_season_is_after_training():
    assert B.held_out_season({'train_seasons': ['2024-25', '2025-26']}, ['2024-25', '2025-26', '2026-27']) == '2026-27'
    assert B.held_out_season({'train_seasons': ['2024-25', '2025-26']}, ['2024-25', '2025-26']) is None


def test_rows_in_equals_rows_out_plus_counted_drops_with_null_features(setup):
    feats, models = setup
    rep = B.run(stats=('pts',), features=feats, models=models)['stats']['pts']
    c = rep['counts']
    assert rep['season'] == '2026-27' and rep['in_sample'] is False
    assert c['rows_in'] == 400
    assert c['rows_in'] == c['rows_scored'] + c['dropped_min_L10'] + c['dropped_no_actual']   # nothing else dropped
    assert c['rows_with_null_features'] == c['rows_scored']        # every row has the all-null series feature
    df = _frame()
    kept = df[df['min_L10'] >= B.MIN_MINUTES]
    assert c['rows_scored'] == int(kept['pts'].notna().sum())       # null f2 rows were scored, not filtered


def test_thresholds_are_kalshi_n_plus(setup):
    pred = pd.DataFrame({'proj': [10.2], 'sd': [3.0], 'actual': [12.0], 'game_date': ['d'], 'l10': [9.5],
                         'player_id': [1]})
    t = B.threshold_rows(pred)
    assert sorted(t['n']) == [7, 8, 9, 10, 11, 12, 13]
    row = t[t['n'] == 12].iloc[0]
    from scipy.stats import norm
    p_yes = 1 - norm.cdf(11.5, loc=10.2, scale=3.0)
    assert row['p_yes'] == pytest.approx(p_yes) and row['y_yes'] == 1          # 12 >= 12
    assert row['p'] == pytest.approx(max(p_yes, 1 - p_yes)) and row['y'] == 0   # favoured side is NO
    assert (B.threshold_rows(pred.assign(proj=0.4))['n'] >= 1).all()            # no 0+ or negative strikes


def test_l10_line_drops_only_missing_l10(setup):
    feats, models = setup
    rep = B.run(stats=('pts',), features=feats, models=models)['stats']['pts']
    assert rep['l10_line']['overall']['n'] == rep['counts']['rows_scored'] - rep['counts']['l10_line_dropped_no_L10']
    assert rep['thresholds']['overall']['n'] > rep['counts']['rows_scored']     # several strikes per row


def test_in_sample_season_is_labelled(setup):
    feats, models = setup
    rep = B.run(stats=('pts',), season='2025-26', features=feats, models=models)['stats']['pts']
    assert rep['in_sample'] is True
