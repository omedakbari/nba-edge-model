"""
Live daily picks generator — v2.

Automatically fetches today's NBA schedule, builds current-form features
for every player on a live roster, and outputs two SAFE Flex cards.

Usage:
    python scripts/generate_todays_picks.py [--date 2026-04-02] [--season 2025-26]

Stat thresholds (minimum P(over) to qualify):
    REB / AST      : P >= 60%  (well-calibrated in backtest)
    PRA / REB_AST  : P >= 65%
    PTS            : P >= 70%  (noisier — only high-confidence picks)
"""

import argparse
import sys
import time
import sqlite3
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import pickle
from scipy import stats as scipy_stats

from config.settings import PROCESSED_DIR, DB_PATH, NBA_API_DELAY, CURRENT_SEASON


# ── Constants ──────────────────────────────────────────────────────────────────

COMPOSITE_STATS = {
    'pra':     ['pts', 'reb', 'ast'],
    'reb_ast': ['reb', 'ast'],
}

STAT_MIN_PROB = {
    'reb':     0.60,
    'ast':     0.60,
    'pra':     0.65,
    'reb_ast': 0.65,
    'pts':     0.70,
}

STATS = ['reb', 'ast', 'pra', 'reb_ast', 'pts']
MIN_MINUTES = 20
DEF_WINDOW = 15


# ── Schedule / roster fetching ─────────────────────────────────────────────────

def get_todays_games(target_date: str) -> list[dict]:
    """Fetch scheduled games from NBA.com for target_date ('YYYY-MM-DD')."""
    from nba_api.stats.endpoints import scoreboardv2
    from nba_api.stats.static import teams as static_teams

    dt = datetime.strptime(target_date, "%Y-%m-%d")
    nba_date = dt.strftime("%m/%d/%Y")

    time.sleep(NBA_API_DELAY)
    try:
        board = scoreboardv2.ScoreboardV2(game_date=nba_date, league_id='00', day_offset=0)
        games_df = board.get_data_frames()[0]
    except Exception as e:
        print(f"  Error fetching schedule: {e}")
        return []

    if games_df.empty:
        return []

    team_dict = {t['id']: t['abbreviation'] for t in static_teams.get_teams()}

    result = []
    for _, row in games_df.iterrows():
        home_id = int(row.get('HOME_TEAM_ID', 0))
        away_id = int(row.get('VISITOR_TEAM_ID', 0))
        result.append({
            'game_id': str(row['GAME_ID']),
            'home_team_id': home_id,
            'away_team_id': away_id,
            'home_abbr': team_dict.get(home_id, 'UNK'),
            'away_abbr': team_dict.get(away_id, 'UNK'),
        })
    return result


def get_roster_player_ids(team_id: int, season: str) -> list[int]:
    """Get active roster player IDs for a team."""
    from nba_api.stats.endpoints import commonteamroster

    time.sleep(NBA_API_DELAY)
    try:
        roster = commonteamroster.CommonTeamRoster(team_id=team_id, season=season)
        df = roster.get_data_frames()[0]
        return [int(pid) for pid in df['PLAYER_ID'].tolist()]
    except Exception as e:
        print(f"  Warning: could not fetch roster for team {team_id}: {e}")
        return []


def get_todays_player_contexts(target_date: str, season: str) -> list[dict]:
    """
    Return [{player_id, matchup, team_abbr, opp_abbr, is_home}, ...]
    for every player scheduled to play on target_date.
    """
    games = get_todays_games(target_date)
    if not games:
        print("  No games found for this date.")
        return []

    print(f"  Found {len(games)} game(s) on {target_date}")
    contexts = []

    for game in games:
        label = f"{game['away_abbr']} @ {game['home_abbr']}"
        print(f"    Fetching rosters for {label}...", end="", flush=True)

        home_ids = get_roster_player_ids(game['home_team_id'], season)
        away_ids = get_roster_player_ids(game['away_team_id'], season)

        for pid in home_ids:
            contexts.append({
                'player_id': pid,
                'matchup': f"{game['home_abbr']} vs. {game['away_abbr']}",
                'team_abbr': game['home_abbr'],
                'opp_abbr': game['away_abbr'],
                'is_home': 1,
            })
        for pid in away_ids:
            contexts.append({
                'player_id': pid,
                'matchup': f"{game['away_abbr']} @ {game['home_abbr']}",
                'team_abbr': game['away_abbr'],
                'opp_abbr': game['home_abbr'],
                'is_home': 0,
            })
        print(f" {len(home_ids) + len(away_ids)} players")

    return contexts


