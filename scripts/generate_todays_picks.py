"""
Live daily picks generator — v3.

Fetches real PrizePicks lines, today's NBA schedule, and current rosters.
Outputs only picks with an actual PrizePicks market, using real lines instead
of L10 averages. Falls back to L10 mode with a warning if PP is unreachable.

Usage:
    python scripts/generate_todays_picks.py [--date 2026-04-03] [--season 2025-26]

Stat thresholds (minimum P(over line) to qualify):
    REB / AST      : P >= 60%
    PRA / REB_AST  : P >= 65%
    PTS            : P >= 70%
"""

import argparse
import sys
import time
import sqlite3
import unicodedata
import warnings
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import pickle
import requests
from scipy import stats as scipy_stats

warnings.filterwarnings("ignore")

from config.settings import PROCESSED_DIR, DB_PATH, NBA_API_DELAY, CURRENT_SEASON


# ── Constants ──────────────────────────────────────────────────────────────────

COMPOSITE_STATS = {
    'pra':     ['pts', 'reb', 'ast'],
    'reb_ast': ['reb', 'ast'],
}

# Thresholds for use against REAL PrizePicks lines (not fake L10 baselines).
# PP lines are set near expected value, so any P > 55% represents real edge.
# The old 0.70-0.75 thresholds were calibrated on L10-as-line (a much easier bar).
# Against real market lines, the model rarely exceeds 0.65 — that's expected and fine.
# Daily cards are built by sorting on prob and picking the top N above these floors.
STAT_MIN_PROB = {
    'reb':     0.57,
    'ast':     0.57,
    'pra':     0.58,
    'reb_ast': 0.58,
    'pts':     0.57,
}

# PrizePicks stat_type strings → our internal stat keys
PP_STAT_MAP = {
    'Points':        'pts',
    'Rebounds':      'reb',
    'Assists':       'ast',
    'Pts+Rebs+Asts': 'pra',
    'Rebs+Asts':     'reb_ast',
    '3-PT Made':     'fg3m',
}

STATS = ['reb', 'ast', 'pra', 'reb_ast', 'pts']
MIN_MINUTES = 25   # raised from 20 — keeps out fringe/garbage-time players
DEF_WINDOW = 15

PP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Referer': 'https://app.prizepicks.com/',
    'Origin': 'https://app.prizepicks.com',
}


# ── PrizePicks integration ─────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    """Lowercase, strip accents, remove punctuation — for fuzzy name matching."""
    name = unicodedata.normalize('NFKD', name)
    name = ''.join(c for c in name if not unicodedata.combining(c))
    name = name.lower()
    name = name.replace("'", '').replace('.', '').replace('-', ' ')
    name = ' '.join(name.split())
    for suffix in (' jr', ' sr', ' ii', ' iii', ' iv'):
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
    return name


