"""
Playoff Backtest Harness — chronological replay of historical playoff games.

Key design decisions (all derived from 2024-25 data analysis):
  - Synthetic line = L10 × PLAYOFF_LINE_RATIO[stat] (approximates real market lines)
  - Playoff blend = 65% L10 + 35% model (canonical spec)
  - Per-stat elevation ratios (not PTS-only) — separate ratios for PTS/REB/AST/PRA/RA
  - Stdev multipliers EXPAND for all tiers (actual playoff variance > L10 variance)
  - No leakage: rolling windows computed from data strictly before each game
  - FG3M excluded entirely

Usage:
    from src.backtest.playoff_backtest import run_playoff_backtest
    results_df = run_playoff_backtest(seasons=['2024-25'])
"""

import sqlite3
import pickle
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

warnings.filterwarnings("ignore")

from config.settings import DB_PATH, PROCESSED_DIR
from src.playoffs.adjustments import PRED_MULT as PRED_MULT_PLAYOFF


# ── Constants ─────────────────────────────────────────────────────────────────

STATS = ['pts', 'reb', 'ast', 'pra', 'reb_ast']
COMPOSITE = {'pra': ['pts', 'reb', 'ast'], 'reb_ast': ['reb', 'ast']}

# Min minutes filter — consistent with daily picks
MIN_L10_MINUTES = 20.0
MIN_L10_GAMES   = 5     # need at least 5 reg-season games for a valid L10

# Playoff blend weight — canonical spec: 65% L10 / 35% model
PLAYOFF_BLEND_L10  = 0.65
PLAYOFF_BLEND_MODEL = 0.35

# Synthetic line ratios: playoff actual median / reg-season L10.
# These approximate what the market (PrizePicks) would set as the line.
# Derived from 2024-25 playoff data (n=1,034 player-games, min>=20).
PLAYOFF_LINE_RATIO = {
    'pts':     0.909,
    'reb':     0.986,
    'ast':     0.769,
    'pra':     0.939,
    'reb_ast': 0.899,
}

# Stdev expansion multipliers by tier.
# Actual playoff residual stdev vs L10 stdev (from 2024-25 analysis):
#   STAR:     PTS=1.078, REB=1.155, AST=1.162  → use 1.13
#   CORE:     PTS=1.064, REB=1.100, AST=1.027  → use 1.07
#   ROTATION: PTS=1.197, REB=1.051, AST=1.044  → use 1.10
# Previous system compressed (STAR 0.87, CORE 0.93) — wrong direction.
STD_MULT_PLAYOFF = {
    'STAR':     1.20,   # raised from 1.13 — PTS branch: fix overconfidence in 55-60% band
    'CORE':     1.07,
    'ROTATION': 1.10,
}

# Tier thresholds (matches adjustments.py)
TIER_THRESHOLDS = {'STAR': 34.0, 'CORE': 28.0}

# Game number in series: extracted from position 10 of game_id (1-indexed SQLite)
ROUND_LABELS = {'1': 'R1', '2': 'CSF', '3': 'CF', '4': 'Finals'}

# Defensive tier cutoffs (top/bottom 10 by DRTG)
DEF_TIER_TOP    = 10
DEF_TIER_BOTTOM = 10

# Star definition: regular-season min_L10 >= 32 OR top-3 usage proxy
STAR_MIN_L10 = 32.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tier(min_l10: float) -> str:
    if min_l10 >= TIER_THRESHOLDS['STAR']:
        return 'STAR'
    elif min_l10 >= TIER_THRESHOLDS['CORE']:
        return 'CORE'
    return 'ROTATION'


def _prob_over(pred: float, line: float, std: float) -> float:
    std = max(std, 0.5)
    z = (line - pred) / std
    return float(np.clip(1 - scipy_stats.norm.cdf(z), 0.01, 0.99))


def _game_num_in_series(game_id: str) -> int:
    """Extract game number in series from NBA game_id (position 10, 1-indexed)."""
    try:
        return int(game_id[9])
    except (IndexError, ValueError):
        return 0


