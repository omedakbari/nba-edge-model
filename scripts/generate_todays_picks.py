"""
Live daily picks generator — v8.

Fetches real PrizePicks lines, today's NBA schedule, current rosters, and the
ESPN injury report. Outputs two SAFE Flex cards: 3-leg and 5-leg. Falls back
to L10-as-line mode with a warning if PrizePicks is unreachable.

Usage:
    python scripts/generate_todays_picks.py [--date 2026-04-04] [--season 2025-26]

Probability thresholds (against real PP lines, not L10 fake lines):
    All stats: P >= 57-58%  — any model signal above 57% is edge on a no-vig platform.
"""

import argparse
import json
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
from src.playoffs.adjustments import (
    load_playoff_elevation_ratios,
    load_playoff_elevation_ratios_per_stat,
    apply_playoff_adjustments,
    should_suppress,
    playoff_context_note,
    TIER_LABELS,
)


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
    'pra':     0.57,
    'reb_ast': 0.57,
    'pts':     0.57,
}

# Playoff-specific minimums — tighter thresholds derived from backtest.
# AST top-quintile (P≥0.75 vs synthetic line) hits 68%; REB non-monotonic → 0.60 floor.
# PTS: model discriminates at P≥0.65 against real PP lines.
STAT_MIN_PROB_PLAYOFFS = {
    'reb':     0.60,
    'ast':     0.62,
    'pra':     0.60,
    'reb_ast': 0.60,
    'pts':     0.60,
}

# Tier-aware playoff gate — applied on top of STAT_MIN_PROB_PLAYOFFS.
# Effective floor = max(stat_min, tier_min).
# Source: policy-gating ablation (4-season backtest, 19,445 player-game rows).
#   ROTATION at P≥55%: 54.8% HR across all stats — below SAFE Flex breakeven.
#   ROTATION at P≥65%: 60.1% HR, +5.3pp gain, −723 picks (10.9% volume reduction).
#   CORE at P≥55%: 61.6% HR — gating to 60% costs 885 picks for +1.0pp, not worth it.
#   STAR at P≥55%: 57.5% HR — flat below P≥70%, no benefit gating lower.
TIER_MIN_PROB_PLAYOFFS = {
    'STAR':     0.55,   # stat-level floor governs; tier gate adds nothing below P≥70%
    'CORE':     0.55,   # 61.6% at P≥55% is already strong; gating sacrifices good picks
    'ROTATION': 0.65,   # 54.8%→60.1% improvement; filters low-quality ROTATION picks
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
            'https://api.prizepicks.com/projections?league_id=7&per_page=500',
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
        raw_home = row.get('HOME_TEAM_ID')
        raw_away = row.get('VISITOR_TEAM_ID')
        if raw_home is None or raw_away is None:
            continue  # API returns incomplete rows for games far in the future
        home_id = int(raw_home)
        away_id = int(raw_away)
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
                           target_date: str,
                           is_playoffs: bool = False,
                           season: str | None = None) -> pd.DataFrame:
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

    # Pre-index playoff game logs for series context lookups (only needed in playoff mode).
    # Scoped to the current season so prior-year matchups against the same opponent don't
    # inflate series_game_num (e.g. LeBron vs DEN had 9 cross-season games — without the
    # season filter his series_game_num for game 1 of 2025-26 playoffs would be 10).
    if is_playoffs and 'season_type' in features_df.columns and season:
        playoff_logs = features_df[
            (features_df['season_type'] == 'Playoffs') &
            (features_df['season'] == season) &
            (features_df['game_date'] < target_dt)
        ].sort_values('game_date')
    else:
        playoff_logs = pd.DataFrame()

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

        # Playoff series context: compute series_game_num and series_L3 features
        # from actual prior games in this series, not the stale parquet row values.
        if is_playoffs and not playoff_logs.empty:
            series_games = playoff_logs[
                (playoff_logs['player_id'] == pid) &
                (playoff_logs['opp_abbr'] == opp)
            ]
            row['is_playoff'] = 1
            row['series_game_num'] = len(series_games) + 1
            for stat in ['pts', 'reb', 'ast']:
                col = f'{stat}_series_L3'
                if not series_games.empty and stat in series_games.columns:
                    row[col] = series_games[stat].tail(3).mean()
                else:
                    row[col] = float('nan')
            # pra = pts + reb + ast composite
            if not series_games.empty and all(c in series_games.columns for c in ['pts', 'reb', 'ast']):
                row['pra_series_L3'] = (
                    series_games[['pts', 'reb', 'ast']].sum(axis=1).tail(3).mean()
                )
            else:
                row['pra_series_L3'] = float('nan')
        elif is_playoffs:
            row['is_playoff'] = 1
            row['series_game_num'] = 1
            for col in ['pts_series_L3', 'reb_series_L3', 'ast_series_L3', 'pra_series_L3']:
                row[col] = float('nan')

        # NaN guard for series_L3 features — model requires them; fill with L10 when no
        # series context exists (Game 1, non-playoff mode, or first career playoff game).
        # series_game_num also defaults to 1 for non-playoff games.
        for col, l10_col in [('pts_series_L3', 'pts_L10'),
                              ('reb_series_L3', 'reb_L10'),
                              ('ast_series_L3', 'ast_L10'),
                              ('pra_series_L3', 'pra_L10')]:
            if pd.isna(row.get(col)):
                row[col] = row.get(l10_col, float('nan'))
        if pd.isna(row.get('series_game_num')):
            row['series_game_num'] = 1

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


