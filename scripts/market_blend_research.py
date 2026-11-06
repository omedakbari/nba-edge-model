"""
Which probability is most accurate for a player prop going over its line?

Candidates, all scored out-of-sample on the held-out season:
  market   — sportsbook consensus no-vig P(over) at the opening line
  model    — production props model (Gaussian P(over) from blended prediction)
  linear   — logit(P) = a + b·logit(market) + c·(logit(model) − logit(market)), per stat
  gbm      — shallow gradient-boosted classifier on market + model + context

Base-model predictions are WALK-FORWARD: each season is predicted by models
trained only on earlier seasons (same features/hyperparameters as
src/models/player_props.py). The production models were trained through
2023-24, so using them to build blend training data would leak.

Usage:
    python scripts/market_blend_research.py                 # train 2023-24+2024-25, test 2025-26
    python scripts/market_blend_research.py --rebuild-oof   # recompute walk-forward predictions
"""

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error

from config.settings import PROCESSED_DIR, BLEND_L10_WEIGHT, DB_PATH
from src.models.player_props import get_feature_columns, select_features
from backtest_props_vs_market import load_market, _norm_name, _logit, _logit_fit

BASE_STATS = ['pts', 'reb', 'ast', 'fg3m']
COMPOSITES = {'pra': ['pts', 'reb', 'ast'], 'reb_ast': ['reb', 'ast'],
              'pts_reb': ['pts', 'reb'], 'pts_ast': ['pts', 'ast']}
ALL_SEASONS = ['2019-20', '2020-21', '2021-22', '2022-23', '2023-24', '2024-25', '2025-26']
EVAL_SEASONS = ['2023-24', '2024-25', '2025-26']
OOF_PATH = PROCESSED_DIR / 'walkforward_preds.parquet'
DATASET_PATH = PROCESSED_DIR / 'market_blend_dataset.parquet'


# ── 1. Walk-forward base predictions ──────────────────────────────────────────

def _fit(train, val, target, feats_all):
    feats = select_features(train, target, feats_all, max_features=40, corr_threshold=0.92)
    tr, va = train[train[target].notna()], val[val[target].notna()]
    m = HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, min_samples_leaf=30,
                                      l2_regularization=5.0, max_features=0.6,
                                      early_stopping=False, random_state=42)
    best, best_iter = np.inf, 50
    for n in [50, 100, 150, 200, 250, 300]:
        m.set_params(max_iter=n).fit(tr[feats], tr[target])
        mae = mean_absolute_error(va[target], m.predict(va[feats]))
        if mae < best:
            best, best_iter = mae, n
    full = pd.concat([tr, va])
    m.set_params(max_iter=best_iter).fit(full[feats], full[target])
    return m, feats


def build_oof(df: pd.DataFrame) -> pd.DataFrame:
    feats_all = get_feature_columns(df)
    out = []
    for season in EVAL_SEASONS:
        i = ALL_SEASONS.index(season)
        train = df[df['season'].isin(ALL_SEASONS[:i - 1])]
        val = df[df['season'] == ALL_SEASONS[i - 1]]
        test = df[df['season'] == season].copy()
        for s in BASE_STATS:
            m, feats = _fit(train, val, s, feats_all)
            test[f'{s}_raw'] = m.predict(test[feats])
            print(f"  {season} {s:<5} test MAE {mean_absolute_error(test[s], test[f'{s}_raw']):.3f}"
                  f"  (L10 {mean_absolute_error(test[s], test[f'{s}_L10'].fillna(test[s].mean())):.3f})",
                  flush=True)
        out.append(test[['player_id', 'game_date', 'season'] + [f'{s}_raw' for s in BASE_STATS]])
    oof = pd.concat(out)
    oof.to_parquet(OOF_PATH)
    return oof


# ── 2. Join with market ───────────────────────────────────────────────────────

