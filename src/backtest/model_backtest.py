"""Held-out backtest of all six ML prop models, at Kalshi-style thresholds (AUDIT_2026-11-24.md item 8; D18).

Run:  ./venv/bin/python -m src.backtest.model_backtest [--season 2026-27] [--out data/processed/backtest/model_backtest.json]

Models: data/processed/models/{pts,reb,ast,fg3m,pra,reb_ast}_model.pkl. They were refit on every season
through 2025-26 (recipe final_refit_all_seasons), so the old dashboard's "2024-25 test set" is IN-SAMPLE.
This scores the first season a model was NOT trained on (2026-27 today), from
data/processed/features_appearance_v2.parquet (the file the daily refresh keeps current).

Per stat:
  projection   (1 - w) * model + w * L10 average   (w = config/model.yaml props_model.l10_weight)
  spread       the player's L10 std (floor 0.5; missing -> the stat's std over the rows)
  thresholds   Kalshi 'N+' = P(X >= N) = 1 - Phi((N - 0.5 - proj) / sd) for N = round(proj) + k, k in -3..3, N >= 1
  L10 line     legacy reference: P(X > L10 avg) (the old fake-edge line; reported, never used for picks)
Reliability is on the favoured side (p = max(p, 1 - p)), buckets as src/models/calibration.reliability.
Rows: features are NEVER dropped for being null (HistGradientBoosting handles NaN natively). The only drops are
counted and reported: min_L10 < MIN_MINUTES (a rotation filter), missing actual (no box score), missing L10
(for the L10 line only).
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ROOT / 'data/processed/features_appearance_v2.parquet'
MODELS = ROOT / 'data/processed/models'
OUT = ROOT / 'data/processed/backtest/model_backtest.json'
STATS = ('pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast')
OFFSETS = range(-3, 4)
MIN_MINUTES = 20


def load_bundle(stat: str, folder: Path = MODELS) -> dict:
    return pickle.loads((folder / f'{stat}_model.pkl').read_bytes())


def held_out_season(bundle: dict, seasons) -> str | None:
    trained = set(bundle.get('train_seasons') or [])
    later = sorted(s for s in seasons if s not in trained and s > max(trained, default=''))
    return later[0] if later else None


def predict(df: pd.DataFrame, bundle: dict, stat: str, l10_weight: float) -> tuple[pd.DataFrame, dict]:
    """(rows with proj / sd / actual / l10, counts). Never drops a row for null features."""
    counts = {'rows_in': int(len(df))}
    if 'min_L10' in df.columns:
        keep = df['min_L10'] >= MIN_MINUTES
        counts['dropped_min_L10'] = int((~keep).sum())
        df = df[keep]
    has_actual = df[stat].notna()
    counts['dropped_no_actual'] = int((~has_actual).sum())
    df = df[has_actual]
    X = df.reindex(columns=bundle['feature_cols']).apply(pd.to_numeric, errors='coerce')
    counts['rows_with_null_features'] = int(X.isna().any(axis=1).sum())
    model_pred = bundle['model'].predict(X) if len(X) else np.array([])
    l10 = df.get(f'{stat}_L10')
    l10v = l10.values.astype(float) if l10 is not None else np.full(len(df), np.nan)
    proj = np.where(np.isnan(l10v), model_pred, (1 - l10_weight) * model_pred + l10_weight * l10v)
    sd_col = df.get(f'{stat}_std_L10')
    fallback = float(df[stat].std()) if len(df) > 1 else 1.0
    sd = np.maximum(np.nan_to_num(sd_col.values.astype(float) if sd_col is not None else np.full(len(df), np.nan),
                                  nan=fallback), 0.5)
    out = pd.DataFrame({'player_id': df.get('player_id', pd.Series(range(len(df)), index=df.index)).values,
                        'game_date': df['game_date'].astype(str).values, 'proj': proj, 'sd': sd,
                        'actual': df[stat].values.astype(float), 'l10': l10v})
    counts['rows_scored'] = int(len(out))
    return out, counts


def threshold_rows(pred: pd.DataFrame) -> pd.DataFrame:
    """One row per (player-game, N): Kalshi N+ = P(X >= N), favoured side p and outcome."""
    parts = []
    base = np.round(pred['proj'].values)
    for k in OFFSETS:
        n = base + k
        ok = n >= 1
        p_yes = 1 - norm.cdf(n - 0.5, loc=pred['proj'].values, scale=pred['sd'].values)
        y_yes = (pred['actual'].values >= n).astype(int)
        parts.append(pd.DataFrame({'n': n[ok], 'p_yes': p_yes[ok], 'y_yes': y_yes[ok],
                                   'game_date': pred['game_date'].values[ok]}))
    t = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=['n', 'p_yes', 'y_yes', 'game_date'])
    yes = t['p_yes'] >= 0.5
    t['p'] = np.where(yes, t['p_yes'], 1 - t['p_yes'])
    t['y'] = np.where(yes, t['y_yes'], 1 - t['y_yes'])
    return t


def l10_rows(pred: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    ok = ~np.isnan(pred['l10'].values)
    p = pred[ok]
    p_over = 1 - norm.cdf(p['l10'].values, loc=p['proj'].values, scale=p['sd'].values)
    y_over = (p['actual'].values > p['l10'].values).astype(int)
    over = p_over >= 0.5
    return pd.DataFrame({'p': np.where(over, p_over, 1 - p_over), 'y': np.where(over, y_over, 1 - y_over)}), int((~ok).sum())


def run(stats=STATS, season: str | None = None, features: Path = FEATURES, models: Path = MODELS) -> dict:
    from config.model_config import WEIGHTS
    from src.models.calibration import reliability
    w = WEIGHTS['props_model']['l10_weight']
    df = pd.read_parquet(features)
    report = {'features': str(features.relative_to(ROOT) if features.is_relative_to(ROOT) else features),
              'l10_weight': w, 'stats': {}}
    for stat in stats:
        bundle = load_bundle(stat, models)
        s = season or held_out_season(bundle, df['season'].unique())
        entry = {'season': s, 'train_seasons': bundle.get('train_seasons'),
                 'in_sample': s in set(bundle.get('train_seasons') or [])}
        if s is None:
            entry['error'] = 'no season after the training seasons in the features file'
            report['stats'][stat] = entry
            continue
        pred, counts = predict(df[df['season'] == s], bundle, stat, w)
        thr = threshold_rows(pred)
        l10, no_l10 = l10_rows(pred)
        counts['l10_line_dropped_no_L10'] = no_l10
        entry.update(counts=counts, dates=[pred['game_date'].min(), pred['game_date'].max()] if len(pred) else None,
                     mae=round(float(np.mean(np.abs(pred['proj'] - pred['actual']))), 4) if len(pred) else None,
                     thresholds=reliability(thr['p'], thr['y']), l10_line=reliability(l10['p'], l10['y']))
        report['stats'][stat] = entry
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--season', help='default: the first season each model was not trained on')
    ap.add_argument('--out', type=Path, default=OUT)
    args = ap.parse_args(argv)
    rep = run(season=args.season)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rep, indent=1, default=float))
    for stat, e in rep['stats'].items():
        if 'error' in e:
            print(f'{stat:8s} {e["error"]}')
            continue
        t, c = e['thresholds']['overall'], e['counts']
        print(f"{stat:8s} {e['season']} {'IN-SAMPLE' if e['in_sample'] else 'held-out'} rows {c['rows_in']}->"
              f"{c['rows_scored']} (min_L10 {c['dropped_min_L10']}, no actual {c['dropped_no_actual']}; "
              f"{c['rows_with_null_features']} with null features kept) MAE {e['mae']} | N+ n={t['n']} "
              f"p {t['pred']} hit {t['hit']} Brier {t['brier']}")
    print(f'saved {args.out}')
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
