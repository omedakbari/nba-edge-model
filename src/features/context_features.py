"""
Game-context features: rest, home/away, back-to-back, and season progression.
"""

import pandas as pd
import numpy as np


def add_context_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add days_rest, is_b2b, season_game_num, and game_month."""
    df = df.sort_values(['player_id', 'game_date']).copy()

    df['prev_game_date'] = df.groupby('player_id')['game_date'].shift(1)
    df['days_rest'] = (
        pd.to_datetime(df['game_date']) - pd.to_datetime(df['prev_game_date'])
    ).dt.days
    df['days_rest'] = df['days_rest'].clip(upper=10)
    df['is_b2b'] = (df['days_rest'] == 1).astype(int)
    df['season_game_num'] = df.groupby(['player_id', 'season']).cumcount() + 1
    df['game_month'] = pd.to_datetime(df['game_date']).dt.month
    df = df.drop(columns=['prev_game_date'], errors='ignore')

    print(f"  ✅ Context features complete")
    return df
