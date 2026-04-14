"""
Playoff-specific feature engineering.

Adds context that captures the unique dynamics of playoff basketball:
  - is_playoff flag: lets the model learn different stat base rates
  - series_game_num: G1 vs G7 is very different defensive context
  - Within-series rolling performance: opponent-specific recent form

KEY RULE: All features use shift(1) within their group — a game's stats
are never included in its own features.
"""

import pandas as pd
import numpy as np


def add_playoff_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(['player_id', 'game_date']).copy()

    # Binary flag — lets model learn different base rates for postseason
    df['is_playoff'] = (df['season_type'] == 'Playoffs').astype(int)

    playoff_mask = df['season_type'] == 'Playoffs'

    # Which game in this series (1–7). Regular season: 0.
    # Groups by player × season × opponent to count games in the matchup.
    df['series_game_num'] = 0
    if playoff_mask.any():
        po = df.loc[playoff_mask]
        df.loc[playoff_mask, 'series_game_num'] = (
            po.groupby(['player_id', 'season', 'opp_abbr']).cumcount() + 1
        )

    # Within-series rolling performance vs same opponent (shift(1) to avoid leakage).
    # NaN for game 1 of each series (no prior data); HGBR handles NaN natively.
    for stat in ['pts', 'reb', 'ast', 'pra']:
        col = f'{stat}_series_L3'
        df[col] = np.nan
        if playoff_mask.any():
            po = df.loc[playoff_mask].copy()
            po['_shifted'] = po.groupby(['player_id', 'season', 'opp_abbr'])[stat].shift(1)
            series_roll = po.groupby(['player_id', 'season', 'opp_abbr'])['_shifted'].transform(
                lambda x: x.rolling(window=3, min_periods=1).mean()
            )
            df.loc[playoff_mask, col] = series_roll.values

    playoff_count = playoff_mask.sum()
    reg_count = (~playoff_mask).sum()
    print(f"  ✅ Playoff features complete "
          f"({playoff_count:,} playoff rows, {reg_count:,} regular season rows)")
    return df
