"""
Unit tests for the NBA Edge Model.
Run with: python -m pytest tests/ -v
"""

import sys
import os
import numpy as np
import pandas as pd
import sqlite3
import pickle
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PROCESSED_DIR = Path(__file__).parent.parent / 'data' / 'processed'
DB_PATH = Path(__file__).parent.parent / 'data' / 'nba_edge.db'


class TestDatabase:
    """Tests for data integrity."""

    def test_database_exists(self):
        assert DB_PATH.exists(), "Database file not found"

    def test_game_logs_not_empty(self):
        conn = sqlite3.connect(str(DB_PATH))
        count = pd.read_sql("SELECT COUNT(*) as n FROM game_logs", conn).iloc[0]['n']
        conn.close()
        assert count > 10000, f"Expected >10k rows, got {count}"

    def test_all_seasons_present(self):
        conn = sqlite3.connect(str(DB_PATH))
        seasons = pd.read_sql("SELECT DISTINCT season FROM game_logs", conn)['season'].tolist()
        conn.close()
        expected = ['2019-20', '2020-21', '2021-22', '2022-23', '2023-24', '2024-25', '2025-26']
        for s in expected:
            assert s in seasons, f"Season {s} missing from database"

    def test_no_negative_stats(self):
        conn = sqlite3.connect(str(DB_PATH))
        df = pd.read_sql("SELECT MIN(pts) as min_pts, MIN(reb) as min_reb, MIN(ast) as min_ast FROM game_logs", conn)
        conn.close()
        assert df['min_pts'].iloc[0] >= 0
        assert df['min_reb'].iloc[0] >= 0
        assert df['min_ast'].iloc[0] >= 0

    def test_reasonable_stat_ranges(self):
        conn = sqlite3.connect(str(DB_PATH))
        df = pd.read_sql("SELECT MAX(pts) as max_pts, MAX(reb) as max_reb, MAX(min) as max_min FROM game_logs", conn)
        conn.close()
        assert df['max_pts'].iloc[0] <= 100, "Points > 100 seems wrong"
        assert df['max_reb'].iloc[0] <= 40, "Rebounds > 40 seems wrong"
        assert df['max_min'].iloc[0] <= 60, "Minutes > 60 seems wrong"


class TestFeatures:
    """Tests for feature engineering."""

    def test_feature_file_exists(self):
        assert (PROCESSED_DIR / 'features.parquet').exists(), "Feature dataset not found"

    def test_feature_count(self):
        df = pd.read_parquet(PROCESSED_DIR / 'features.parquet').head(5)
        engineered = [c for c in df.columns if '_L' in c or '_hr_' in c or '_trend' in c]
        assert len(engineered) >= 50, f"Expected 50+ engineered features, got {len(engineered)}"

    def test_no_data_leakage_rolling(self):
        """
        Verify rolling features never include the current game's own stats.

        NOTE: features.parquet itself can't be used for this — build_feature_dataset()
        drops every row with a NaN pts_L10/reb_L10/ast_L10 before saving, so a player's
        leakage-prone first game (which is exactly what a leakage test needs to inspect)
        is never present in the saved file. Instead, call the actual production
        add_rolling_features() directly on a small synthetic game log, matching how
        src/features/pipeline.py invokes it.
        """
        from src.features.player_features import add_rolling_features

        stat_cols = ['pts', 'reb', 'ast', 'stl', 'blk', 'fg3m',
                     'pra', 'pts_reb', 'reb_ast', 'pts_ast',
                     'min', 'fga', 'fg3a', 'fta', 'tov', 'pf']
        n_games = 4
        raw = pd.DataFrame({
            'player_id': [1] * n_games,
            'game_date': pd.date_range('2024-01-01', periods=n_games, freq='3D'),
            **{stat: [10.0, 20.0, 30.0, 999.0] for stat in stat_cols},
        })

        out = add_rolling_features(raw)

        # First game: no prior data exists yet, so every rolling feature must be NaN.
        assert pd.isna(out.iloc[0]['pts_L5']), (
            "First game's pts_L5 should be NaN (no prior games) — leakage guard broken"
        )

        # Fourth game: L5 must be the mean of the *prior* 3 games (10, 20, 30) = 20.0.
        # If the current game's own stat (999) leaked into its own rolling window,
        # this would instead be ~264.75 — the two are unmistakably different.
        assert out.iloc[3]['pts_L5'] == pytest.approx(20.0), (
            f"pts_L5 on the 4th game = {out.iloc[3]['pts_L5']}, expected 20.0 "
            f"(mean of prior 3 games only) — the game's own value appears to have leaked in"
        )

    def test_combo_stats_correct(self):
        df = pd.read_parquet(PROCESSED_DIR / 'features.parquet').head(100)
        if 'pra' in df.columns and 'pts' in df.columns:
            expected_pra = df['pts'] + df['reb'] + df['ast']
            np.testing.assert_array_equal(df['pra'].values, expected_pra.values)

    def test_is_home_binary(self):
        df = pd.read_parquet(PROCESSED_DIR / 'features.parquet').head(1000)
        assert set(df['is_home'].unique()).issubset({0, 1})

    def test_days_rest_reasonable(self):
        df = pd.read_parquet(PROCESSED_DIR / 'features.parquet')
        valid_rest = df['days_rest'].dropna()
        assert valid_rest.min() >= 1, "Days rest should be >= 1"
        assert valid_rest.max() <= 10, "Days rest should be capped at 10"