def fetch_prizepicks_lines() -> dict:
    """
    Fetch tonight's NBA standard lines from PrizePicks.
    Returns {(normalized_player_name, stat_key): line_score} or {} on failure.
    """
    try:
        r = requests.get(
            'https://api.prizepicks.com/projections?league_id=7&per_page=250',
            headers=PP_HEADERS, timeout=12,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        if 'data' not in data:
            return {}
    except Exception:
        return {}

    # Build player ID → display name
    player_map = {}
    for item in data.get('included', []):
        if item.get('type') == 'new_player':
            player_map[item['id']] = item['attributes'].get('display_name', '')

    lines = {}
    for proj in data.get('data', []):
        attr = proj.get('attributes', {})
        if attr.get('odds_type') != 'standard' or attr.get('status') != 'pre_game':
            continue
        stat_raw = attr.get('stat_type', '')
        stat_key = PP_STAT_MAP.get(stat_raw)
        if not stat_key:
            continue
        rels = proj.get('relationships', {})
        pid = rels.get('new_player', {}).get('data', {}).get('id', '')
        player_name = player_map.get(pid, '')
        if not player_name:
            continue
        key = (normalize_name(player_name), stat_key)
        lines[key] = float(attr['line_score'])

    return lines


# ── Injury report ─────────────────────────────────────────────────────────────

def fetch_injury_report() -> dict:
    """
    Fetch NBA injury statuses from ESPN.
    Returns {normalized_player_name: status_string}
    e.g. {'anthony edwards': 'Day-To-Day', 'giannis antetokounmpo': 'Out'}
    """
    try:
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries',
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=8,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
    except Exception:
        return {}

    injured = {}
    for team_entry in data.get('injuries', []):
        for inj in team_entry.get('injuries', []):
            status = inj.get('status', '')
            name = inj.get('athlete', {}).get('displayName', '')
            if name and status:
                injured[normalize_name(name)] = status
    return injured


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
    games = get_todays_games(target_date)
    if not games:
        print("  No games found for this date.")
        return []

    print(f"  Found {len(games)} game(s) on {target_date}")
    contexts = []
    for game in games:
        label = f"{game['away_abbr']} @ {game['home_abbr']}"
        print(f"    {label}...", end="", flush=True)
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
    features_df = features_df.copy()
    features_df['game_date'] = pd.to_datetime(features_df['game_date'])
    target_dt = pd.to_datetime(target_date)

    recent_df = (
        features_df[features_df['game_date'] < target_dt]
        .sort_values('game_date')
        .groupby('player_id')
        .last()
        .reset_index()
    )
    recent_map = recent_df.set_index('player_id').to_dict(orient='index')

    def_cache: dict = {}
    pace_cache: dict = {}
    rows = []

    for ctx in player_contexts:
        pid = ctx['player_id']
        if pid not in recent_map:
            continue
        row = dict(recent_map[pid])
        row['player_id'] = pid

        min_l10 = row.get('min_L10') or 0
        if min_l10 < MIN_MINUTES:
            continue

        last_game_date = row['game_date']
        days_rest = (target_dt - last_game_date).days
        row['days_rest'] = min(int(days_rest), 10)
        row['is_b2b'] = int(days_rest == 1)
        row['game_month'] = target_dt.month
        row['is_home'] = ctx['is_home']
        row['matchup'] = ctx['matchup']
        row['opp_abbr'] = ctx['opp_abbr']
        row['team_abbr'] = ctx['team_abbr']
        row['game_date'] = target_dt

        opp = ctx['opp_abbr']
        if opp not in def_cache:
            def_cache[opp] = get_opponent_def_stats(opp, target_date)
        row.update(def_cache[opp])

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


def predict_stat_raw(df: pd.DataFrame, stat: str) -> pd.Series:
    """
    Raw model prediction (no L10 blend) — used when comparing to real lines.
    The L10 blend was designed for comparing against L10 as a fake line;
    against a real sportsbook line, the raw model signal is more accurate.
    """
    components = COMPOSITE_STATS.get(stat)
    if components:
        models = {c: load_model(c) for c in components}
        valid = pd.Series(True, index=df.index)
        for m in models.values():
            cols = [c for c in m['feature_cols'] if c in df.columns]
            valid &= df[cols].notna().all(axis=1)
        preds = pd.Series(0.0, index=df.index)
        for m in models.values():
            cols = [c for c in m['feature_cols'] if c in df.columns]
            preds.loc[valid] += m['model'].predict(df.loc[valid, cols])
        preds[~valid] = np.nan
    else:
        m = load_model(stat)
        cols = [c for c in m['feature_cols'] if c in df.columns]
        valid = df[cols].notna().all(axis=1)
        preds = pd.Series(np.nan, index=df.index)
        preds.loc[valid] = m['model'].predict(df.loc[valid, cols])
    return preds


# ── Candidate generation ───────────────────────────────────────────────────────

def build_candidates(df: pd.DataFrame, pp_lines: dict,
                     injury_report: dict) -> pd.DataFrame:
    """
    Score every (player, stat) pair. Returns OVER and UNDER candidates.

    Filters:
      - Players listed as 'Out' in injury report are removed entirely.
      - Players with 'Day-To-Day' or 'Doubtful' are flagged but kept.
      - When pp_lines is populated: only surfaces picks with a real PP line.
      - Falls back to L10 baseline if pp_lines is empty.

    Returns a DataFrame with a 'direction' column: 'OVER' or 'UNDER'.
    """
    using_real_lines = bool(pp_lines)
    rows = []

    out_statuses = {'out', 'out (injury)', 'inactive', 'did not play'}
    warn_statuses = {'day-to-day', 'doubtful', 'questionable'}

    for stat in STATS:
        l10_col = f'{stat}_L10'
        std_col = f'{stat}_std_L10'
        if l10_col not in df.columns:
            continue

        min_p = STAT_MIN_PROB[stat]
        preds = predict_stat_raw(df, stat)

        for idx, row in df.iterrows():
            pred = preds.get(idx, np.nan)
            if pd.isna(pred):
                continue

            player_name = row.get('full_name', '')
            norm_name = normalize_name(player_name)

            # Injury filter
            inj_status = injury_report.get(norm_name, '')
            if any(s in inj_status.lower() for s in out_statuses):
                continue  # player is out — skip entirely
            inj_flag = '⚠️ ' if any(s in inj_status.lower() for s in warn_statuses) else ''

            pp_key = (norm_name, stat)
            if using_real_lines:
                if pp_key not in pp_lines:
                    continue
                line = pp_lines[pp_key]
            else:
                line = row.get(l10_col)
                if pd.isna(line) or float(line) == 0:
                    continue

            std_val = row.get(std_col, np.nan)
            std_float = float('nan') if pd.isna(std_val) else float(std_val)
            p_over = prob_over(float(pred), float(line), std_float)
            p_under = 1.0 - p_over

            base = {
                'player_id': row['player_id'],
                'player':    inj_flag + player_name,
                'game':      row.get('matchup', 'UNK'),
                'stat':      stat.upper(),
                'line':      round(float(line), 1),
                'pred':      round(float(pred), 1),
            }

            if p_over >= min_p:
                rows.append({**base, 'direction': 'OVER', 'prob': p_over})

            # UNDER pick: model thinks player will fall well short of the line
            if p_under >= min_p:
                rows.append({**base, 'direction': 'UNDER', 'prob': p_under})

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values('prob', ascending=False).reset_index(drop=True)


# ── Card builder ───────────────────────────────────────────────────────────────

def build_card(candidates: pd.DataFrame, n_legs: int, exclude_keys: set) -> list[dict]:
    """
    Greedy highest-confidence selection with constraints:
      - Skip (player_id, stat, direction) keys already in exclude_keys
      - One prop per player per card
      - Max 2 legs per game
      - Max 2 legs of the same stat type per card (diversity)
    """
    used_players: set = set()
    game_counts: dict = {}
    stat_counts: dict = {}
    card: list[dict] = []

    for _, row in candidates.iterrows():
        key = (row['player_id'], row['stat'], row['direction'])
        if key in exclude_keys:
            continue
        if row['player_id'] in used_players:
            continue
        if game_counts.get(row['game'], 0) >= 2:
            continue
        if stat_counts.get(row['stat'], 0) >= 2:
            continue

        card.append(row.to_dict())
        used_players.add(row['player_id'])
        game_counts[row['game']] = game_counts.get(row['game'], 0) + 1
        stat_counts[row['stat']] = stat_counts.get(row['stat'], 0) + 1

        if len(card) == n_legs:
            break
    return card


# ── Output ─────────────────────────────────────────────────────────────────────

def print_card(card: list[dict], label: str) -> None:
    w = 78
    print(f"\n  {label}")
    print(f"  {'Player':<28} {'Stat':<9} {'Dir':<5} {'PP Line':>7} {'Model':>7} {'P(dir)':>7}  Matchup")
    print(f"  {'-' * w}")
    for leg in card:
        direction = leg.get('direction', 'OVER')
        arrow = '▲' if direction == 'OVER' else '▼'
        print(f"  {leg['player']:<28} {leg['stat']:<9} {arrow} {direction:<4} {leg['line']:>7.1f} "
              f"{leg['pred']:>7.1f} {leg['prob']:>7.1%}  {leg['game']}")
    avg_prob = sum(leg['prob'] for leg in card) / len(card)
    print(f"  {'-' * w}")
    print(f"  {'Avg confidence':>44}  {avg_prob:>7.1%}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily SAFE Flex picks (live schedule + real PP lines)")
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

    # Fetch PrizePicks lines
    print(f"\n  Fetching PrizePicks lines...")
    pp_lines = fetch_prizepicks_lines()
    if pp_lines:
        print(f"  Found {len(pp_lines)} PrizePicks standard lines for tonight")
    else:
        print(f"  *** PrizePicks unavailable — falling back to L10 averages as lines ***")
        print(f"  *** Output will NOT be directly actionable on PrizePicks ***")

    # Fetch injury report
    print(f"  Fetching injury report from ESPN...")
    injury_report = fetch_injury_report()
    out_count = sum(1 for s in injury_report.values() if 'out' in s.lower())
    dtd_count = sum(1 for s in injury_report.values() if 'day' in s.lower() or 'doubt' in s.lower())
    print(f"  {out_count} players OUT  |  {dtd_count} Day-To-Day/Doubtful")

    print(f"\n  Loading features...")
    features_df = pd.read_parquet(feature_path)
    db_latest = pd.to_datetime(features_df['game_date']).max()
    target_dt = pd.to_datetime(args.date)
    staleness = (target_dt - db_latest).days
    if staleness > 60:
        print(f"\n  *** DATA WARNING: DB is {staleness} days stale (latest: {db_latest.date()}) ***")
        print(f"  *** Fix: python scripts/pull_season_data.py --season {CURRENT_SEASON} ***")

    print(f"\n  Fetching today's schedule from NBA.com...")
    player_contexts = get_todays_player_contexts(args.date, args.season)

    if not player_contexts:
        print("  No players found for this date.")
        sys.exit(0)

    print(f"\n  Building features for {len(player_contexts)} roster slots...")
    df_today = build_todays_features(features_df, player_contexts, args.date)

    if df_today.empty:
        print("  No players with sufficient game history.")
        sys.exit(0)

    print(f"  Players qualifying (min_L10 >= {MIN_MINUTES} min): {len(df_today)}")

    candidates = build_candidates(df_today, pp_lines, injury_report)

    if candidates.empty:
        if pp_lines:
            print("  No players matched PrizePicks lines above probability thresholds.")
        else:
            print("  No legs met minimum probability thresholds.")
        sys.exit(0)

    print(f"  Qualified legs: {len(candidates)}")

    card1 = build_card(candidates, n_legs=3, exclude_keys=set())
    used = {(leg['player_id'], leg['stat'], leg['direction']) for leg in card1}
    card2 = build_card(candidates, n_legs=5, exclude_keys=used)

    line_label = "PrizePicks lines" if pp_lines else "L10 avg lines (PP unavailable)"
    print(f"\n{'=' * 76}")
    print(f"  SAFE FLEX DAILY PICKS  —  {line_label}")
    print(f"{'=' * 76}")

    if card1:
        print_card(card1, "CARD 1  —  3-Leg SAFE Flex")
    else:
        print("\n  CARD 1: Not enough qualifying legs (need 3).")

    if card2:
        print_card(card2, "CARD 2  —  5-Leg SAFE Flex")
    else:
        print("\n  CARD 2: Not enough non-overlapping legs (need 5).")

    print(f"\n{'=' * 76}")
    if pp_lines:
        print(f"  PP Line = PrizePicks standard line  |  Model = raw model prediction")
    thresholds_str = "  |  ".join(f"{s.upper()} >= {int(v*100)}%" for s, v in STAT_MIN_PROB.items())
    print(f"  THRESHOLDS  {thresholds_str}")
    print(f"  RULES       0 shared legs  |  max 2/game  |  1/player  |  max 2/stat-type")
    print(f"  ⚠️  = Day-To-Day/Doubtful — verify lineup before betting")
    print(f"{'=' * 76}\n")


if __name__ == "__main__":
    main()