def predict_stat_raw(df: pd.DataFrame, stat: str,
                     model_cache: dict | None = None) -> pd.Series:
    """
    Raw model prediction (no L10 blend) — used when comparing to real lines.
    model_cache: optional pre-loaded {stat: model_data} dict to avoid repeated
    disk reads when scoring all stats in a single run.
    """
    def _get(s):
        if model_cache and s in model_cache:
            return model_cache[s]
        return load_model(s)

    components = COMPOSITE_STATS.get(stat)
    if components:
        models = {c: _get(c) for c in components}
        valid = pd.Series(True, index=df.index)
        for m in models.values():
            cols = [c for c in m['feature_cols'] if c in df.columns]
            valid &= df[cols].notna().all(axis=1)
        preds = pd.Series(0.0, index=df.index)
        if valid.sum() > 0:
            for m in models.values():
                cols = [c for c in m['feature_cols'] if c in df.columns]
                preds.loc[valid] += m['model'].predict(df.loc[valid, cols])
        preds[~valid] = np.nan
    else:
        m = _get(stat)
        cols = [c for c in m['feature_cols'] if c in df.columns]
        valid = df[cols].notna().all(axis=1)
        preds = pd.Series(np.nan, index=df.index)
        if valid.sum() > 0:
            preds.loc[valid] = m['model'].predict(df.loc[valid, cols])
    return preds


# ── Candidate generation ───────────────────────────────────────────────────────