def build_dataset(df: pd.DataFrame, oof: pd.DataFrame) -> pd.DataFrame:
    import sqlite3
    df = df.merge(oof, on=['player_id', 'game_date', 'season'])
    for s in BASE_STATS:
        df[f'{s}_rawpred'] = df[f'{s}_raw']
    for c, parts in COMPOSITES.items():
        df[f'{c}_rawpred'] = sum(df[f'{p}_raw'] for p in parts)
        if c not in df.columns:
            df[c] = sum(df[p] for p in parts)
    df['name_key'] = df['full_name'].map(_norm_name)
    df['game_date'] = pd.to_datetime(df['game_date'])

    conn = sqlite3.connect(str(DB_PATH))
    mkt = load_market(conn, (df['game_date'].min().strftime('%Y-%m-%d'),
                             df['game_date'].max().strftime('%Y-%m-%d')))
    conn.close()

    parts = []
    for stat in BASE_STATS + list(COMPOSITES):
        m = mkt[mkt['stat_key'] == stat]
        if m.empty or f'{stat}_L10' not in df.columns:
            continue
        cols = ['name_key', 'game_date', 'season', 'player_id', 'is_home', 'days_rest', 'is_b2b',
                'min_L10', 'min_trend', f'{stat}_L10', f'{stat}_rawpred', stat]
        std_col = f'{stat}_std_L10'
        if std_col in df.columns:
            cols.append(std_col)
        j = m.merge(df[[c for c in cols if c in df.columns]], on=['name_key', 'game_date'])
        j = j.rename(columns={f'{stat}_L10': 'l10', f'{stat}_rawpred': 'raw', stat: 'actual',
                              std_col: 'std'})
        if 'std' not in j.columns:
            j['std'] = np.nan
        parts.append(j)
    d = pd.concat(parts, ignore_index=True)

    d['pred'] = (1 - BLEND_L10_WEIGHT) * d['raw'] + BLEND_L10_WEIGHT * d['l10']
    std = d['std'].where(d['std'] > 0, np.maximum(d['pred'].abs() * 0.2, 1.0)).fillna(
        np.maximum(d['pred'].abs() * 0.2, 1.0))
    d['std_eff'] = std
    d['model_p_over'] = np.clip(1 - scipy_stats.norm.cdf((d['line'] - d['pred']) / std), 0.01, 0.99)
    d = d[d['actual'] != d['line']].copy()
    d['over_hit'] = (d['actual'] > d['line']).astype(int)

    # Blend features
    d['mkt_logit'] = _logit(d['mkt_p_over'])
    d['dis_logit'] = _logit(d['model_p_over']) - d['mkt_logit']
    d['z_pred'] = (d['pred'] - d['line']) / std
    d['z_raw'] = (d['raw'] - d['line']) / std
    d['z_l10'] = (d['l10'] - d['line']) / std
    d['cv'] = std / d['line'].clip(lower=0.5)
    d['stat_code'] = d['stat_key'].astype('category').cat.codes
    ab = absence_features()
    d = d.merge(ab, on=['player_id', 'game_date'], how='left')
    d.to_parquet(DATASET_PATH)
    return d


# ── 2b. Teammate / opponent absence (who didn't play) ─────────────────────────
# Reconstructed from box scores: a "rotation" player is one who averaged >= 15
# min across his appearances in the team's previous 5 games (>= 2 appearances)
# and hasn't since appeared for another team. Missing = rotation player with
# no box-score row for this game. Only reliable from 2023-24 on (earlier
# game_logs are missing player rows). Production equivalent: ESPN injury
# report "Out" list — actual absences include late scratches, a mild lookahead.