# ── Opponent context from DB ───────────────────────────────────────────────────

def get_opponent_def_stats(opp_abbr: str, before_date: str, window: int = DEF_WINDOW) -> dict:
    """Compute opponent's rolling defensive stats from recent game_logs."""
    conn = sqlite3.connect(str(DB_PATH))
    query = """
        SELECT game_id,
               SUM(pts)  AS pts_allowed,
               SUM(reb)  AS reb_allowed,
               SUM(ast)  AS ast_allowed,
               SUM(fg3m) AS fg3m_allowed
        FROM game_logs
        WHERE opp_abbr = ? AND game_date < ?
        GROUP BY game_id
        ORDER BY game_id DESC
        LIMIT ?
    """
    df = pd.read_sql(query, conn, params=(opp_abbr, before_date, window))
    conn.close()

    if df.empty:
        return {}

    return {
        f'pts_allowed_L{window}':  df['pts_allowed'].mean(),
        f'reb_allowed_L{window}':  df['reb_allowed'].mean(),
        f'ast_allowed_L{window}':  df['ast_allowed'].mean(),
        f'fg3m_allowed_L{window}': df['fg3m_allowed'].mean(),
    }


def get_team_pace(team_abbr: str, before_date: str, window: int = DEF_WINDOW) -> dict:
    """Compute team's rolling pace estimate from recent game_logs."""
    conn = sqlite3.connect(str(DB_PATH))
    query = """
        SELECT game_id,
               SUM(fga) AS tfga,
               SUM(tov) AS ttov,
               SUM(fta) AS tfta
        FROM game_logs
        WHERE team_abbr = ? AND game_date < ?
        GROUP BY game_id
        ORDER BY game_id DESC
        LIMIT ?
    """
    df = pd.read_sql(query, conn, params=(team_abbr, before_date, window))
    conn.close()

    if df.empty:
        return {}

    df['est_poss'] = df['tfga'] + df['ttov'] + 0.44 * df['tfta']
    return {f'pace_L{window}': df['est_poss'].mean()}


# ── Feature construction ───────────────────────────────────────────────────────

def build_todays_features(features_df: pd.DataFrame,
                           player_contexts: list[dict],
                           target_date: str) -> pd.DataFrame:
    """
    For each player playing today, grab their most recent row from features_df
    (most recent game played BEFORE today) and override context/matchup features.
    """
    features_df = features_df.copy()
    features_df['game_date'] = pd.to_datetime(features_df['game_date'])
    target_dt = pd.to_datetime(target_date)

    # Most recent feature row per player, before today
    recent_df = (
        features_df[features_df['game_date'] < target_dt]
        .sort_values('game_date')
        .groupby('player_id')
        .last()
        .reset_index()  # brings player_id back as a column
    )
    recent_map = recent_df.set_index('player_id').to_dict(orient='index')

    # Cache per-team DB lookups
    def_cache: dict = {}
    pace_cache: dict = {}

    rows = []
    for ctx in player_contexts:
        pid = ctx['player_id']
        if pid not in recent_map:
            continue

        row = dict(recent_map[pid])
        row['player_id'] = pid  # re-add since set_index removed it

        # Minutes filter — drop players not getting real run
        min_l10 = row.get('min_L10') or 0
        if min_l10 < MIN_MINUTES:
            continue

        # Override context features for today's game
        last_game_date = row['game_date']
        days_rest = (target_dt - last_game_date).days
        row['days_rest'] = min(int(days_rest), 10)
        row['is_b2b'] = int(days_rest == 1)
        row['game_month'] = target_dt.month
        row['is_home'] = ctx['is_home']
        row['matchup'] = ctx['matchup']
        row['opp_abbr'] = ctx['opp_abbr']
        row['team_abbr'] = ctx['team_abbr']
        row['game_date'] = target_dt  # for display only

        # Override opponent defensive stats
        opp = ctx['opp_abbr']
        if opp not in def_cache:
            def_cache[opp] = get_opponent_def_stats(opp, target_date)
        row.update(def_cache[opp])

        # Override team pace
        team = ctx['team_abbr']
        if team not in pace_cache:
            pace_cache[team] = get_team_pace(team, target_date)
        row.update(pace_cache[team])

        rows.append(row)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).reset_index(drop=True)