def build_candidates(df: pd.DataFrame, pp_lines: dict,
                     injury_report: dict,
                     is_playoffs: bool = False,
                     is_play_in: bool = False,
                     playoff_ratios: dict | None = None) -> pd.DataFrame:
    """
    Score every (player, stat) pair. Returns OVER and UNDER candidates.

    Filters:
      - Players listed as 'Out' in injury report are removed entirely.
      - Players with 'Day-To-Day' or 'Doubtful' are flagged but kept.
      - When pp_lines is populated: only surfaces picks with a real PP line.
      - Falls back to L10 baseline if pp_lines is empty.

    Playoff mode (is_playoffs=True):
      - Applies tier-based prediction and variance adjustments.
      - Suppresses high-risk rotation plays (DNP/minute-cut signals).
      - Labels each pick with its playoff tier and adjustment info.

    Returns a DataFrame with a 'direction' column: 'OVER' or 'UNDER'.
    """
    using_real_lines = bool(pp_lines)
    rows = []

    # Pre-load all models once — avoids repeated pickle reads (8 files per run)
    base_stats = list({c for stat in STATS for c in (COMPOSITE_STATS.get(stat) or [stat])})
    model_cache = {s: load_model(s) for s in base_stats}

    out_statuses = {'out', 'out (injury)', 'inactive', 'did not play'}
    warn_statuses = {'day-to-day', 'doubtful', 'questionable'}

    for stat in STATS:
        l10_col = f'{stat}_L10'
        std_col = f'{stat}_std_L10'
        if l10_col not in df.columns:
            continue

        min_p = STAT_MIN_PROB_PLAYOFFS[stat] if is_playoffs else STAT_MIN_PROB[stat]
        preds = predict_stat_raw(df, stat, model_cache=model_cache)

        for idx, row in df.iterrows():
            pred_raw = preds.get(idx, np.nan)
            l10_val = row.get(l10_col)

            if is_playoffs and not pd.isna(pred_raw) and not pd.isna(l10_val) and float(l10_val) > 0:
                # 50/50 L10 / model blend for playoff mode — mirrors backtest harness
                # and aligns with the validate_playoffs.py calibration (67% hit rate)
                pred = 0.50 * float(l10_val) + 0.50 * float(pred_raw)
            else:
                pred = pred_raw

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

            # ── Playoff adjustment layer ───────────────────────────────────────
            playoff_note = ''
            tier = None
            if is_playoffs:
                adj_pred, adj_std, tier, pred_mult, std_mult = apply_playoff_adjustments(
                    float(pred), std_float, stat, row, playoff_ratios,
                    play_in=is_play_in,
                )
                # Suppress high-risk rotation plays
                if should_suppress(row, tier):
                    continue
                has_history = bool(playoff_ratios and int(row.get('player_id', -1)) in playoff_ratios)
                playoff_note = playoff_context_note(tier, pred_mult, std_mult, False, has_history)
                pred = adj_pred
                std_float = adj_std
            # ──────────────────────────────────────────────────────────────────

            p_over = prob_over(float(pred), float(line), std_float)
            p_under = 1.0 - p_over

            # Tier-aware gate (playoffs only): raise min_p for ROTATION picks.
            # Effective threshold = max(stat-level floor, tier floor).
            if is_playoffs and tier is not None:
                min_p = max(min_p, TIER_MIN_PROB_PLAYOFFS.get(tier, 0.55))

            base = {
                'player_id':    row['player_id'],
                'player':       inj_flag + player_name,
                'player_name':  player_name,              # clean name, no flag
                'team':         row.get('team_abbr', ''),
                'opp':          row.get('opp_abbr', ''),
                'game':         row.get('matchup', 'UNK'),
                'stat':         stat.upper(),
                'stat_key':     stat,                     # lowercase for DB lookup
                'line':         round(float(line), 1),
                'pred':         round(float(pred), 1),
                'line_source':  'prizepicks' if using_real_lines else 'l10_avg',
                'injury_flag':  inj_status,
                'playoff_note': playoff_note,
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
    Greedy highest-confidence selection with correlation-aware constraints:
      - Skip (player_id, stat, direction) keys already in exclude_keys
      - One prop per player per card
      - Max 2 legs per game (limits correlated failure in blowouts/closeouts)
      - Max 2 legs of the same stat type per card (diversification)
      - Prefer different games over same game when confidence is close (within 2%)
        to reduce correlated failure risk across legs.
    """
    used_players: set = set()
    game_counts: dict = {}
    stat_counts: dict = {}
    card: list[dict] = []
    card_games: list = []  # ordered list of games added, for correlation tracking

    for _, row in candidates.iterrows():
        key = (row['player_id'], row['stat'], row['direction'])
        if key in exclude_keys:
            continue
        if row['player_id'] in used_players:
            continue

        game = row['game']
        game_cnt = game_counts.get(game, 0)
        if game_cnt >= 2:
            continue

        if stat_counts.get(row['stat'], 0) >= 2:
            continue

        # Soft correlation penalty: skip same-game if card already has 2+ legs
        # AND there's a different-game candidate within 3% probability
        # (only enforce when we have choices, i.e., card not yet desperate for legs)
        if game_cnt == 1 and len(card) >= 2:
            # Check if a different-game candidate is close in confidence
            game_threshold = row['prob'] + 0.03
            has_diff_game_alt = False
            for _, alt in candidates.iterrows():
                if alt['game'] != game and alt['game'] not in {r['game'] for r in card if r.get('game') == game}:
                    alt_key = (alt['player_id'], alt['stat'], alt['direction'])
                    if (alt_key not in exclude_keys and
                            alt['player_id'] not in used_players and
                            game_counts.get(alt['game'], 0) < 2 and
                            stat_counts.get(alt['stat'], 0) < 2 and
                            alt['prob'] >= game_threshold - 0.03):
                        has_diff_game_alt = True
                        break
            if has_diff_game_alt:
                continue  # defer this same-game pick, prefer diversity

        card.append(row.to_dict())
        used_players.add(row['player_id'])
        game_counts[game] = game_cnt + 1
        stat_counts[row['stat']] = stat_counts.get(row['stat'], 0) + 1
        card_games.append(game)

        if len(card) == n_legs:
            break

    # If we fell short due to the correlation penalty, fill remaining slots without it
    if len(card) < n_legs:
        for _, row in candidates.iterrows():
            if len(card) == n_legs:
                break
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

    return card


# ── Output ─────────────────────────────────────────────────────────────────────

def print_ranked_board(candidates: pd.DataFrame, top_n: int = 20,
                        is_playoffs: bool = False) -> None:
    """Print the full ranked prop board before combo selection."""
    if candidates.empty:
        return
    w = 84
    label = "RANKED PROP BOARD (all qualifying legs, sorted by confidence)"
    print(f"\n  {label}")
    if is_playoffs:
        print(f"  {'#':<3} {'Player':<26} {'Stat':<9} {'Dir':<5} {'Line':>6} {'Model':>6} {'P':>6}  Context")
    else:
        print(f"  {'#':<3} {'Player':<26} {'Stat':<9} {'Dir':<5} {'Line':>6} {'Model':>6} {'P':>6}  Game")
    print(f"  {'-' * w}")
    for rank, (_, row) in enumerate(candidates.head(top_n).iterrows(), 1):
        direction = row.get('direction', 'OVER')
        arrow = '▲' if direction == 'OVER' else '▼'
        context = row.get('playoff_note', '') or row['game']
        print(f"  {rank:<3} {row['player']:<26} {row['stat']:<9} {arrow}{direction:<4} "
              f"{row['line']:>6.1f} {row['pred']:>6.1f} {row['prob']:>6.1%}  {context}")
    if len(candidates) > top_n:
        print(f"  ... and {len(candidates) - top_n} more legs above minimum threshold")


def print_card(card: list[dict], label: str, is_playoffs: bool = False) -> None:
    w = 78
    print(f"\n  {label}")
    if is_playoffs:
        print(f"  {'Player':<28} {'Stat':<9} {'Dir':<5} {'PP Line':>7} {'Model':>7} {'P(dir)':>7}  Playoff Context")
    else:
        print(f"  {'Player':<28} {'Stat':<9} {'Dir':<5} {'PP Line':>7} {'Model':>7} {'P(dir)':>7}  Matchup")
    print(f"  {'-' * w}")
    for leg in card:
        direction = leg.get('direction', 'OVER')
        arrow = '▲' if direction == 'OVER' else '▼'
        context = leg.get('playoff_note', '') or leg['game']
        print(f"  {leg['player']:<28} {leg['stat']:<9} {arrow} {direction:<4} {leg['line']:>7.1f} "
              f"{leg['pred']:>7.1f} {leg['prob']:>7.1%}  {context}")
    avg_prob = sum(leg['prob'] for leg in card) / len(card)
    combo_edge = sum(leg['prob'] for leg in card) / len(card) - 0.5
    print(f"  {'-' * w}")
    print(f"  {'Avg confidence':>44}  {avg_prob:>7.1%}  (edge: {combo_edge:+.1%} vs 50%)")


# ── Main ───────────────────────────────────────────────────────────────────────

def save_picks(card1: list[dict], card2: list[dict], date_str: str,
               season: str, line_source: str, is_playoffs: bool = False) -> Path:
    """Serialize both cards to data/picks/YYYY-MM-DD.json."""
    picks_dir = Path(__file__).parent.parent / "data" / "picks"
    picks_dir.mkdir(parents=True, exist_ok=True)
    out_path = picks_dir / f"{date_str}.json"

    def _serialize_card(card: list[dict], label: str, n_legs: int) -> list[dict]:
        out = []
        for leg in card:
            out.append({
                'player_id':   int(leg['player_id']),
                'player_name': leg['player_name'],
                'team':        leg['team'],
                'opp':         leg['opp'],
                'stat':        leg['stat'],         # uppercase display label
                'stat_key':    leg['stat_key'],     # lowercase for DB lookup
                'direction':   leg['direction'],
                'line':        leg['line'],
                'model_pred':  leg['pred'],
                'prob':        round(leg['prob'], 4),
                'card':        label,
                'n_legs':      n_legs,
                'line_source': leg['line_source'],
                'injury_flag': leg['injury_flag'],
            })
        return out

    payload = {
        'date':         date_str,
        'season':       season,
        'generated_at': datetime.utcnow().isoformat() + 'Z',
        'line_source':  line_source,
        'is_playoffs':  is_playoffs,
        'picks':        _serialize_card(card1, 'card1', len(card1))
                      + _serialize_card(card2, 'card2', len(card2)),
    }

    with open(out_path, 'w') as f:
        json.dump(payload, f, indent=2)

    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily SAFE Flex picks (live schedule + real PP lines)")
    parser.add_argument('--date', default=date.today().isoformat(),
                        help='Game date YYYY-MM-DD (default: today)')
    parser.add_argument('--season', default=CURRENT_SEASON,
                        help=f'NBA season string (default: {CURRENT_SEASON})')
    parser.add_argument('--save', action='store_true',
                        help='Save picks to data/picks/YYYY-MM-DD.json for later grading')
    parser.add_argument('--playoffs', action='store_true',
                        help='Activate playoffs mode: applies tier-based prediction and variance adjustments')
    parser.add_argument('--play-in', action='store_true',
                        help='Activate Play-In mode: shallower discounts/narrower stdev than full playoffs, '
                             'no elevation ratios (no Play-In history in DB yet)')
    args = parser.parse_args()

    if args.play_in and args.playoffs:
        print("Error: --play-in and --playoffs are mutually exclusive.")
        sys.exit(1)

    feature_path = PROCESSED_DIR / "features.parquet"
    if not feature_path.exists():
        print("Error: features.parquet not found. Run: python -m src.features.pipeline")
        sys.exit(1)

    is_playoffs = args.playoffs or args.play_in   # both modes use the playoff adjustment path
    is_play_in  = args.play_in
    playoff_ratios: dict = {}

    print(f"\n  NBA Edge Model — Daily Picks")
    if is_play_in:
        mode_label = "PLAY-IN EDITION"
    elif is_playoffs:
        mode_label = "PLAYOFFS EDITION"
    else:
        mode_label = "Regular Season"
    print(f"  Date: {args.date}  |  Season: {args.season}  |  Mode: {mode_label}")

    if is_playoffs and not is_play_in:
        print(f"\n  Loading playoff history from DB (per-stat elevation ratios)...")
        playoff_ratios = load_playoff_elevation_ratios_per_stat(DB_PATH)
        if playoff_ratios:
            print(f"  Found per-stat elevation ratios for {len(playoff_ratios)} players")
        else:
            # Fall back to legacy PTS-only ratios
            playoff_ratios = load_playoff_elevation_ratios(DB_PATH)
            if playoff_ratios:
                print(f"  Found legacy PTS-based ratios for {len(playoff_ratios)} players")
            else:
                print(f"  No playoff history in DB — using tier-based adjustments only")
                print(f"  (Run 'python scripts/pull_season_data.py --playoffs' to add history)")
    elif is_play_in:
        print(f"  Play-In mode: elevation ratios disabled (no Play-In game log history in DB).")

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
    if staleness > 5:
        severity = "*** CRITICAL" if staleness > 14 else "*** WARNING"
        print(f"\n  {severity}: DB is {staleness} days stale (latest game: {db_latest.date()}) ***")
        print(f"  *** Rolling features are stale — run before trusting picks:")
        print(f"  *** python scripts/pull_season_data.py --update && python -m src.features.pipeline ***")

    print(f"\n  Fetching today's schedule from NBA.com...")
    player_contexts = get_todays_player_contexts(args.date, args.season)

    if not player_contexts:
        print("  No players found for this date.")
        sys.exit(0)

    print(f"\n  Building features for {len(player_contexts)} roster slots...")
    df_today = build_todays_features(features_df, player_contexts, args.date,
                                      is_playoffs=is_playoffs, season=args.season)

    if df_today.empty:
        print("  No players with sufficient game history.")
        sys.exit(0)

    print(f"  Players qualifying (min_L10 >= {MIN_MINUTES} min): {len(df_today)}")

    candidates = build_candidates(df_today, pp_lines, injury_report,
                                  is_playoffs=is_playoffs,
                                  is_play_in=is_play_in,
                                  playoff_ratios=playoff_ratios if is_playoffs else None)

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
    if is_play_in:
        playoffs_label = "  [PLAY-IN EDITION]"
    elif is_playoffs:
        playoffs_label = "  [PLAYOFFS EDITION]"
    else:
        playoffs_label = ""
    print(f"\n{'=' * 76}")
    print(f"  SAFE FLEX DAILY PICKS  —  {line_label}{playoffs_label}")
    print(f"{'=' * 76}")

    # Show ranked board first (all qualifying legs)
    if is_playoffs:
        print_ranked_board(candidates, top_n=25, is_playoffs=True)

    if card1:
        print_card(card1, "CARD 1  —  3-Leg SAFE Flex", is_playoffs=is_playoffs)
    else:
        print("\n  CARD 1: Not enough qualifying legs (need 3).")

    if card2:
        print_card(card2, "CARD 2  —  5-Leg SAFE Flex", is_playoffs=is_playoffs)
    else:
        print("\n  CARD 2: Not enough non-overlapping legs (need 5).")

    print(f"\n{'=' * 76}")
    if pp_lines:
        print(f"  PP Line = PrizePicks standard line  |  Model = blended prediction")
    thresholds = STAT_MIN_PROB_PLAYOFFS if is_playoffs else STAT_MIN_PROB
    thresholds_str = "  |  ".join(f"{s.upper()} >= {round(v*100)}%" for s, v in thresholds.items())
    print(f"  THRESHOLDS  {thresholds_str}")
    if is_playoffs:
        tier_str = "  |  ".join(f"{t} >= {round(v*100)}%" for t, v in TIER_MIN_PROB_PLAYOFFS.items())
        print(f"  TIER GATES  {tier_str}")
    print(f"  RULES       0 shared legs  |  max 2/game  |  1/player  |  max 2/stat-type")
    if is_playoffs:
        print(f"  ADJUSTMENTS per-stat elevation ratios | expanded stdev | suppression active")
    print(f"  ⚠️  = Day-To-Day/Doubtful — verify lineup before betting")
    print(f"{'=' * 76}\n")

    if args.save:
        line_source = 'prizepicks' if pp_lines else 'l10_avg'
        out_path = save_picks(card1 or [], card2 or [], args.date, args.season, line_source,
                              is_playoffs=is_playoffs)
        n_saved = len(card1 or []) + len(card2 or [])
        print(f"  Saved {n_saved} picks → {out_path}")
        print(f"  Grade after games: python scripts/log_pick_results.py --date {args.date}\n")


if __name__ == "__main__":
    main()