def absence_features() -> pd.DataFrame:
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    g = pd.read_sql("""SELECT player_id, game_id, game_date, team_abbr, opp_abbr, min, pts, reb, ast, fga
                       FROM game_logs WHERE season IN ('2023-24','2024-25','2025-26')
                       AND season_type IN ('Regular Season','Playoffs','PlayIn')""", conn)
    conn.close()
    g['game_date'] = pd.to_datetime(g['game_date'])
    games = (g[['game_id', 'game_date', 'team_abbr', 'opp_abbr']].drop_duplicates(['game_id', 'team_abbr'])
             .sort_values('game_date'))
    last_team = g.sort_values('game_date').groupby('player_id')[['game_date', 'team_abbr']]
    by_game = {k: v for k, v in g.groupby(['game_id', 'team_abbr'])}
    # player's appearance dates per team, for "since appeared elsewhere" check
    appear = g.sort_values('game_date').groupby('player_id')

    rows = []
    for team, tg in games.groupby('team_abbr'):
        tg = tg.reset_index(drop=True)
        hist: list[pd.DataFrame] = []
        for i, r in tg.iterrows():
            played = by_game.get((r['game_id'], team))
            if played is None:
                continue
            prev = pd.concat(hist[-5:]) if hist else None
            feat = {'game_id': r['game_id'], 'team_abbr': team, 'miss_min': 0.0, 'miss_pts': 0.0,
                    'miss_reb': 0.0, 'miss_ast': 0.0, 'miss_fga': 0.0, 'n_missing': 0}
            if prev is not None:
                agg = prev.groupby('player_id').agg(n=('game_id', 'count'), min=('min', 'mean'),
                                                    pts=('pts', 'mean'), reb=('reb', 'mean'),
                                                    ast=('ast', 'mean'), fga=('fga', 'mean'))
                rot = agg[(agg['n'] >= 2) & (agg['min'] >= 15)]
                missing = rot[~rot.index.isin(played['player_id'])]
                for pid in list(missing.index):   # traded away? appeared for another team since
                    a = appear.get_group(pid)
                    if ((a['game_date'] < r['game_date']) & (a['team_abbr'] != team)
                            & (a['game_date'] > prev['game_date'].min())).any():
                        missing = missing.drop(pid)
                feat.update(miss_min=missing['min'].sum(), miss_pts=missing['pts'].sum(),
                            miss_reb=missing['reb'].sum(), miss_ast=missing['ast'].sum(),
                            miss_fga=missing['fga'].sum(), n_missing=len(missing))
                # first game back: played now, missed the team's previous game, was rotation
                prev_game = hist[-1]
                back = rot.index[rot.index.isin(played['player_id']) & ~rot.index.isin(prev_game['player_id'])]
                feat['returning_ids'] = set(back)
            rows.append(feat)
            hist.append(played)
    a = pd.DataFrame(rows)
    a['returning_ids'] = a['returning_ids'].apply(lambda x: x if isinstance(x, set) else set())
    opp = a[['game_id', 'team_abbr', 'miss_min', 'miss_pts', 'n_missing']].rename(columns={
        'team_abbr': 'opp_abbr', 'miss_min': 'opp_miss_min', 'miss_pts': 'opp_miss_pts',
        'n_missing': 'opp_n_missing'})
    keys = g[['player_id', 'game_id', 'game_date', 'team_abbr', 'opp_abbr']]
    out = keys.merge(a, on=['game_id', 'team_abbr'], how='left').merge(opp, on=['game_id', 'opp_abbr'], how='left')
    out['is_returning'] = [int(pid in s) if isinstance(s, set) else 0
                           for pid, s in zip(out['player_id'], out['returning_ids'])]
    return out.drop(columns=['returning_ids', 'game_id', 'team_abbr', 'opp_abbr'])


ABSENCE_COLS = ['miss_min', 'miss_pts', 'miss_reb', 'miss_ast', 'miss_fga', 'n_missing',
                'opp_miss_min', 'opp_miss_pts', 'opp_n_missing', 'is_returning']


# ── 3. Compare candidates ─────────────────────────────────────────────────────

GBM_BASE = ['mkt_logit', 'dis_logit', 'z_pred', 'z_raw', 'z_l10', 'cv', 'line',
            'min_L10', 'min_trend', 'n_books', 'is_home', 'days_rest', 'is_b2b', 'stat_code']
GBM_FEATURES = GBM_BASE + ABSENCE_COLS