def _round_from_game_id(game_id: str) -> str:
    try:
        return ROUND_LABELS.get(game_id[7], game_id[7])
    except IndexError:
        return 'UNK'


def _series_id(game_id: str) -> str:
    """First 9 chars identify the series."""
    return game_id[:9]


# ── Data loading ──────────────────────────────────────────────────────────────

def load_per_stat_elevation_ratios(
    conn: sqlite3.Connection,
    seasons: list,
    min_playoff_games: int = 5,
    min_reg_games: int = 20,
) -> dict:
    """
    Compute per-player, per-stat playoff elevation ratios using season-level aggregates.
    Returns {player_id: {stat: ratio}} where ratio > 1.0 = player elevates in playoffs.

    Uses only the seasons provided (excludes 2021-22 by default — incomplete data).
    Ratios are clipped to [0.75, 1.25] to prevent small-sample distortion.
    """
    placeholders = ','.join('?' * len(seasons))
    query = f"""
    WITH reg AS (
        SELECT player_id, season,
               AVG(pts) AS avg_pts, AVG(reb) AS avg_reb, AVG(ast) AS avg_ast,
               AVG(pts+reb+ast) AS avg_pra, AVG(reb+ast) AS avg_ra,
               COUNT(*) AS n
        FROM game_logs
        WHERE season_type='Regular Season' AND min>=10 AND season IN ({placeholders})
        GROUP BY player_id, season
        HAVING COUNT(*) >= {min_reg_games}
    ),
    po AS (
        SELECT player_id, season,
               AVG(pts) AS po_pts, AVG(reb) AS po_reb, AVG(ast) AS po_ast,
               AVG(pts+reb+ast) AS po_pra, AVG(reb+ast) AS po_ra,
               COUNT(*) AS n
        FROM game_logs
        WHERE season_type='Playoffs' AND min>=15 AND season IN ({placeholders})
        GROUP BY player_id, season
        HAVING COUNT(*) >= {min_playoff_games}
    )
    SELECT r.player_id,
           po.po_pts / NULLIF(r.avg_pts, 0) AS pts_ratio,
           po.po_reb / NULLIF(r.avg_reb, 0) AS reb_ratio,
           po.po_ast / NULLIF(r.avg_ast, 0) AS ast_ratio,
           po.po_pra / NULLIF(r.avg_pra, 0) AS pra_ratio,
           po.po_ra  / NULLIF(r.avg_ra,  0) AS ra_ratio,
           po.n AS po_games
    FROM reg r JOIN po USING (player_id, season)
    """
    params = list(seasons) + list(seasons)
    df = pd.read_sql(query, conn, params=params)

    if df.empty:
        return {}

    ratios = {}
    stat_cols = ['pts_ratio', 'reb_ratio', 'ast_ratio', 'pra_ratio', 'ra_ratio']
    stat_keys  = ['pts',       'reb',       'ast',       'pra',       'reb_ast']

    # If a player has multiple seasons, take recency-weighted average (newer = 2x weight)
    for pid, grp in df.groupby('player_id'):
        pid = int(pid)
        ratios[pid] = {}
        for col, key in zip(stat_cols, stat_keys):
            vals = grp[col].dropna()
            if vals.empty:
                continue
            # Weight: later rows get higher weight (assume sorted by season within group)
            weights = np.arange(1, len(vals) + 1, dtype=float)
            weights = weights / weights.sum()
            ratio = np.average(vals, weights=weights)
            ratio = float(np.clip(ratio, 0.75, 1.25))
            ratios[pid][key] = ratio

    return ratios


def load_opponent_def_tiers(conn: sqlite3.Connection, seasons: list) -> dict:
    """
    Build a {(team_abbr, season): 'top'|'mid'|'bot'} defensive tier map.
    Uses regular-season team_stats table DRTG.
    """
    placeholders = ','.join('?' * len(seasons))
    df = pd.read_sql(f"""
        SELECT team_abbr, season, def_rtg
        FROM team_stats
        WHERE season IN ({placeholders}) AND def_rtg IS NOT NULL
        ORDER BY season, def_rtg
    """, conn, params=seasons)

    if df.empty:
        return {}

    tiers = {}
    for season, grp in df.groupby('season'):
        grp = grp.sort_values('def_rtg').reset_index(drop=True)
        n = len(grp)
        for i, row in grp.iterrows():
            if i < DEF_TIER_TOP:
                tier = 'top'
            elif i >= n - DEF_TIER_BOTTOM:
                tier = 'bot'
            else:
                tier = 'mid'
            tiers[(row['team_abbr'], season)] = tier
    return tiers


