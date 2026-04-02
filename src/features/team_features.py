"""
Team-level features: opponent defensive quality, pace, and strength.
Computed from game logs themselves (self-contained pipeline).
"""

import pandas as pd
import numpy as np


def compute_team_defensive_stats(df: pd.DataFrame, window: int = 15) -> pd.DataFrame:
    team_game = df.groupby(['game_id', 'game_date', 'opp_abbr']).agg(
        pts_allowed=('pts', 'sum'),
        reb_allowed=('reb', 'sum'),
        ast_allowed=('ast', 'sum'),
        fg3m_allowed=('fg3m', 'sum'),
    ).reset_index()

    team_game = team_game.rename(columns={'opp_abbr': 'def_team'})
    team_game = team_game.sort_values(['def_team', 'game_date'])

    for stat in ['pts_allowed', 'reb_allowed', 'ast_allowed', 'fg3m_allowed']:
        shifted = team_game.groupby('def_team')[stat].shift(1)
        team_game[f'{stat}_L{window}'] = shifted.groupby(team_game['def_team']).transform(
            lambda x: x.rolling(window=window, min_periods=5).mean()
        )

    keep_cols = ['game_id', 'def_team'] + [c for c in team_game.columns if f'_L{window}' in c]
    return team_game[keep_cols].copy()


def compute_team_pace(df: pd.DataFrame, window: int = 15) -> pd.DataFrame:
    team_game = df.groupby(['game_id', 'game_date', 'team_abbr']).agg(
        total_fga=('fga', 'sum'),
        total_tov=('tov', 'sum'),
        total_fta=('fta', 'sum'),
    ).reset_index()

    team_game['est_poss'] = team_game['total_fga'] + team_game['total_tov'] + 0.44 * team_game['total_fta']
    team_game = team_game.sort_values(['team_abbr', 'game_date'])

    shifted = team_game.groupby('team_abbr')['est_poss'].shift(1)
    team_game[f'pace_L{window}'] = shifted.groupby(team_game['team_abbr']).transform(
        lambda x: x.rolling(window=window, min_periods=5).mean()
    )

    return team_game[['game_id', 'team_abbr', f'pace_L{window}']].copy()


def add_team_features(df: pd.DataFrame, window: int = 15) -> pd.DataFrame:
    print("  Computing team defensive stats...")
    team_def = compute_team_defensive_stats(df, window=window)

    print("  Computing team pace...")
    team_pace = compute_team_pace(df, window=window)

    df = df.merge(
        team_def,
        left_on=['game_id', 'opp_abbr'],
        right_on=['game_id', 'def_team'],
        how='left',
    )
    df = df.drop(columns=['def_team'], errors='ignore')

    df = df.merge(
        team_pace,
        left_on=['game_id', 'opp_abbr'],
        right_on=['game_id', 'team_abbr'],
        how='left',
        suffixes=('', '_opp_pace'),
    )
    df = df.drop(columns=['team_abbr_opp_pace'], errors='ignore')

    player_pace = team_pace.rename(columns={f'pace_L{window}': f'own_pace_L{window}'})
    df = df.merge(
        player_pace,
        left_on=['game_id', 'team_abbr'],
        right_on=['game_id', 'team_abbr'],
        how='left',
    )

    print(f"  ✅ Team features complete")
    return df