# ── Model loading and prediction ───────────────────────────────────────────────

def load_model(stat: str) -> dict:
    path = PROCESSED_DIR / "models" / f"{stat}_model.pkl"
    if not path.exists():
        raise FileNotFoundError(f"Model not found: {path}. Run: python -m src.models.player_props")
    with open(path, 'rb') as f:
        return pickle.load(f)


def prob_over(pred: float, line: float, std: float) -> float:
    if std <= 0 or np.isnan(std):
        std = max(abs(pred) * 0.2, 1.0)
    z = (line - pred) / std
    return float(np.clip(1 - scipy_stats.norm.cdf(z), 0.01, 0.99))


def predict_stat(df: pd.DataFrame, stat: str) -> pd.Series:
    """Return blended model predictions (model + L10 baseline) for a stat."""
    components = COMPOSITE_STATS.get(stat)
    if components:
        models = {c: load_model(c) for c in components}
        valid = pd.Series(True, index=df.index)
        for m in models.values():
            cols = [c for c in m['feature_cols'] if c in df.columns]
            valid &= df[cols].notna().all(axis=1)
        raw_preds = pd.Series(0.0, index=df.index)
        for c, m in models.items():
            cols = [col for col in m['feature_cols'] if col in df.columns]
            sub = df.loc[valid, cols]
            model_pred = pd.Series(m['model'].predict(sub), index=sub.index)
            # Blend: model + L10 baseline
            bw = m.get('blend_weight', 0.65)
            baseline = df.loc[valid, f'{c}_L10'] if f'{c}_L10' in df.columns else model_pred
            blended = (1 - bw) * model_pred + bw * baseline
            raw_preds.loc[valid] += blended
        raw_preds[~valid] = np.nan
    else:
        m = load_model(stat)
        cols = [c for c in m['feature_cols'] if c in df.columns]
        valid = df[cols].notna().all(axis=1)
        raw_preds = pd.Series(np.nan, index=df.index)
        model_pred = pd.Series(m['model'].predict(df.loc[valid, cols]), index=df.loc[valid].index)
        bw = m.get('blend_weight', 0.65)
        baseline_col = f'{stat}_L10'
        if baseline_col in df.columns:
            baseline = df.loc[valid, baseline_col]
        else:
            baseline = model_pred
        raw_preds.loc[valid] = (1 - bw) * model_pred + bw * baseline

    return raw_preds


# ── Candidate generation ───────────────────────────────────────────────────────

def build_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """Score every (player, stat) pair and return rows above threshold."""
    rows = []
    for stat in STATS:
        l10_col = f'{stat}_L10'
        std_col = f'{stat}_std_L10'
        if l10_col not in df.columns:
            continue

        min_p = STAT_MIN_PROB[stat]
        preds = predict_stat(df, stat)

        for idx, row in df.iterrows():
            pred = preds.get(idx, np.nan)
            if pd.isna(pred):
                continue
            line = row.get(l10_col)
            if pd.isna(line) or float(line) == 0:
                continue
            std_val = row.get(std_col, np.nan)
            std_float = float('nan') if pd.isna(std_val) else float(std_val)
            p = prob_over(float(pred), float(line), std_float)
            if p < min_p:
                continue
            rows.append({
                'player_id': row['player_id'],
                'player':    row.get('full_name', str(row['player_id'])),
                'game':      row.get('matchup', 'UNK'),
                'stat':      stat.upper(),
                'line':      round(float(line), 1),
                'pred':      round(float(pred), 1),
                'prob':      p,
            })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values('prob', ascending=False).reset_index(drop=True)


# ── Card builder ───────────────────────────────────────────────────────────────

def build_card(candidates: pd.DataFrame, n_legs: int, exclude_keys: set) -> list[dict]:
    """
    Greedy highest-confidence selection with constraints:
      - Skip (player_id, stat) keys in exclude_keys
      - One prop per player per card
      - Max 2 legs per game
    """
    used_players: set = set()
    game_counts: dict = {}
    card: list[dict] = []

    for _, row in candidates.iterrows():
        key = (row['player_id'], row['stat'])
        if key in exclude_keys:
            continue
        if row['player_id'] in used_players:
            continue
        if game_counts.get(row['game'], 0) >= 2:
            continue

        card.append(row.to_dict())
        used_players.add(row['player_id'])
        game_counts[row['game']] = game_counts.get(row['game'], 0) + 1

        if len(card) == n_legs:
            break

    return card


