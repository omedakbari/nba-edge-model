"""
Master feature pipeline. Loads raw game logs, applies all feature
engineering, and outputs a clean dataset ready for model training.

Usage:
    python -m src.features.pipeline
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import sqlite3

from config.settings import DB_PATH, PROCESSED_DIR
from src.features.player_features import build_player_features
from src.features.context_features import add_context_features
from src.features.team_features import add_team_features


def build_feature_dataset(min_minutes: float = 12.0) -> pd.DataFrame:
    print("Loading game logs from database...")
    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql("""
        SELECT gl.*, p.full_name
        FROM game_logs gl
        JOIN players p ON gl.player_id = p.player_id
        WHERE gl.min > 0
        ORDER BY gl.player_id, gl.game_date
    """, conn)
    conn.close()

    print(f"  Loaded {len(df):,} rows")
    df['game_date'] = pd.to_datetime(df['game_date'])
    df = df[df['min'] >= min_minutes].copy()
    print(f"  After {min_minutes}+ min filter: {len(df):,} rows")

    print("\n--- Player Features ---")
    df = build_player_features(df)

    print("\n--- Context Features ---")
    df = add_context_features(df)

    print("\n--- Team Features ---")
    df = add_team_features(df)

    initial_rows = len(df)
    df = df.dropna(subset=['pts_L10', 'reb_L10', 'ast_L10'])
    print(f"\n  Dropped {initial_rows - len(df):,} rows with missing features (early-season games)")
    print(f"  Final dataset: {len(df):,} rows × {len(df.columns)} columns")

    return df


def save_feature_dataset(df: pd.DataFrame, filename: str = "features.parquet") -> Path:
    path = PROCESSED_DIR / filename
    df.to_parquet(path, index=False)
    print(f"\n  ✅ Saved to {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return path


if __name__ == "__main__":
    df = build_feature_dataset()
    save_feature_dataset(df)

    print("\n=== Feature Dataset Summary ===")
    print(f"  Rows: {len(df):,}")
    print(f"  Columns: {len(df.columns)}")
    print(f"  Seasons: {sorted(df['season'].unique())}")
    print(f"  Players: {df['player_id'].nunique()}")
    print(f"\n  Feature columns ({len([c for c in df.columns if '_L' in c or '_hr_' in c or '_trend' in c])} engineered):")
    for col in sorted(df.columns):
        if '_L' in col or '_hr_' in col or '_trend' in col:
            print(f"    {col}")
