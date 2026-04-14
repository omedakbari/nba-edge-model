"""
Playoff Adjustment Layer — applied on top of existing regular-season predictions.

Design: pure post-prediction adjustments. The regular-season model runs unchanged.
This module then adjusts:
  1. predicted stat value — for expected minute changes and pace effects
  2. standard deviation — recalibrated for playoff variance patterns

Changes from prior version (2026-04-13 rebuild):
  - Per-stat elevation ratios: PTS/REB/AST/PRA/RA computed separately
    (prior version used PTS-only ratio applied to all stats — wrong for AST/REB)
  - Stdev multipliers EXPANDED for all tiers, not compressed.
    Actual playoff residual stdev analysis (2024-25, n=983 active player-games):
      STAR:     PTS=1.08×, REB=1.16×, AST=1.16×  → use 1.13
      CORE:     PTS=1.06×, REB=1.10×, AST=1.03×  → use 1.07
      ROTATION: PTS=1.20×, REB=1.05×, AST=1.04×  → use 1.10
    Prior version compressed (STAR 0.87×, CORE 0.93×) — directionally wrong,
    caused overconfident picks that underperformed at high thresholds.
  - Tier multipliers updated to reflect actual per-stat underperformance.
  - Backtest shows AST OVER picks at P≥0.75 hit 68%; was 62.9% overall before.

Usage from generate_todays_picks.py:
    from src.playoffs.adjustments import (
        load_playoff_elevation_ratios,
        apply_playoff_adjustments,
        get_player_tier,
        TIER_LABELS,
    )

Tier classification (based on min_L10):
    STAR      >= 34 min  — extended usage, high coach trust
    CORE      28–33.9    — flat to slight increase
    ROTATION  25–27.9    — meaningful minute-cut risk
"""

import sqlite3
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Optional


# ── Tier thresholds ────────────────────────────────────────────────────────────

TIER_STAR     = 34.0
TIER_CORE     = 28.0
# ROTATION is anyone >= MIN_MINUTES (25) and < TIER_CORE

TIER_LABELS = {
    'STAR':     '★ Star',
    'CORE':     '◆ Core',
    'ROTATION': '● Rotation',
}


# ── Prediction multipliers per (tier, stat) ────────────────────────────────────
# Recalibrated from 2024-25 playoffs (n=1,034 player-games, min>=20).
# Key stats from median actual/L10 ratios:
#   PTS: STAR=0.937, all tiers overall=0.909
#   REB: STAR=1.000, all tiers overall=0.986 (essentially flat)
#   AST: STAR=~0.85, all tiers overall=0.769 (strong underperformance)
# Tier multipliers reflect PREDICTION DISCOUNT relative to reg-season L10 average.

PRED_MULT: dict[str, dict[str, float]] = {
    'STAR': {
        'pts':     0.94,   # was 0.97 — backtest: median actual/L10=0.937, residual +2.10 median
        'reb':     1.00,   # data: 48.7% over-L10 rate; nearly flat
        'ast':     0.92,   # data: 32.0% over-L10 rate; meaningful decline
        'fg3m':    0.94,   # excluded from picks but kept for completeness
        'pra':     0.97,   # unchanged — PRA addressed separately after PTS branch
        'reb_ast': 0.96,   # REB flat + AST down
    },
    'CORE': {
        'pts':     0.93,   # was 0.97 — PTS branch
        'reb':     0.99,
        'ast':     0.82,   # was 0.93 — AST branch: CORE actual/L10=0.769 right at line; need model edge to clear
        'fg3m':    0.95,
        'pra':     0.97,   # unchanged
        'reb_ast': 0.96,
    },
    'ROTATION': {
        'pts':     0.89,   # was 0.93 — PTS branch
        'reb':     0.95,
        'ast':     0.77,   # was 0.91 — AST branch: ROTATION actual/L10=0.556 vs line 0.769
        'fg3m':    0.92,
        'pra':     0.87,   # was 0.93 — PRA branch: actual/L10=0.691 vs line 0.939; 0.87 requires elev_ratio≥1.08 to clear line
        'reb_ast': 0.93,
    },
}

