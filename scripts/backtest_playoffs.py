"""
Playoff backtest: validates model calibration on postseason games.

Applies the full playoff pipeline — model prediction → L10 blend → playoff
adjustment layer — the same path used by generate_todays_picks.py --playoffs.

Uses 2024-25 playoffs as the held-out evaluation set (2021-22 through 2023-24
playoff games were in the training data).

Usage:
    python scripts/backtest_playoffs.py
    python scripts/backtest_playoffs.py --season 2023-24
    python scripts/backtest_playoffs.py --season 2024-25 --min-prob 0.57
"""

import sys
import argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
import pickle
from scipy import stats as scipy_stats

from config.settings import DB_PATH, PROCESSED_DIR
from src.playoffs.adjustments import (
    apply_playoff_adjustments,
    load_playoff_elevation_ratios,
    should_suppress,
    get_player_tier,
)

COMPOSITE_STATS = {
    'pra':     ['pts', 'reb', 'ast'],
    'reb_ast': ['reb', 'ast'],
}
BLEND_WEIGHT = 0.65


def load_model(stat: str) -> dict:
    model_path = PROCESSED_DIR / "models" / f"{stat}_model.pkl"
    with open(model_path, 'rb') as f:
        return pickle.load(f)


def prob_over(pred: float, line: float, std: float) -> float:
    if std <= 0 or np.isnan(std):
        std = max(abs(pred) * 0.2, 1.0)
    z = (line - pred) / std
    return float(np.clip(1 - scipy_stats.norm.cdf(z), 0.01, 0.99))


def predict_stat(df: pd.DataFrame, stat: str) -> pd.Series:
    """Return blended model predictions for a stat column."""
    components = COMPOSITE_STATS.get(stat)
    if components:
        preds = pd.Series(0.0, index=df.index)
        for c in components:
            m = load_model(c)
            valid = df[m['feature_cols']].notna().all(axis=1)
            raw = pd.Series(np.nan, index=df.index)
            raw[valid] = m['model'].predict(df.loc[valid, m['feature_cols']])
            preds += raw
    else:
        m = load_model(stat)
        valid = df[m['feature_cols']].notna().all(axis=1)
        preds = pd.Series(np.nan, index=df.index)
        preds[valid] = m['model'].predict(df.loc[valid, m['feature_cols']])

    l10_col = f'{stat}_L10'
    if l10_col in df.columns:
        blended = (1 - BLEND_WEIGHT) * preds + BLEND_WEIGHT * df[l10_col]
        return blended
    return preds