# ── Output ─────────────────────────────────────────────────────────────────────

def print_card(card: list[dict], label: str) -> None:
    w = 74
    print(f"\n  {label}")
    print(f"  {'Player':<26} {'Stat':<9} {'Line':>5} {'Pred':>6} {'P(over)':>8}  Matchup")
    print(f"  {'-' * w}")
    for leg in card:
        print(f"  {leg['player']:<26} {leg['stat']:<9} {leg['line']:>5.1f} "
              f"{leg['pred']:>6.1f} {leg['prob']:>8.1%}  {leg['game']}")
    avg_prob = sum(leg['prob'] for leg in card) / len(card)
    print(f"  {'-' * w}")
    print(f"  {'Avg confidence':>42}  {avg_prob:>8.1%}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily SAFE Flex picks (live schedule)")
    parser.add_argument('--date', default=date.today().isoformat(),
                        help='Game date YYYY-MM-DD (default: today)')
    parser.add_argument('--season', default=CURRENT_SEASON,
                        help=f'NBA season string (default: {CURRENT_SEASON})')
    args = parser.parse_args()

    feature_path = PROCESSED_DIR / "features.parquet"
    if not feature_path.exists():
        print("Error: features.parquet not found. Run: python -m src.features.pipeline")
        sys.exit(1)

    print(f"\n  NBA Edge Model — Daily Picks")
    print(f"  Date: {args.date}  |  Season: {args.season}")
    print(f"\n  Loading features...")
    features_df = pd.read_parquet(feature_path)
    print(f"  {len(features_df):,} rows, latest game: {features_df['game_date'].max()}")

    print(f"\n  Fetching today's schedule from NBA.com...")
    player_contexts = get_todays_player_contexts(args.date, args.season)

    if not player_contexts:
        print("  No players found for this date. Check the date or try --date YYYY-MM-DD.")
        sys.exit(0)

    total_players = len(player_contexts)
    print(f"\n  Building features for {total_players} roster slots...")
    df_today = build_todays_features(features_df, player_contexts, args.date)

    if df_today.empty:
        print("  No players have sufficient game history in the database.")
        print("  Run: python scripts/pull_season_data.py --season 2025-26")
        sys.exit(0)

    # Warn if feature data is stale (> 60 days old = missing current season)
    target_dt = pd.to_datetime(args.date)
    db_latest = pd.to_datetime(features_df['game_date']).max()
    staleness = (target_dt - db_latest).days
    if staleness > 60:
        print(f"\n  *** DATA WARNING: DB is {staleness} days stale (latest game: {db_latest.date()}) ***")
        print(f"  *** Lines and rolling stats are from LAST season — picks will be inaccurate ***")
        print(f"  *** Fix:  python scripts/pull_season_data.py --season {CURRENT_SEASON}  (then rebuild features + retrain) ***")

    print(f"  Players with 20+ min history: {len(df_today)} / {total_players}")

    candidates = build_candidates(df_today)

    if candidates.empty:
        print("  No legs met minimum probability thresholds.")
        sys.exit(0)

    print(f"  Qualified legs: {len(candidates)}")

    card1 = build_card(candidates, n_legs=3, exclude_keys=set())
    used = {(leg['player_id'], leg['stat']) for leg in card1}
    card2 = build_card(candidates, n_legs=5, exclude_keys=used)

    print("\n" + "=" * 76)
    print("  SAFE FLEX DAILY PICKS")
    print("=" * 76)

    if card1:
        print_card(card1, "CARD 1  —  3-Leg SAFE Flex")
    else:
        print("\n  CARD 1: Not enough qualifying legs (need 3).")

    if card2:
        print_card(card2, "CARD 2  —  5-Leg SAFE Flex")
    else:
        print("\n  CARD 2: Not enough non-overlapping legs (need 5).")

    print("\n" + "=" * 76)
    print("  THRESHOLDS  REB/AST >= 60%  |  PRA/REB_AST >= 65%  |  PTS >= 70%")
    print("  RULES       0 shared legs between cards  |  max 2 legs/game  |  1 prop/player")
    print("=" * 76 + "\n")


if __name__ == "__main__":
    main()