class TestModels:
    """Tests for trained models."""

    def test_all_models_exist(self):
        for stat in ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']:
            path = PROCESSED_DIR / 'models' / f'{stat}_model.pkl'
            assert path.exists(), f"Model file for {stat} not found"

    def test_models_beat_baseline(self):
        for stat in ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']:
            with open(PROCESSED_DIR / 'models' / f'{stat}_model.pkl', 'rb') as f:
                data = pickle.load(f)
            assert data['test_mae'] < data['baseline_mae'], \
                f"{stat} model ({data['test_mae']:.3f}) doesn't beat baseline ({data['baseline_mae']:.3f})"

    def test_model_can_predict(self):
        with open(PROCESSED_DIR / 'models' / 'pts_model.pkl', 'rb') as f:
            data = pickle.load(f)
        model = data['model']
        feature_cols = data['feature_cols']

        # Create a dummy input
        dummy = pd.DataFrame(np.random.randn(5, len(feature_cols)), columns=feature_cols)
        predictions = model.predict(dummy)
        assert len(predictions) == 5
        assert all(np.isfinite(predictions))


class TestPlots:
    """Tests for generated evaluation artifacts.
    Skipped when plots/ hasn't been generated (they are not tracked in git).
    """
    pytestmark = pytest.mark.skipif(
        not (PROCESSED_DIR / 'plots').exists(),
        reason="plots/ not generated; run training script first",
    )

    def test_plots_exist(self):
        plot_dir = PROCESSED_DIR / 'plots'
        expected_plots = [
            'calibration.png', 'feature_importance.png',
            'model_vs_baseline.png', 'stat_distributions.png',
            'calibration_comparison.png', 'brier_comparison.png',
            'model_evolution.png',
        ]
        for plot in expected_plots:
            assert (plot_dir / plot).exists(), f"Plot {plot} not found"

    def test_plots_not_empty(self):
        plot_dir = PROCESSED_DIR / 'plots'
        for plot_file in plot_dir.glob('*.png'):
            assert plot_file.stat().st_size > 1000, f"Plot {plot_file.name} seems too small"


# Run tests
if __name__ == '__main__':
    test_classes = [TestDatabase, TestFeatures, TestModels, TestPlots]
    total = 0
    passed = 0
    failed = 0

    for cls in test_classes:
        print(f"\n{'='*50}")
        print(f"  {cls.__name__}")
        print(f"{'='*50}")
        instance = cls()
        for method_name in dir(instance):
            if method_name.startswith('test_'):
                total += 1
                try:
                    getattr(instance, method_name)()
                    print(f"  ✅ {method_name}")
                    passed += 1
                except AssertionError as e:
                    print(f"  ❌ {method_name}: {e}")
                    failed += 1
                except Exception as e:
                    print(f"  ❌ {method_name}: {type(e).__name__}: {e}")
                    failed += 1

    print(f"\n{'='*50}")
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
    print(f"{'='*50}")