# ── Feature retrieval ─────────────────────────────────────────────────────────

def get_player_reg_season_context(
    conn: sqlite3.Connection,
    player_id: int,
    before_date: str,
    stat: str,
) -> Optional[dict]:
    """
    Compute everything we'd know about a player strictly before tipoff:
      - L10 mean and stdev for the stat (from reg-season games)
      - min_L10, min_L3, low_min_count_L5
    Returns None if insufficient data.
    """
    # Stat column for composite stats
    if stat == 'pra':
        stat_expr = 'pts+reb+ast'
    elif stat == 'reb_ast':
        stat_expr = 'reb+ast'
    else:
        stat_expr = stat

    df = pd.read_sql(f"""
        SELECT {stat_expr} AS val, min
        FROM game_logs
        WHERE player_id=? AND season_type='Regular Season'
          AND game_date < ? AND min >= 10
        ORDER BY game_date DESC LIMIT 20
    """, conn, params=(player_id, before_date))

    if len(df) < MIN_L10_GAMES:
        return None

    l10 = df['val'].head(10)
    min10 = df['min'].head(10)
    min3  = df['min'].head(3)
    min5  = df['min'].head(5)

    return {
        'l10_mean':          float(l10.mean()),
        'l10_std':           float(l10.std()) if len(l10) > 1 else float(l10.mean() * 0.3),
        'min_L10':           float(min10.mean()),
        'min_L3':            float(min3.mean()),
        'low_min_count_L5':  int((min5 < 20).sum()),
        'n_reg_games':       len(df),
    }


def get_series_l3(
    conn: sqlite3.Connection,
    player_id: int,
    opp_abbr: str,
    season: str,
    before_date: str,
    stat: str,
) -> Optional[float]:
    """Return mean of last 3 games vs same opponent in this season's playoffs."""
    if stat == 'pra':
        expr = 'pts+reb+ast'
    elif stat == 'reb_ast':
        expr = 'reb+ast'
    else:
        expr = stat

    df = pd.read_sql(f"""
        SELECT {expr} AS val
        FROM game_logs
        WHERE player_id=? AND season_type='Playoffs' AND season=?
          AND opp_abbr=? AND game_date < ? AND min >= 10
        ORDER BY game_date DESC LIMIT 3
    """, conn, params=(player_id, season, opp_abbr, before_date))

    if df.empty:
        return None
    return float(df['val'].mean())


def get_rotation_tightening(
    conn: sqlite3.Connection,
    game_id: str,
    team_abbr: str,
    season: str,
) -> bool:
    """
    Return True if this team's rotation tightened vs reg-season average.
    Tightened = count of players with >=15 min in this game < reg-season avg.
    """
    # Players with >=15 min in this specific game
    game_players = pd.read_sql("""
        SELECT COUNT(DISTINCT player_id) AS cnt
        FROM game_logs
        WHERE game_id=? AND team_abbr=? AND min >= 15
    """, conn, params=(game_id, team_abbr))
    game_cnt = int(game_players['cnt'].iloc[0]) if not game_players.empty else 0

    # Reg-season average rotation size for this team
    reg_avg = pd.read_sql("""
        SELECT AVG(cnt) AS avg_cnt FROM (
            SELECT game_id, COUNT(DISTINCT player_id) AS cnt
            FROM game_logs
            WHERE team_abbr=? AND season=? AND season_type='Regular Season' AND min >= 15
            GROUP BY game_id
        )
    """, conn, params=(team_abbr, season))
    reg_cnt = float(reg_avg['avg_cnt'].iloc[0]) if not reg_avg.empty else 8.0

    return game_cnt < (reg_cnt - 1.5)


