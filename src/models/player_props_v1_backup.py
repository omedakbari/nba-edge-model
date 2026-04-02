"""
Player props prediction model. Trains XGBoost regression models
to predict player stat lines. One model per stat type.

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
import xgboost as xgb

from config.settings import PROCESSED_DIR

TARGET_STATS = ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']
TRAIN_SEASONS = ['2019-20', '2020-21', '2021-22', '2022-23', '2023-24']
TEST_SEASON = '2024-25'

FEATURES_TO_EXCLUDE = [
    'pts', 'reb', 'ast', 'stl', 'blk', 'fg3m', 'tov', 'pf',
    'fgm', 'fga', 'fg3a', 'ftm', 'fta', 'oreb', 'dreb',
    'pts_reb', 'pts_ast', 'reb_ast', 'pra', 'plus_minus', 'min', 'wl',
    'id', 'player_id', 'game_id', 'game_date', 'season', 'season_type',
    'matchup', 'team_abbr', 'opp_abbr', 'full_name', 'inserted_at',
]


def get_feature_columns(df: pd.DataFrame) -> list[str]:
    exclude_set = set(FEATURES_TO_EXCLUDE)
    return sorted([
        col for col in df.columns
        if col not in exclude_set
        and df[col].dtype in ['int64', 'float64', 'int32', 'float32']
    ])


def train_model_for_stat(df_train, df_test, target, feature_cols) -> dict:
    print(f"\n  --- Training model for: {target.upper()} ---")

    X_train = df_train[feature_cols].copy()
    y_train = df_train[target].copy()
    X_test = df_test[feature_cols].copy()
    y_test = df_test[target].copy()

    valid_train = y_train.notna()
    X_train, y_train = X_train[valid_train], y_train[valid_train]
    valid_test = y_test.notna()
    X_test, y_test = X_test[valid_test], y_test[valid_test]

    print(f"  Train: {len(X_train):,} rows, Test: {len(X_test):,} rows")

    params = {
        'objective': 'reg:squarederror',
        'max_depth': 6,
        'learning_rate': 0.05,
        'n_estimators': 500,
        'min_child_weight': 10,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'reg_alpha': 0.1,
        'reg_lambda': 1.0,
        'random_state': 42,
        'n_jobs': -1,
        'early_stopping_rounds': 50,
    }

    model = xgb.XGBRegressor(**params)
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

    y_pred_train = model.predict(X_train)
    y_pred_test = model.predict(X_test)

    train_mae = mean_absolute_error(y_train, y_pred_train)
    test_mae = mean_absolute_error(y_test, y_pred_test)
    test_rmse = np.sqrt(mean_squared_error(y_test, y_pred_test))

    baseline_col = f'{target}_L10'
    baseline_mae = float('nan')
    if baseline_col in df_test.columns:
        baseline_pred = df_test.loc[valid_test, baseline_col]
        baseline_valid = baseline_pred.notna()
        if baseline_valid.sum() > 0:
            baseline_mae = mean_absolute_error(y_test[baseline_valid], baseline_pred[baseline_valid])

    improvement = (1 - test_mae / baseline_mae) * 100 if not np.isnan(baseline_mae) else 0

    print(f"  Train MAE: {train_mae:.2f}")
    print(f"  Test MAE:  {test_mae:.2f}")
    print(f"  Test RMSE: {test_rmse:.2f}")
    print(f"  Baseline MAE (L10 avg): {baseline_mae:.2f}")
    print(f"  Improvement over baseline: {improvement:.1f}%")

    importance = pd.DataFrame({
        'feature': feature_cols,
        'importance': model.feature_importances_,
    }).sort_values('importance', ascending=False)

    print(f"\n  Top 10 features:")
    for _, row in importance.head(10).iterrows():
        print(f"    {row['feature']:40s} {row['importance']:.4f}")

    return {
        'model': model,
        'target': target,
        'feature_cols': feature_cols,
        'train_mae': train_mae,
        'test_mae': test_mae,
        'test_rmse': test_rmse,
        'baseline_mae': baseline_mae,
        'improvement_pct': improvement,
        'feature_importance': importance,
        'test_predictions': pd.DataFrame({
            'player_id': df_test.loc[valid_test, 'player_id'].values,
            'game_date': df_test.loc[valid_test, 'game_date'].values,
            'actual': y_test.values,
            'predicted': y_pred_test,
        }),
    }


def train_all_models() -> dict:
    feature_path = PROCESSED_DIR / "features.parquet"
    if not feature_path.exists():
        print("❌ Feature dataset not found. Run the feature pipeline first:")
        print("   python -m src.features.pipeline")
        return {}

    print("Loading feature dataset...")
    df = pd.read_parquet(feature_path)
    print(f"  {len(df):,} rows × {len(df.columns)} columns")

    df_train = df[df['season'].isin(TRAIN_SEASONS)].copy()
    df_test = df[df['season'] == TEST_SEASON].copy()

    print(f"\n  Train seasons: {TRAIN_SEASONS}")
    print(f"  Test season: {TEST_SEASON}")
    print(f"  Train rows: {len(df_train):,}")
    print(f"  Test rows: {len(df_test):,}")

    feature_cols = get_feature_columns(df)
    print(f"  Features: {len(feature_cols)}")

    results = {}
    for target in TARGET_STATS:
        results[target] = train_model_for_stat(df_train, df_test, target, feature_cols)

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
                'test_season': TEST_SEASON,
                'test_mae': result['test_mae'],
            }, f)

    print(f"\n\n{'='*60}")
    print("  MODEL TRAINING SUMMARY")
    print(f"{'='*60}")
    print(f"\n  {'Stat':<10} {'Test MAE':>10} {'Baseline':>10} {'Improvement':>12}")
    print(f"  {'-'*42}")
    for stat, result in results.items():
        print(f"  {stat.upper():<10} {result['test_mae']:>10.2f} {result['baseline_mae']:>10.2f} "
              f"{result['improvement_pct']:>11.1f}%")

    print(f"\n  Models saved to {models_dir}/")
    return results


if __name__ == "__main__":
    train_all_models()