def run_playoff_backtest(eval_season: str = '2024-25', min_prob: float = 0.55) -> None:
    feature_path = PROCESSED_DIR / "features.parquet"
    if not feature_path.exists():
        print("❌ features.parquet not found. Run: python -m src.features.pipeline")
        sys.exit(1)

    df = pd.read_parquet(feature_path)

    df_playoffs = df[
        (df['season'] == eval_season) &
        (df['season_type'] == 'Playoffs')
    ].copy()

    if df_playoffs.empty:
        available = sorted(df[df['season_type'] == 'Playoffs']['season'].unique())
        print(f"❌ No playoff data for {eval_season}. Available: {available}")
        sys.exit(1)

    # Apply same minute filter as daily picks
    min_col = 'min_L10' if 'min_L10' in df_playoffs.columns else 'min'
    df_playoffs = df_playoffs[df_playoffs[min_col] >= 20].copy()

    print(f"\n  Loading playoff elevation ratios...")
    playoff_ratios = load_playoff_elevation_ratios(DB_PATH)
    if playoff_ratios:
        print(f"  Found historical ratios for {len(playoff_ratios)} players")
    else:
        print(f"  No historical ratios — using tier multipliers only")

    print(f"\n{'=' * 72}")
    print(f"  PLAYOFF BACKTEST — {eval_season} (n={len(df_playoffs):,} player-games)")
    print(f"  Full pipeline: model → L10 blend → playoff adjustment layer")
    print(f"{'=' * 72}")

    if 'series_game_num' in df_playoffs.columns:
        dist = df_playoffs['series_game_num'].value_counts().sort_index()
        print(f"\n  Series game distribution:")
        for g, n in dist.items():
            print(f"    Game {g}: {n:>4} player-games")

    summary = []

    for stat in ['pts', 'reb', 'ast', 'pra', 'reb_ast']:
        try:
            df_s = df_playoffs.dropna(subset=[stat, f'{stat}_L10']).copy()
            df_s['pred'] = predict_stat(df_s, stat)
            df_s = df_s.dropna(subset=['pred']).copy()

            std_col = f'{stat}_std_L10'

            # Apply playoff adjustment layer per row
            adj_preds, adj_stds, suppressed_flags = [], [], []
            for _, row in df_s.iterrows():
                std_raw = float(row[std_col]) if std_col in df_s.columns and not np.isnan(row.get(std_col, np.nan)) else float(row['pred']) * 0.2
                adj_pred, adj_std, tier, _, _ = apply_playoff_adjustments(
                    float(row['pred']), std_raw, stat, row, playoff_ratios
                )
                suppressed = should_suppress(row, tier)
                adj_preds.append(adj_pred)
                adj_stds.append(adj_std)
                suppressed_flags.append(suppressed)

            df_s['adj_pred'] = adj_preds
            df_s['adj_std'] = adj_stds
            df_s['suppressed'] = suppressed_flags
            df_s['line'] = df_s[f'{stat}_L10']

            df_active = df_s[~df_s['suppressed']].copy()
            df_active['prob_over'] = [
                prob_over(p, l, s)
                for p, l, s in zip(df_active['adj_pred'], df_active['line'], df_active['adj_std'])
            ]
            df_active['hit'] = (df_active[stat] >= df_active['line']).astype(int)

        except Exception as e:
            print(f"\n  {stat.upper()}: Error — {e}")
            continue

        n_suppressed = df_s['suppressed'].sum()
        print(f"\n  --- {stat.upper()} ({len(df_active):,} active, {n_suppressed} suppressed) ---")

        for threshold in [0.55, 0.57, 0.60, 0.63, 0.65, 0.70]:
            picks = df_active[df_active['prob_over'] >= threshold]
            if len(picks) >= 15:
                hr = picks['hit'].mean()
                marker = '✅' if hr >= threshold - 0.05 else '⚠️'
                print(f"  P(over) >= {threshold:.0%}: {len(picks):>4} picks,  hit rate = {hr:.1%} {marker}")
                summary.append({'stat': stat, 'threshold': threshold,
                                 'picks': len(picks), 'hit_rate': hr,
                                 'ok': hr >= threshold - 0.05})

        # Series progression (if available)
        if 'series_game_num' in df_active.columns:
            early = df_active[df_active['series_game_num'] <= 2]
            late  = df_active[df_active['series_game_num'] >= 5]
            if len(early) >= 20 and len(late) >= 10:
                p60_early = early[early['prob_over'] >= 0.60]
                p60_late  = late[late['prob_over'] >= 0.60]
                if len(p60_early) >= 5 and len(p60_late) >= 5:
                    print(f"  Series split (P≥60%): G1-2 = {p60_early['hit'].mean():.1%}  "
                          f"G5-7 = {p60_late['hit'].mean():.1%}")

    print(f"\n{'=' * 72}")
    if summary:
        df_sum = pd.DataFrame(summary)
        pct_ok = df_sum['ok'].mean()
        print(f"  Calibration summary: {pct_ok:.0%} of threshold/stat combos well-calibrated")
        print(f"  (regular season baseline for comparison: ~85%)")
    print(f"  Note: lines are L10 reg-season averages, not real PP market lines.")
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--season', default='2024-25')
    parser.add_argument('--min-prob', type=float, default=0.55)
    args = parser.parse_args()
    run_playoff_backtest(eval_season=args.season, min_prob=args.min_prob)