# ── Model loading ─────────────────────────────────────────────────────────────

_model_cache: dict = {}

def load_model(stat: str) -> Optional[dict]:
    if stat in _model_cache:
        return _model_cache[stat]
    path = PROCESSED_DIR / "models" / f"{stat}_model.pkl"
    if not path.exists():
        return None
    with open(path, 'rb') as f:
        m = pickle.load(f)
    _model_cache[stat] = m
    return m


def predict_blended(features_row: pd.Series, stat: str, l10_mean: float) -> Optional[float]:
    """
    65% L10 + 35% model output (canonical playoff spec).
    For composite stats: sum the component model predictions.
    Returns None if model is unavailable or features are missing.
    """
    components = COMPOSITE.get(stat)
    if components:
        total = 0.0
        for c in components:
            m = load_model(c)
            if m is None:
                return None
            cols = [f for f in m['feature_cols'] if f in features_row.index]
            if features_row[cols].isna().any():
                return None
            total += float(m['model'].predict(features_row[cols].values.reshape(1, -1))[0])
        raw_model = total
    else:
        m = load_model(stat)
        if m is None:
            return None
        cols = [f for f in m['feature_cols'] if f in features_row.index]
        if features_row[cols].isna().any():
            return None
        raw_model = float(m['model'].predict(features_row[cols].values.reshape(1, -1))[0])

    return PLAYOFF_BLEND_L10 * l10_mean + PLAYOFF_BLEND_MODEL * raw_model


def predict_l10_only(l10_mean: float) -> float:
    return l10_mean


def predict_model_only(features_row: pd.Series, stat: str) -> Optional[float]:
    """Raw model output, no blend."""
    components = COMPOSITE.get(stat)
    if components:
        total = 0.0
        for c in components:
            m = load_model(c)
            if m is None:
                return None
            cols = [f for f in m['feature_cols'] if f in features_row.index]
            if features_row[cols].isna().any():
                return None
            total += float(m['model'].predict(features_row[cols].values.reshape(1, -1))[0])
        return total
    else:
        m = load_model(stat)
        if m is None:
            return None
        cols = [f for f in m['feature_cols'] if f in features_row.index]
        if features_row[cols].isna().any():
            return None
        return float(m['model'].predict(features_row[cols].values.reshape(1, -1))[0])


# ── Suppression ───────────────────────────────────────────────────────────────

def should_suppress(ctx: dict) -> bool:
    """Suppress rotation players with strong DNP/minute-cut signals."""
    tier = _tier(ctx.get('min_L10', 28.0))
    if tier not in ('ROTATION',):
        return False
    low_min = int(ctx.get('low_min_count_L5', 0) or 0)
    if low_min >= 2:
        return True
    min_l3  = float(ctx.get('min_L3',  0) or 0)
    min_l10 = float(ctx.get('min_L10', 0) or 0)
    if min_l10 > 0 and min_l3 / min_l10 < 0.82:
        return True
    return False


# ── Core backtest engine ──────────────────────────────────────────────────────