PRED_MULT['FRINGE'] = {s: 0.85 for s in ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']}


# ── Play-In prediction multipliers ────────────────────────────────────────────
# Play-In = seeds 7-10, single elimination, no series history, weaker opponents
# than full-playoff field. Discounts shallower than full playoffs:
#   • Stars: minimal discount — coach trust same, opponent quality lower
#   • Rotation: less compressed than deep series (coaches haven't locked rotations)
# NOTE: These are heuristic — no Play-In game log history in DB to calibrate against.
# Revisit after first Play-In season is ingested.
PLAY_IN_PRED_MULT: dict[str, dict[str, float]] = {
    'STAR': {
        'pts':     0.93,   # data: median actual/L10=0.913 (n=71); was 0.99 — too generous
        'reb':     1.00,   # data: median actual/L10=1.023 — within range, no change
        'ast':     0.96,   # data: median actual/L10=0.946 — within range, no change
        'fg3m':    0.97,
        'pra':     0.99,
        'reb_ast': 0.98,
    },
    'CORE': {
        'pts':     0.98,   # data: median actual/L10=0.968 — within range, no change
        'reb':     1.08,   # data: median actual/L10=1.111 (n=119); was 1.00 — direction wrong
        'ast':     0.88,   # data: median actual/L10=0.833 (n=119); was 0.96 — too generous
        'fg3m':    0.97,
        'pra':     0.98,
        'reb_ast': 0.98,
    },
    'ROTATION': {
        'pts':     0.92,   # data: median actual/L10=0.909 (n=143); was 0.95 — too generous
        'reb':     0.97,   # data: median actual/L10=1.000 — within range, no change
        'ast':     0.94,   # data: median actual/L10=0.909 — within range, no change
        'fg3m':    0.95,
        'pra':     0.95,
        'reb_ast': 0.95,
    },
}
PLAY_IN_PRED_MULT['FRINGE'] = {s: 0.88 for s in ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']}


# ── Std (variance) multipliers per tier ────────────────────────────────────────
# From 2024-25 data: playoff residual stdev > L10 stdev for ALL tiers.
# Previous version compressed variance (STAR 0.87×) — caused overconfident picks.
# Correct: EXPAND stdev for all tiers to reflect actual playoff uncertainty.

STD_MULT: dict[str, float] = {
    'STAR':     1.35,   # was 1.20 — STAR PTS ablation: raising STD disperses overconfident 65-70% picks,
                        #             improves STAR PTS P≥65% from 50.9%→54.3% (+3.5pp) and P≥70% overall
                        #             +0.9pp; zero spillover to non-PTS stats. Config D in ablation_star_pts.py
    'CORE':     1.07,   # unchanged
    'ROTATION': 1.10,   # unchanged
    'FRINGE':   1.20,
}

STD_MULT_UNSTABLE = 1.10   # applied on top for rotation players with 2+ low-min games in L5
                            # (reduced from 1.15 since we already expand baseline std)


# ── Play-In std multipliers ────────────────────────────────────────────────────
# Narrower expansion than full playoffs: single game, less series-specific fatigue,
# but still more uncertain than regular season (elimination pressure, unfamiliar matchup).
PLAY_IN_STD_MULT: dict[str, float] = {
    'STAR':     1.06,   # borderline — per-stat spread too wide (AST 0.974×, PTS 1.225×) to resolve cleanly; leave until more data
    'CORE':     1.08,   # data: avg empirical mult=1.085 (n=119); was 1.04 — too compressed
    'ROTATION': 1.15,   # data: avg empirical mult=1.174 (n=143); was 1.07 — REB(1.28×) and AST(1.18×) clearly underexpanded
    'FRINGE':   1.12,   # dead code — MIN_MINUTES=25 filter removes FRINGE before this is reached
}


# ── Elevation ratio caps ───────────────────────────────────────────────────────
# Tighter caps than before — per-stat ratios are more precise, less noise.
ELEV_RATIO_MIN = 0.80
ELEV_RATIO_MAX = 1.20


def get_player_tier(min_l10: float) -> str:
    """Classify player into playoff minute tier based on regular-season min_L10."""
    if min_l10 >= TIER_STAR:
        return 'STAR'
    elif min_l10 >= TIER_CORE:
        return 'CORE'
    else:
        return 'ROTATION'


# ── Per-stat elevation ratios (new in 2026-04-13 rebuild) ─────────────────────

def load_playoff_elevation_ratios(
    db_path: str | Path,
    min_playoff_games: int = 5,
) -> dict[int, float]:
    """
    LEGACY: Returns single PTS-based elevation ratio per player.
    Used by validate_playoffs.py. For production picks, use
    load_playoff_elevation_ratios_per_stat instead.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM game_logs WHERE season_type = 'Playoffs'"
        ).fetchone()[0]
        if count == 0:
            return {}

        query = """
        WITH per_min AS (
            SELECT
                player_id,
                season_type,
                SUM(pts)  AS total_pts,
                SUM(min)  AS total_min,
                COUNT(*)  AS games
            FROM game_logs
            WHERE min > 0
            GROUP BY player_id, season_type
        ),
        playoff AS (
            SELECT player_id, total_pts / total_min AS pts_pm, games
            FROM per_min
            WHERE season_type = 'Playoffs' AND games >= ?
        ),
        regseason AS (
            SELECT player_id, total_pts / total_min AS pts_pm
            FROM per_min
            WHERE season_type = 'Regular Season' AND total_min > 0
        )
        SELECT p.player_id,
               p.pts_pm / r.pts_pm AS elev_ratio,
               p.games             AS playoff_games
        FROM playoff p
        JOIN regseason r USING (player_id)
        WHERE r.pts_pm > 0
        """
        df = pd.read_sql(query, conn, params=(min_playoff_games,))
    finally:
        conn.close()

    if df.empty:
        return {}

    df['elev_ratio'] = df['elev_ratio'].clip(ELEV_RATIO_MIN, ELEV_RATIO_MAX)
    return dict(zip(df['player_id'].astype(int), df['elev_ratio']))


def load_playoff_elevation_ratios_per_stat(
    db_path: str | Path,
    min_playoff_games: int = 5,
    min_reg_games: int = 20,
    elev_seasons: list | None = None,
) -> dict[int, dict[str, float]]:
    """
    Per-player, per-stat playoff elevation ratios.

    Returns {player_id: {stat: ratio}} where:
      ratio > 1.0 = player historically produces more in playoffs than reg-season
      ratio < 1.0 = player historically under-produces

    Stats: 'pts', 'reb', 'ast', 'pra', 'reb_ast'

    Uses recency-weighting: more recent seasons count 2× older seasons.

    Args:
        db_path: Path to nba_edge.db
        min_playoff_games: min playoff games per player-season
        min_reg_games: min reg-season games per player-season
        elev_seasons: which seasons to include (default: 2022-23, 2023-24, 2024-25)
    """
    if elev_seasons is None:
        elev_seasons = ['2022-23', '2023-24', '2024-25']

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM game_logs WHERE season_type='Playoffs'"
        ).fetchone()[0]
        if count == 0:
            return {}

        placeholders = ','.join('?' * len(elev_seasons))
        query = f"""
        WITH reg AS (
            SELECT player_id, season,
                   AVG(pts) AS avg_pts, AVG(reb) AS avg_reb, AVG(ast) AS avg_ast,
                   AVG(pts+reb+ast) AS avg_pra, AVG(reb+ast) AS avg_ra,
                   COUNT(*) AS n
            FROM game_logs
            WHERE season_type='Regular Season' AND min>=10
              AND season IN ({placeholders})
            GROUP BY player_id, season
            HAVING COUNT(*) >= {min_reg_games}
        ),
        po AS (
            SELECT player_id, season,
                   AVG(pts) AS po_pts, AVG(reb) AS po_reb, AVG(ast) AS po_ast,
                   AVG(pts+reb+ast) AS po_pra, AVG(reb+ast) AS po_ra,
                   COUNT(*) AS n
            FROM game_logs
            WHERE season_type='Playoffs' AND min>=15
              AND season IN ({placeholders})
            GROUP BY player_id, season
            HAVING COUNT(*) >= {min_playoff_games}
        )
        SELECT r.player_id, r.season,
               po.po_pts / NULLIF(r.avg_pts, 0) AS pts_ratio,
               po.po_reb / NULLIF(r.avg_reb, 0) AS reb_ratio,
               po.po_ast / NULLIF(r.avg_ast, 0) AS ast_ratio,
               po.po_pra / NULLIF(r.avg_pra, 0) AS pra_ratio,
               po.po_ra  / NULLIF(r.avg_ra,  0) AS ra_ratio,
               po.n AS po_games
        FROM reg r JOIN po USING (player_id, season)
        """
        params = list(elev_seasons) + list(elev_seasons)
        df = pd.read_sql(query, conn, params=params)
    finally:
        conn.close()

    if df.empty:
        return {}

    stat_cols = ['pts_ratio', 'reb_ratio', 'ast_ratio', 'pra_ratio', 'ra_ratio']
    stat_keys  = ['pts',       'reb',       'ast',       'pra',       'reb_ast']

    ratios: dict = {}
    for pid, grp in df.groupby('player_id'):
        pid = int(pid)
        ratios[pid] = {}
        grp = grp.sort_values('season')  # oldest first
        for col, key in zip(stat_cols, stat_keys):
            vals = grp[col].dropna()
            if vals.empty:
                continue
            # Recency weighting: most recent season gets 2× weight
            n = len(vals)
            weights = np.ones(n)
            weights[-1] = 2.0  # latest season double-weighted
            weights = weights / weights.sum()
            ratio = float(np.average(vals.values, weights=weights))
            ratio = float(np.clip(ratio, ELEV_RATIO_MIN, ELEV_RATIO_MAX))
            ratios[pid][key] = ratio

    return ratios


def apply_playoff_adjustments(
    pred: float,
    std: float,
    stat: str,
    row: pd.Series,
    playoff_ratios: Optional[dict] = None,
    play_in: bool = False,
) -> tuple[float, float, str, float, float]:
    """
    Apply playoff (or Play-In) adjustment to a single (player, stat) prediction.

    Supports both legacy {player_id: float} and new {player_id: {stat: float}}
    formats for playoff_ratios.

    Args:
        pred: raw model prediction (blended with L10)
        std:  player's rolling std (pts_std_L10 etc.)
        stat: stat key ('pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast')
        row:  feature row for this player (must contain min_L10, low_min_count_L5)
        playoff_ratios: optional dict — either:
            {player_id: float}           (legacy, PTS-only)
            {player_id: {stat: float}}   (new, per-stat)
            Ignored when play_in=True (no Play-In history in DB).
        play_in: if True, use PLAY_IN_PRED_MULT / PLAY_IN_STD_MULT instead of
            full-playoff values, and skip elevation ratios.

    Returns:
        (adj_pred, adj_std, tier, pred_mult, std_mult)
    """
    min_l10 = float(row.get('min_L10', 28.0) or 28.0)
    tier = get_player_tier(min_l10)

    # Select parameter set based on mode
    pred_mult_table = PLAY_IN_PRED_MULT if play_in else PRED_MULT
    std_mult_table  = PLAY_IN_STD_MULT  if play_in else STD_MULT

    # 1. Prediction multiplier (tier base)
    tier_pred_mult = pred_mult_table.get(tier, pred_mult_table['CORE']).get(stat, 1.0)

    # 2. Historical playoff elevation (per-stat if available)
    # Skipped in Play-In mode: no Play-In game log history in DB.
    elev_ratio = 1.0
    if playoff_ratios and not play_in:
        pid = int(row.get('player_id', -1))
        if pid in playoff_ratios:
            pid_data = playoff_ratios[pid]
            if isinstance(pid_data, dict):
                # New per-stat format
                # Map stat key to ratio key (reb_ast → ra, others direct)
                ratio_key = 'ra' if stat == 'reb_ast' else stat
                elev_ratio = pid_data.get(ratio_key, pid_data.get(stat, 1.0))
            else:
                # Legacy PTS-only float
                elev_ratio = float(pid_data)
            elev_ratio = float(np.clip(elev_ratio, ELEV_RATIO_MIN, ELEV_RATIO_MAX))

    # Combined prediction multiplier
    pred_mult = tier_pred_mult * elev_ratio
    adj_pred = pred * pred_mult

    # 3. Std multiplier — EXPANDED for all tiers (corrected from prior compressed values)
    tier_std_mult = std_mult_table.get(tier, 1.10)
    low_min_count = int(row.get('low_min_count_L5', 0) or 0)
    unstable = (low_min_count >= 2) and (tier != 'STAR')
    std_mult = tier_std_mult * (STD_MULT_UNSTABLE if unstable else 1.0)

    adj_std = std * std_mult

    return adj_pred, adj_std, tier, pred_mult, std_mult


def should_suppress(row: pd.Series, tier: str) -> bool:
    """
    Return True if this play should be suppressed from playoff cards.

    Suppress when (ROTATION only):
      - >= 2 low-min games in last 5 (DNP risk)
      - min_L3 substantially below min_L10 (recent demotion)

    NOTE: Limiting suppression to ROTATION only. For STAR/CORE, low min_L3
    is typically regular-season load management, not a playoff demotion signal.
    """
    if tier not in ('ROTATION', 'FRINGE'):
        return False

    low_min_count = int(row.get('low_min_count_L5', 0) or 0)
    if low_min_count >= 2:
        return True

    min_l3  = float(row.get('min_L3',  0) or 0)
    min_l10 = float(row.get('min_L10', 0) or 0)
    if min_l10 > 0 and min_l3 / min_l10 < 0.82:
        return True

    return False


def playoff_context_note(tier: str, pred_mult: float, std_mult: float,
                          suppressed: bool, has_history: bool) -> str:
    """Return a short annotation string for display in picks output."""
    parts = []
    if tier == 'STAR':
        parts.append('★ Star')
    elif tier == 'CORE':
        parts.append('◆ Core')
    else:
        parts.append('● Rotation')

    if abs(pred_mult - 1.0) >= 0.02:
        parts.append(f'pred{(pred_mult - 1.0)*100:+.0f}%')

    if has_history:
        parts.append('hist✓')

    return ' | '.join(parts)
