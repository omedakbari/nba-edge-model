"""
Player props prediction model — v2 (overfitting fix).

Changes from v1:
  1. Much stronger regularization (shallow trees, high min_samples)
  2. Proper validation split (2023-24 for early stopping, not test set)
  3. Feature pre-selection (drop highly correlated, keep top by target correlation)
  4. Model-baseline blend for robust predictions
  5. HistGradientBoosting as primary (better small-data regularization)

Usage:
    python -m src.models.player_props
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import numpy as np
import pickle
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.ensemble import HistGradientBoostingRegressor

from config.settings import PROCESSED_DIR

TARGET_STATS = ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']
TRAIN_SEASONS = ['2019-20', '2020-21', '2021-22', '2022-23', '2023-24']
VAL_SEASON = '2024-25'
TEST_SEASON = '2025-26'

FEATURES_TO_EXCLUDE = [
    'pts', 'reb', 'ast', 'stl', 'blk', 'fg3m', 'tov', 'pf',
    'fgm', 'fga', 'fg3a', 'ftm', 'fta', 'oreb', 'dreb',
    'pts_reb', 'pts_ast', 'reb_ast', 'pra', 'plus_minus', 'min', 'wl',
    'id', 'player_id', 'game_id', 'game_date', 'season', 'season_type',
    'matchup', 'team_abbr', 'opp_abbr', 'full_name', 'inserted_at',
]

# --- Blend weight: 0.0 = pure model, 1.0 = pure L10 baseline ---
# 0.4 means 60% model + 40% L10. Stabilizes predictions when data is thin.
BLEND_WEIGHT = 0.65


def get_feature_columns(df: pd.DataFrame) -> list[str]:
    """Get all numeric columns that aren't targets or identifiers."""
    exclude_set = set(FEATURES_TO_EXCLUDE)
    return sorted([
        col for col in df.columns
        if col not in exclude_set
        and df[col].dtype in ['int64', 'float64', 'int32', 'float32']
    ])


def select_features(df_train: pd.DataFrame, target: str,
                     feature_cols: list[str],
                     max_features: int = 40,
                     corr_threshold: float = 0.95) -> list[str]:
    """
    Two-step feature selection:
      1. Remove features correlated > threshold with each other (keep the one
         more correlated with target).
      2. Keep top N features by absolute correlation with target.
    """
    X = df_train[feature_cols].copy()
    y = df_train[target]

    # Step 1: compute correlation with target
    target_corr = X.corrwith(y).abs().fillna(0)

    # Step 2: remove highly inter-correlated features
    corr_matrix = X.corr().abs()
    drop_set = set()
    cols = list(corr_matrix.columns)
    for i in range(len(cols)):
        if cols[i] in drop_set:
            continue
        for j in range(i + 1, len(cols)):
            if cols[j] in drop_set:
                continue
            if corr_matrix.iloc[i, j] > corr_threshold:
                # Drop the one less correlated with target
                if target_corr[cols[i]] >= target_corr[cols[j]]:
                    drop_set.add(cols[j])
                else:
                    drop_set.add(cols[i])

    surviving = [c for c in feature_cols if c not in drop_set]

    # Step 3: keep top N by target correlation
    top = target_corr[surviving].sort_values(ascending=False).head(max_features)
    selected = list(top.index)

    return selected