def _ll(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def evaluate(d: pd.DataFrame, test_season: str) -> pd.DataFrame:
    train, test = d[d['season'] != test_season].copy(), d[d['season'] == test_season].copy()
    print(f"\n  blend train n={len(train):,} ({sorted(train.season.unique())})   "
          f"test n={len(test):,} ({test_season})")

    # linear, per stat
    test['linear'] = np.nan
    for stat, g in train.groupby('stat_key'):
        X = np.c_[np.ones(len(g)), g['mkt_logit'], g['dis_logit']]
        b, t = _logit_fit(X, g['over_hit'].values.astype(float))
        m = test['stat_key'] == stat
        Xt = np.c_[np.ones(m.sum()), test.loc[m, 'mkt_logit'], test.loc[m, 'dis_logit']]
        test.loc[m, 'linear'] = 1 / (1 + np.exp(-Xt @ b))
        print(f"    linear {stat:<8} mkt {b[1]:+.2f}  model-nudge {b[2]:+.3f} (t={t[2]:+.1f})")
    test['linear'] = test['linear'].fillna(test['mkt_p_over'])

    # gbm
    for name, feats in [('gbm', GBM_BASE), ('gbm_inj', GBM_FEATURES)]:
        feats = [f for f in feats if f in train.columns]
        gbm = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.03, max_iter=400,
                                             min_samples_leaf=200, l2_regularization=10.0,
                                             categorical_features=[feats.index('stat_code')],
                                             random_state=42)
        gbm.fit(train[feats], train['over_hit'])
        test[name] = gbm.predict_proba(test[feats])[:, 1]

    y = test['over_hit'].values
    print(f"\n  {'Candidate':<10} {'LogLoss':>8} {'Brier':>8}   (held-out {test_season})")
    res = {}
    for c in ['mkt_p_over', 'model_p_over', 'linear', 'gbm', 'gbm_inj']:
        res[c] = (_ll(test[c].values, y), float(np.mean((test[c].values - y) ** 2)))
        print(f"  {c:<10} {res[c][0]:>8.4f} {res[c][1]:>8.4f}")

    print(f"\n  Per stat — Brier (market / linear / gbm / gbm_inj):")
    for stat, g in test.groupby('stat_key'):
        yy = g['over_hit'].values
        b = [np.mean((g[c].values - yy) ** 2) for c in ['mkt_p_over', 'linear', 'gbm', 'gbm_inj']]
        print(f"    {stat:<8} n={len(g):>6,}  {b[0]:.4f} / {b[1]:.4f} / {b[2]:.4f} / {b[3]:.4f}")

    print(f"\n  Selection accuracy: legs where candidate P(favored side) ≥ t")
    print(f"  {'t':<6}" + ''.join(f"{c:>22}" for c in ['mkt_p_over', 'linear', 'gbm', 'gbm_inj']))
    for t in [0.54, 0.56, 0.58, 0.60, 0.62, 0.65]:
        row = f"  {t:<6.2f}"
        for c in ['mkt_p_over', 'linear', 'gbm', 'gbm_inj']:
            p = test[c].values
            side = p >= 0.5
            pf = np.where(side, p, 1 - p)
            hit = np.where(side, y, 1 - y)
            m = pf >= t
            row += f"   n={m.sum():>6,} hit {hit[m].mean() if m.sum() else np.nan:>6.1%}"
        print(row)
    return test


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild-oof', action='store_true')
    ap.add_argument('--rebuild-dataset', action='store_true')
    ap.add_argument('--test-season', default='2025-26')
    args = ap.parse_args()

    if args.rebuild_oof or args.rebuild_dataset or not DATASET_PATH.exists():
        df = pd.read_parquet(PROCESSED_DIR / 'features.parquet')
        df['game_date'] = pd.to_datetime(df['game_date'])
        if args.rebuild_oof or not OOF_PATH.exists():
            oof = build_oof(df)
        else:
            oof = pd.read_parquet(OOF_PATH)
        oof['game_date'] = pd.to_datetime(oof['game_date'])
        d = build_dataset(df, oof)
    else:
        d = pd.read_parquet(DATASET_PATH)
    print(f"  dataset: {len(d):,} props  " + str(d.groupby('season').size().to_dict()))
    evaluate(d, args.test_season)


if __name__ == '__main__':
    main()
