"""
Edge finder: compare model predictions to sportsbook lines.
Includes backtesting framework.

Usage:
    python -m src.models.edge_finder
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import numpy as np
import pickle
from scipy import stats as scipy_stats

from config.settings import PROCESSED_DIR, BLEND_L10_WEIGHT


# Composite stats: sum predictions from component models instead of using a
# single composite model, which avoids compounding rolling-average noise.
COMPOSITE_STATS = {
    'pra':     ['pts', 'reb', 'ast'],
    'reb_ast': ['reb', 'ast'],
}


def load_model(stat: str) -> dict:
    model_path = PROCESSED_DIR / "models" / f"{stat}_model.pkl"
    with open(model_path, 'rb') as f:
        return pickle.load(f)


def predicted_prob_over(predicted_value: float, line: float, std: float) -> float:
    if std <= 0 or np.isnan(std):
        std = max(abs(predicted_value) * 0.2, 1.0)
    z_score = (line - predicted_value) / std
    prob_over = 1 - scipy_stats.norm.cdf(z_score)
    return np.clip(prob_over, 0.01, 0.99)


def backtest_props(stat: str, line_offset: float = 0.0) -> pd.DataFrame:
    feature_path = PROCESSED_DIR / "features.parquet"
    df = pd.read_parquet(feature_path)
    backtest_season = '2025-26' if '2025-26' in df['season'].unique() else '2024-25'
    df_test = df[df['season'] == backtest_season].copy()

    # Drop garbage-time noise — bench/low-min players break FG3M/PTS calibration
    min_col = 'min_L10' if 'min_L10' in df_test.columns else 'min'
    df_test = df_test[df_test[min_col] >= 20].copy()

    components = COMPOSITE_STATS.get(stat)
    if components:
        # Load each component model, sum predictions. HistGradientBoostingRegressor
        # has native NaN support and was trained without any feature-NaN filtering
        # (see player_props.py: only rows with NaN target are dropped, never rows
        # with NaN features) — do not drop or impute rows here either, or this
        # backtest silently evaluates a different, biased population than the one
        # the model was actually trained and is actually used on.
        component_models = {c: load_model(c) for c in components}

        df_test[f'{stat}_pred_raw'] = 0.0
        for m_data in component_models.values():
            preds = m_data['model'].predict(df_test[m_data['feature_cols']])
            df_test[f'{stat}_pred_raw'] += preds
    else:
        model_data = load_model(stat)
        model = model_data['model']
        feature_cols = model_data['feature_cols']

        df_test[f'{stat}_pred_raw'] = model.predict(df_test[feature_cols])

    l10_col = f'{stat}_L10'
    df_test['line'] = df_test[l10_col] + line_offset

    # Unified L10/model blend (config.BLEND_L10_WEIGHT) — see config/settings.py
    # for the empirical derivation. Previously this backtest used 0% L10 (pure
    # raw model), a third, undocumented ratio alongside the 50% and 65% used
    # elsewhere in the codebase.
    df_test[f'{stat}_pred'] = (
        (1 - BLEND_L10_WEIGHT) * df_test[f'{stat}_pred_raw'] + BLEND_L10_WEIGHT * df_test[l10_col]
    )

    std_col = f'{stat}_std_L10'
    df_test['est_std'] = df_test[std_col].fillna(df_test[f'{stat}_pred'] * 0.2) if std_col in df_test.columns else df_test[f'{stat}_pred'] * 0.2

    df_test['prob_over'] = df_test.apply(
        lambda row: predicted_prob_over(row[f'{stat}_pred'], row['line'], row['est_std']),
        axis=1,
    )

    df_test['actual'] = df_test[stat]
    df_test['hit_over'] = (df_test['actual'] >= df_test['line']).astype(int)

    return df_test[['player_id', 'full_name', 'game_date', 'line',
                     f'{stat}_pred', 'prob_over', 'actual', 'hit_over']].copy()


def run_backtest_report():
    print("\n" + "=" * 70)
    import pandas as _pd
    _df = _pd.read_parquet(PROCESSED_DIR / "features.parquet")
    _season = '2025-26' if '2025-26' in _df['season'].unique() else '2024-25'
    print(f"  BACKTEST REPORT — {_season} Season")
    print("=" * 70)

    for stat in ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']:
        try:
            bt = backtest_props(stat)
        except Exception as e:
            print(f"\n  {stat.upper()}: Error — {e}")
            continue

        if bt.empty:
            continue

        total = len(bt)
        print(f"\n  --- {stat.upper()} ---")
        print(f"  Total predictions: {total:,}")

        for threshold in [0.55, 0.60, 0.65, 0.70, 0.75]:
            picks = bt[bt['prob_over'] >= threshold]
            if len(picks) > 10:
                hit_rate = picks['hit_over'].mean()
                print(f"  P(over) >= {threshold:.0%}: "
                      f"{len(picks):>5} picks, "
                      f"hit rate = {hit_rate:.1%} "
                      f"{'✅' if hit_rate >= threshold - 0.05 else '⚠️'}")

        elite = bt[bt['prob_over'] >= 0.70]
        if len(elite) > 0:
            print(f"\n  High-confidence sample (P >= 70%):")
            sample = elite.sample(min(5, len(elite)), random_state=42)
            for _, row in sample.iterrows():
                result = '✅' if row['hit_over'] else '❌'
                print(f"    {row['full_name']:25s} | Line: {row['line']:.1f} | "
                      f"Pred: {row[f'{stat}_pred']:.1f} | "
                      f"Actual: {row['actual']:.0f} | {result}")

    print("\n" + "=" * 70)
    print("  KEY INTERPRETATION GUIDE:")
    print("  • If P >= 70% picks hit at ~65-75%, your model is well-calibrated")
    print("  • If P >= 70% picks hit at 55-60%, model is overconfident — needs calibration")
    print("  • If P >= 70% picks hit at 80%+, model is underconfident — you're leaving edge on the table")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    run_backtest_report()