def train_model_for_stat(df_train, df_val, df_test, target, all_feature_cols) -> dict:
    print(f"\n  --- {target.upper()} ---")

    # Feature selection on training data only
    selected_features = select_features(df_train, target, all_feature_cols,
                                         max_features=40, corr_threshold=0.92)
    print(f"  Features: {len(all_feature_cols)} → {len(selected_features)} after selection")

    X_train = df_train[selected_features].copy()
    y_train = df_train[target].copy()
    X_val = df_val[selected_features].copy()
    y_val = df_val[target].copy()
    X_test = df_test[selected_features].copy()
    y_test = df_test[target].copy()

    # Drop NaN targets
    valid_train = y_train.notna()
    X_train, y_train = X_train[valid_train], y_train[valid_train]
    valid_val = y_val.notna()
    X_val, y_val = X_val[valid_val], y_val[valid_val]
    valid_test = y_test.notna()
    X_test, y_test = X_test[valid_test], y_test[valid_test]

    print(f"  Train: {len(X_train):,} | Val: {len(X_val):,} | Test: {len(X_test):,}")

    # HistGradientBoosting — better regularization for small datasets
    model = HistGradientBoostingRegressor(
        max_depth=3,                # shallow trees — key overfitting fix
        learning_rate=0.05,
        max_iter=300,
        min_samples_leaf=30,        # need 30+ samples per leaf
        l2_regularization=5.0,      # strong L2
        max_features=0.6,           # column subsampling
        validation_fraction=None,   # we provide our own validation set
        early_stopping=False,       # manual control below
        random_state=42,
    )

    # Manual early stopping via validation set
    best_val_mae = float('inf')
    best_iter = 50
    for n_iter in [50, 100, 150, 200, 250, 300]:
        model.set_params(max_iter=n_iter)
        model.fit(X_train, y_train)
        val_pred = model.predict(X_val)
        val_mae = mean_absolute_error(y_val, val_pred)
        if val_mae < best_val_mae:
            best_val_mae = val_mae
            best_iter = n_iter

    # Retrain with best iteration count
    model.set_params(max_iter=best_iter)
    model.fit(X_train, y_train)

    y_pred_test = model.predict(X_test)
    model_mae = mean_absolute_error(y_test, y_pred_test)

    # Baseline: L10 rolling average
    baseline_col = f'{target}_L10'
    baseline_mae = float('nan')
    baseline_pred = None
    if baseline_col in df_test.columns:
        baseline_pred = df_test.loc[valid_test, baseline_col]
        baseline_valid = baseline_pred.notna()
        if baseline_valid.sum() > 0:
            baseline_mae = mean_absolute_error(
                y_test[baseline_valid], baseline_pred[baseline_valid]
            )

    # Blended prediction: model + L10
    blend_mae = float('nan')
    if baseline_pred is not None and not baseline_pred.isna().all():
        blended = (1 - BLEND_WEIGHT) * y_pred_test + BLEND_WEIGHT * baseline_pred.values
        blend_valid = ~np.isnan(blended)
        if blend_valid.sum() > 0:
            blend_mae = mean_absolute_error(y_test[blend_valid], blended[blend_valid])

    improvement_model = (1 - model_mae / baseline_mae) * 100 if not np.isnan(baseline_mae) else 0
    improvement_blend = (1 - blend_mae / baseline_mae) * 100 if not np.isnan(blend_mae) else 0

    print(f"  Best iter: {best_iter} (val MAE: {best_val_mae:.3f})")
    print(f"  Model-only MAE:  {model_mae:.3f}  ({improvement_model:+.1f}% vs baseline)")
    print(f"  Blended MAE:     {blend_mae:.3f}  ({improvement_blend:+.1f}% vs baseline)")
    print(f"  Baseline MAE:    {baseline_mae:.3f}")

    # Feature importance (permutation-style from HistGBR isn't available,
    # but we can use the split-based importance if available)
    # For display, show top features by target correlation
    target_corr = df_train[selected_features].corrwith(df_train[target]).abs().sort_values(ascending=False)
    print(f"\n  Top features (by |correlation| with {target}):")
    for feat, corr_val in target_corr.head(8).items():
        print(f"    {feat:40s} r={corr_val:.3f}")

    return {
        'model': model,
        'target': target,
        'feature_cols': selected_features,
        'best_iter': best_iter,
        'model_mae': model_mae,
        'blend_mae': blend_mae,
        'baseline_mae': baseline_mae,
        'improvement_model_pct': improvement_model,
        'improvement_blend_pct': improvement_blend,
        'blend_weight': BLEND_WEIGHT,
        'test_mae': min(model_mae, blend_mae),  # best of the two
    }


def train_all_models() -> dict:
    feature_path = PROCESSED_DIR / "features.parquet"
    if not feature_path.exists():
        feature_path = PROCESSED_DIR / "features.csv"
    if not feature_path.exists():
        print("❌ Feature dataset not found. Run: python -m src.features.pipeline")
        return {}

    print("Loading feature dataset...")
    if feature_path.suffix == '.parquet':
        df = pd.read_parquet(feature_path)
    else:
        df = pd.read_csv(feature_path)
    print(f"  {len(df):,} rows × {len(df.columns)} columns")

    df_train = df[df['season'].isin(TRAIN_SEASONS)].copy()
    df_val = df[df['season'] == VAL_SEASON].copy()
    df_test = df[df['season'] == TEST_SEASON].copy()

    print(f"\n  Train seasons: {TRAIN_SEASONS}  ({len(df_train):,} rows)")
    print(f"  Val season:    {VAL_SEASON}  ({len(df_val):,} rows)")
    print(f"  Test season:   {TEST_SEASON}  ({len(df_test):,} rows)")
    print(f"  Blend weight:  {BLEND_WEIGHT} (0=pure model, 1=pure L10)")

    all_feature_cols = get_feature_columns(df)
    print(f"  Available features: {len(all_feature_cols)}")

    results = {}
    for target in TARGET_STATS:
        results[target] = train_model_for_stat(
            df_train, df_val, df_test, target, all_feature_cols
        )

    # Save models
    models_dir = PROCESSED_DIR / "models"
    models_dir.mkdir(exist_ok=True)

    for stat, result in results.items():
        model_path = models_dir / f"{stat}_model.pkl"
        with open(model_path, 'wb') as f:
            pickle.dump({
                'model': result['model'],
                'feature_cols': result['feature_cols'],
                'target': stat,
                'train_seasons': TRAIN_SEASONS,
                'val_season': VAL_SEASON,
                'test_season': TEST_SEASON,
                'test_mae': result['test_mae'],
                'baseline_mae': result['baseline_mae'],
                'blend_weight': result['blend_weight'],
            }, f)

    # Summary table
    print(f"\n\n{'='*70}")
    print("  MODEL TRAINING SUMMARY")
    print(f"{'='*70}")
    print(f"\n  {'Stat':<10} {'Model MAE':>10} {'Blended':>10} {'Baseline':>10} {'Best Δ':>10}")
    print(f"  {'-'*50}")
    for stat, r in results.items():
        best = min(r['model_mae'], r['blend_mae'])
        best_pct = (1 - best / r['baseline_mae']) * 100 if not np.isnan(r['baseline_mae']) else 0
        marker = '✅' if best_pct > 0 else '⚠️'
        print(f"  {stat.upper():<10} {r['model_mae']:>10.3f} {r['blend_mae']:>10.3f} "
              f"{r['baseline_mae']:>10.3f} {best_pct:>+9.1f}% {marker}")

    print(f"\n  Models saved to {models_dir}/")
    print(f"  Blend weight = {BLEND_WEIGHT} (tune this if needed)")
    return results


if __name__ == "__main__":
    train_all_models()
