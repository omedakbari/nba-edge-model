"""
Player-level feature engineering.

Transforms raw game logs into rolling stats, trends, and hit-rate features
that capture a player's recent form and consistency.

KEY RULE: Every feature uses ONLY data from BEFORE the game being predicted.
This is enforced by using shift(1) after groupby — the current row's stats
are never included in its own features.
"""

import pandas as pd
import numpy as np


def add_rolling_features(df: pd.DataFrame, windows: list[int] = [5, 10, 20]) -> pd.DataFrame:
    stat_cols = ['pts', 'reb', 'ast', 'stl', 'blk', 'fg3m',
                 'pra', 'pts_reb', 'reb_ast', 'pts_ast',
                 'min', 'fga', 'fg3a', 'fta', 'tov', 'pf']

    df = df.sort_values(['player_id', 'game_date']).copy()

    for stat in stat_cols:
        shifted = df.groupby('player_id')[stat].shift(1)
        for w in windows:
            col_name = f'{stat}_L{w}'
            df[col_name] = shifted.groupby(df['player_id']).transform(
                lambda x: x.rolling(window=w, min_periods=max(1, w // 2)).mean()
            )

    return df


def add_hit_rate_features(df: pd.DataFrame, windows: list[int] = [5, 10]) -> pd.DataFrame:
    thresholds = {
        'pts': [15.5, 19.5, 24.5, 29.5],
        'reb': [4.5, 6.5, 8.5, 10.5],
        'ast': [3.5, 5.5, 7.5, 9.5],
        'fg3m': [1.5, 2.5, 3.5],
        'pra': [19.5, 24.5, 29.5, 34.5, 39.5],
        'reb_ast': [9.5, 12.5, 15.5],
    }

    df = df.sort_values(['player_id', 'game_date']).copy()

    for stat, lines in thresholds.items():
        shifted = df.groupby('player_id')[stat].shift(1)
        for line in lines:
            exceeded = (shifted >= line).astype(float)
            for w in windows:
                col_name = f'{stat}_hr_{str(line).replace(".", "_")}_L{w}'
                df[col_name] = exceeded.groupby(df['player_id']).transform(
                    lambda x: x.rolling(window=w, min_periods=max(1, w // 2)).mean()
                )

    return df


def add_trend_features(df: pd.DataFrame) -> pd.DataFrame:
    stat_cols = ['pts', 'reb', 'ast', 'fg3m', 'pra', 'min']
    for stat in stat_cols:
        l5_col = f'{stat}_L5'
        l10_col = f'{stat}_L10'
        if l5_col in df.columns and l10_col in df.columns:
            df[f'{stat}_trend'] = (df[l5_col] - df[l10_col]) / df[l10_col].replace(0, np.nan)
    return df


def add_consistency_features(df: pd.DataFrame) -> pd.DataFrame:
    stat_cols = ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast', 'min']
    df = df.sort_values(['player_id', 'game_date']).copy()

    for stat in stat_cols:
        shifted = df.groupby('player_id')[stat].shift(1)
        df[f'{stat}_std_L10'] = shifted.groupby(df['player_id']).transform(
            lambda x: x.rolling(window=10, min_periods=5).std()
        )
        l10_col = f'{stat}_L10'
        if l10_col in df.columns:
            df[f'{stat}_cv_L10'] = df[f'{stat}_std_L10'] / df[l10_col].replace(0, np.nan)

    return df


def add_minutes_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(['player_id', 'game_date']).copy()
    shifted_min = df.groupby('player_id')['min'].shift(1)

    df['min_L3'] = shifted_min.groupby(df['player_id']).transform(
        lambda x: x.rolling(window=3, min_periods=1).mean()
    )
    df['min_std_L10'] = shifted_min.groupby(df['player_id']).transform(
        lambda x: x.rolling(window=10, min_periods=5).std()
    )
    low_min = (shifted_min < 20).astype(float)
    df['low_min_count_L5'] = low_min.groupby(df['player_id']).transform(
        lambda x: x.rolling(window=5, min_periods=1).sum()
    )

    return df


def build_player_features(df: pd.DataFrame) -> pd.DataFrame:
    print("  Building rolling averages...")
    df = add_rolling_features(df)
    print("  Building hit-rate features...")
    df = add_hit_rate_features(df)
    print("  Building trend features...")
    df = add_trend_features(df)
    print("  Building consistency features...")
    df = add_consistency_features(df)
    print("  Building minutes features...")
    df = add_minutes_features(df)
    print(f"  ✅ Player features complete: {len(df.columns)} total columns")
    return df