def run_playoff_backtest(
    seasons: list = None,
    elev_train_seasons: list = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Replay all playoff games chronologically for the given seasons.

    Args:
        seasons: playoff seasons to evaluate (default: ['2024-25'])
        elev_train_seasons: seasons used to build elevation ratios
                            (default: all seasons except the eval season,
                             using only 2022-23 onward due to data quality)
        verbose: print progress

    Returns:
        DataFrame with one row per (player, game, stat), columns:
          season, round, game_in_series, series_id, game_date, player_id,
          player_name, team, opp, stat, l10_mean, l10_std,
          pred_blended, pred_l10_only, pred_model_only,
          line_synthetic, p_over, actual, hit,
          tier, is_star, def_tier, rotation_tightened,
          series_l3_used, suppressed, is_home, days_rest
    """
    if seasons is None:
        seasons = ['2024-25']

    if elev_train_seasons is None:
        # Use prior seasons for elevation ratios (avoid eval-season leakage)
        all_po = ['2022-23', '2023-24', '2024-25']
        elev_train_seasons = [s for s in all_po if s not in seasons]
        # If evaluating all seasons, use 2022-23 + 2023-24 for elevation
        if not elev_train_seasons:
            elev_train_seasons = ['2022-23', '2023-24']

    conn = sqlite3.connect(str(DB_PATH))

    if verbose:
        print(f"\n  Loading elevation ratios from: {elev_train_seasons}")
    elev_ratios = load_per_stat_elevation_ratios(conn, elev_train_seasons)
    if verbose:
        print(f"  Per-stat elevation ratios for {len(elev_ratios)} players")

    def_tiers = load_opponent_def_tiers(conn, seasons)
    if verbose:
        print(f"  Defensive tiers loaded for {len(def_tiers)} team-seasons")

    # Load features.parquet for model predictions (contains pre-computed rolling features)
    features_df = pd.read_parquet(PROCESSED_DIR / "features.parquet")
    features_df['game_date'] = pd.to_datetime(features_df['game_date'])

    # Load all playoff game rows for the eval seasons
    placeholders = ','.join('?' * len(seasons))
    playoff_games = pd.read_sql(f"""
        SELECT gl.player_id, gl.game_id, gl.game_date, gl.season,
               gl.matchup, gl.is_home, gl.team_abbr, gl.opp_abbr,
               gl.min, gl.pts, gl.reb, gl.ast,
               gl.pts+gl.reb+gl.ast AS pra,
               gl.reb+gl.ast AS reb_ast,
               p.full_name
        FROM game_logs gl
        JOIN players p USING (player_id)
        WHERE gl.season_type='Playoffs' AND gl.season IN ({placeholders})
        ORDER BY gl.game_date, gl.game_id, gl.player_id
    """, conn, params=seasons)

    if verbose:
        print(f"  Playoff game-rows loaded: {len(playoff_games):,}")

    # Index features.parquet by player_id for fast row lookup
    # We'll use the features row from the most recent reg-season game before each playoff game
    feat_index = (
        features_df[features_df['season_type'] == 'Regular Season']
        .sort_values('game_date')
        .groupby('player_id')
    )

    results = []
    skipped_min = 0
    skipped_no_l10 = 0
    skipped_suppressed = 0

    total_rows = len(playoff_games)
    if verbose:
        print(f"\n  Processing {total_rows:,} player-game rows...")

    # Track rotation tightening cache per game_id+team
    rotation_cache: dict = {}

    for i, pg in playoff_games.iterrows():
        pid = int(pg['player_id'])
        game_date = str(pg['game_date'])
        actual_min = float(pg['min'])
        game_id = str(pg['game_id'])
        season = str(pg['season'])
        opp = str(pg['opp_abbr'])
        team = str(pg['team_abbr'])

        # Get reg-season context features from features.parquet
        # Use the most recent reg-season features row before this date
        if pid in feat_index.groups:
            player_feats = feat_index.get_group(pid)
            prior_feats = player_feats[
                player_feats['game_date'] < pd.to_datetime(game_date)
            ]
            if prior_feats.empty:
                skipped_no_l10 += 1
                continue
            feat_row = prior_feats.iloc[-1]
        else:
            skipped_no_l10 += 1
            continue

        min_l10 = float(feat_row.get('min_L10', 0) or 0)
        if min_l10 < MIN_L10_MINUTES:
            skipped_min += 1
            continue

        tier = _tier(min_l10)
        is_star = int(min_l10 >= STAR_MIN_L10)

        # Build context dict for suppression check
        ctx = {
            'min_L10': min_l10,
            'min_L3':  float(feat_row.get('min_L3', min_l10) or min_l10),
            'low_min_count_L5': int(feat_row.get('low_min_count_L5', 0) or 0),
        }

        if should_suppress(ctx):
            skipped_suppressed += 1
            continue

        # Rotation tightening (cached per game+team)
        rt_key = (game_id, team)
        if rt_key not in rotation_cache:
            rotation_cache[rt_key] = get_rotation_tightening(conn, game_id, team, season)
        rotation_tightened = int(rotation_cache[rt_key])

        # Defensive tier of opponent
        def_tier = def_tiers.get((opp, season), 'mid')

        # Days rest
        last_game = prior_feats.iloc[-1]['game_date']
        days_rest = (pd.to_datetime(game_date) - pd.to_datetime(last_game)).days
        days_rest = min(int(days_rest), 10)

        # Game metadata
        game_num = _game_num_in_series(game_id)
        round_label = _round_from_game_id(game_id)
        series_id = _series_id(game_id)

        # Per-stat processing
        player_elev = elev_ratios.get(pid, {})

        for stat in STATS:
            actual = float(pg[stat])

            # L10 mean and std for this stat
            stat_l10_col = f'{stat}_L10'
            stat_std_col = f'{stat}_std_L10'

            if stat_l10_col not in feat_row.index or pd.isna(feat_row.get(stat_l10_col)):
                continue

            l10_mean = float(feat_row[stat_l10_col])
            if l10_mean <= 0:
                continue

            l10_std_raw = float(feat_row.get(stat_std_col, l10_mean * 0.3) or l10_mean * 0.3)
            if l10_std_raw <= 0:
                l10_std_raw = l10_mean * 0.3

            # Adjust stdev to reflect actual playoff variance (expand, not compress)
            adj_std = l10_std_raw * STD_MULT_PLAYOFF.get(tier, 1.10)

            # Blended prediction: 50/50 L10 + model
            pred_blended = predict_blended(feat_row, stat, l10_mean)
            pred_l10 = predict_l10_only(l10_mean)
            pred_model = predict_model_only(feat_row, stat)

            if pred_blended is None:
                pred_blended = l10_mean  # fallback to L10 if model unavailable

            # Apply tier-based prediction multiplier (per-stat, from adjustments.py)
            tier_pred_mult = PRED_MULT_PLAYOFF.get(tier, PRED_MULT_PLAYOFF['CORE']).get(stat, 1.0)
            pred_blended = pred_blended * tier_pred_mult

            # Apply per-stat elevation ratio (if available)
            stat_ratio = player_elev.get(stat, None)
            if stat_ratio is not None:
                pred_blended = pred_blended * stat_ratio

            # Synthetic line: L10 × playoff market ratio
            line = l10_mean * PLAYOFF_LINE_RATIO.get(stat, 1.0)

            # Probability of going over the synthetic line
            p_over = _prob_over(pred_blended, line, adj_std)

            # Series L3 (G2+ only)
            series_l3 = get_series_l3(conn, pid, opp, season, game_date, stat)
            series_l3_used = int(series_l3 is not None)

            results.append({
                'season':              season,
                'round':               round_label,
                'game_in_series':      game_num,
                'series_id':           series_id,
                'game_date':           game_date,
                'player_id':           pid,
                'player_name':         str(pg['full_name']),
                'team':                team,
                'opp':                 opp,
                'stat':                stat,
                'l10_mean':            round(l10_mean, 3),
                'l10_std':             round(l10_std_raw, 3),
                'adj_std':             round(adj_std, 3),
                'pred_blended':        round(pred_blended, 3),
                'pred_l10_only':       round(pred_l10, 3),
                'pred_model_only':     round(float(pred_model), 3) if pred_model is not None else None,
                'line_synthetic':      round(line, 3),
                'p_over':              round(p_over, 4),
                'actual':              actual,
                'hit':                 int(actual > line),
                'tier':                tier,
                'is_star':             is_star,
                'def_tier':            def_tier,
                'rotation_tightened':  rotation_tightened,
                'series_l3':           round(series_l3, 3) if series_l3 is not None else None,
                'series_l3_used':      series_l3_used,
                'suppressed':          0,
                'is_home':             int(pg['is_home']),
                'days_rest':           days_rest,
                'actual_min':          actual_min,
            })

    conn.close()

    df = pd.DataFrame(results)

    if verbose:
        print(f"\n  Skipped — low min_L10:      {skipped_min:,}")
        print(f"  Skipped — no features:      {skipped_no_l10:,}")
        print(f"  Skipped — suppressed:       {skipped_suppressed:,}")
        print(f"  Rows in backtest results:   {len(df):,}")

    return df
