"""
Playoff Adjustment Validation Script.

Runs a historical backtest comparing:
  (A) Baseline mode  — regular-season model, no adjustments
  (B) Playoffs mode  — same model + src/playoffs/adjustments.py layer

Uses actual playoff game logs from the DB (requires pull_season_data.py --playoffs).

Usage:
    python scripts/validate_playoffs.py
    python scripts/validate_playoffs.py --seasons 2024-25 2023-24
    python scripts/validate_playoffs.py --min-prob 0.57 --show-suppressed

The "line" in this backtest is the player's L10 regular-season average before
each playoff game (same fake-line approach used in the existing edge_finder.py).
This is not a real market line but shows directional accuracy and calibration.
"""

import argparse
import sqlite3
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from config.settings import DB_PATH
from src.playoffs.adjustments import (
    apply_playoff_adjustments,
    load_playoff_elevation_ratios,
    load_playoff_elevation_ratios_per_stat,
    should_suppress,
    get_player_tier,
)
from src.utils.probability import prob_over

# ── Constants ──────────────────────────────────────────────────────────────────

STATS = ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']
MIN_MINUTES_PLAYOFF = 20.0   # looser than daily picks (25) for richer sample
MIN_L10_GAMES = 5            # player must have ≥5 reg-season games before each playoff game

COMPOSITE_STATS = {
    'pra':     ['pts', 'reb', 'ast'],
    'reb_ast': ['reb', 'ast'],
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_playoff_games(conn: sqlite3.Connection, seasons: list[str],
                        min_minutes: float = None) -> pd.DataFrame:
    """Load playoff game rows with actual stat outcomes.

    Args:
        min_minutes: minimum minutes filter; defaults to MIN_MINUTES_PLAYOFF.
                     Pass 0 to load all appearances (needed for suppression audit).
    """
    if min_minutes is None:
        min_minutes = MIN_MINUTES_PLAYOFF
    placeholders = ','.join('?' * len(seasons))
    query = f"""
        SELECT gl.player_id, gl.game_id, gl.game_date, gl.season,
               gl.matchup, gl.is_home, gl.team_abbr, gl.opp_abbr,
               gl.min, gl.pts, gl.reb, gl.ast, gl.fg3m,
               gl.pts + gl.reb + gl.ast           AS pra,
               gl.reb + gl.ast                    AS reb_ast,
               p.full_name
        FROM game_logs gl
        JOIN players p USING (player_id)
        WHERE gl.season_type = 'Playoffs'
          AND gl.season IN ({placeholders})
          AND gl.min >= {min_minutes}
        ORDER BY gl.game_date
    """
    return pd.read_sql(query, conn, params=seasons)


def compute_reg_season_l10(conn: sqlite3.Connection, player_id: int,
                            before_date: str, stat: str) -> tuple[float, float]:
    """
    Compute regular-season L10 mean and std for a player before a given date.
    Returns (mean, std) or (nan, nan) if insufficient data.
    """
    # Handle composite stats
    if stat == 'pra':
        cols = 'pts + reb + ast'
    elif stat == 'reb_ast':
        cols = 'reb + ast'
    else:
        cols = stat

    query = f"""
        SELECT {cols} AS val
        FROM game_logs
        WHERE player_id = ?
          AND season_type = 'Regular Season'
          AND game_date < ?
          AND min >= 10
        ORDER BY game_date DESC
        LIMIT 10
    """
    df = pd.read_sql(query, conn, params=(player_id, before_date))
    if len(df) < MIN_L10_GAMES:
        return float('nan'), float('nan')
    return df['val'].mean(), df['val'].std()


def get_player_context_features(conn: sqlite3.Connection, player_id: int,
                                 before_date: str) -> dict:
    """
    Get the feature context we'd use for daily picks:
    min_L10, min_L3, low_min_count_L5 from regular-season games before this date.
    """
    query = """
        SELECT min
        FROM game_logs
        WHERE player_id = ?
          AND season_type = 'Regular Season'
          AND game_date < ?
        ORDER BY game_date DESC
        LIMIT 10
    """
    df = pd.read_sql(query, conn, params=(player_id, before_date))
    if df.empty:
        return {}
    return {
        'min_L10': df['min'].head(10).mean(),
        'min_L3':  df['min'].head(3).mean(),
        'low_min_count_L5': (df['min'].head(5) < 20).sum(),
        'player_id': player_id,
    }


# ── Core validation engine ─────────────────────────────────────────────────────

def run_validation(seasons: list[str], min_prob: float = 0.57,
                   playoff_ratios: dict | None = None,
                   show_suppressed: bool = False) -> None:

    conn = sqlite3.connect(str(DB_PATH))

    print(f"\n  Loading playoff games for seasons: {', '.join(seasons)}")
    playoff_df = load_playoff_games(conn, seasons)

    if playoff_df.empty:
        print("\n  ERROR: No playoff game logs found in DB.")
        print("  Run first: python scripts/pull_season_data.py --playoffs")
        conn.close()
        return

    print(f"  Playoff games loaded: {len(playoff_df):,} (min >= {MIN_MINUTES_PLAYOFF} min)")
    print(f"  Unique players: {playoff_df['player_id'].nunique()}")
    print(f"  Seasons: {sorted(playoff_df['season'].unique())}")

    # Build result rows
    results = []
    skipped_no_l10 = 0
    skipped_suppressed = 0

    for stat in STATS:
        stat_df = playoff_df[['player_id', 'game_id', 'game_date', 'season',
                               'matchup', 'is_home', 'full_name', stat]].copy()
        stat_df = stat_df.rename(columns={stat: 'actual'})

        for _, row in stat_df.iterrows():
            pid = int(row['player_id'])
            game_date = str(row['game_date'])
            actual = float(row['actual'])

            # Get pre-playoff regular-season L10 for this player
            l10_mean, l10_std = compute_reg_season_l10(conn, pid, game_date, stat)
            if np.isnan(l10_mean):
                skipped_no_l10 += 1
                continue

            ctx = get_player_context_features(conn, pid, game_date)
            if not ctx:
                skipped_no_l10 += 1
                continue

            ctx_series = pd.Series(ctx)
            tier = get_player_tier(float(ctx.get('min_L10', 0)))

            # Use L10 as the line (same approach as edge_finder.py backtest)
            line = l10_mean
            pred_base = l10_mean  # baseline: predict L10

            # ── Baseline (no adjustment) ─────────────────────────────────────
            p_base = prob_over(pred_base, line, l10_std)
            # For baseline the prediction equals the line, so P ~= 0.5 always
            # More meaningful baseline: use the actual over/under vs L10 line
            hit_actual = int(actual > line)

            # ── Playoffs mode ────────────────────────────────────────────────
            adj_pred, adj_std, tier, pred_mult, std_mult = apply_playoff_adjustments(
                pred_base, l10_std, stat, ctx_series, playoff_ratios,
            )
            p_playoffs = prob_over(adj_pred, line, adj_std)
            suppressed = should_suppress(ctx_series, tier)

            results.append({
                'player_id':    pid,
                'player_name':  row['full_name'],
                'game_date':    game_date,
                'season':       row['season'],
                'stat':         stat,
                'tier':         tier,
                'actual':       actual,
                'line':         line,
                'l10_std':      l10_std,
                'pred_mult':    pred_mult,
                'std_mult':     std_mult,
                'adj_pred':     adj_pred,
                'adj_std':      adj_std,
                'p_base':       p_base,
                'p_playoffs':   p_playoffs,
                'hit_actual':   hit_actual,   # 1 if actual > line (the L10 avg)
                'suppressed':   suppressed,
            })

    conn.close()
    print(f"  Skipped (insufficient L10): {skipped_no_l10:,}")

    if not results:
        print("\n  No results to analyze.")
        return

    df = pd.DataFrame(results)
    suppressed_df = df[df['suppressed']]
    active_df = df[~df['suppressed']]

    if show_suppressed:
        print(f"\n  Suppressed plays: {len(suppressed_df):,} ({len(suppressed_df)/len(df)*100:.1f}% of total)")

    # ── Section 1: Overall hit rate — does L10 predict playoff over? ──────────
    print(f"\n{'=' * 72}")
    print(f"  SECTION 1: Actual hit rate — did players exceed their L10 average?")
    print(f"{'=' * 72}")
    print(f"  (Line = player's own reg-season L10; actual > line = 'over')")
    print()

    for stat in STATS:
        s_df = active_df[active_df['stat'] == stat]
        if s_df.empty:
            continue
        hr = s_df['hit_actual'].mean()
        n  = len(s_df)
        print(f"  {stat.upper():<8} hit rate: {hr:.1%}  (n={n:,})  "
              f"[>50% = players outperform L10 in playoffs]")

    # ── Section 2: Tier breakdown of actual hit rates ─────────────────────────
    print(f"\n{'=' * 72}")
    print(f"  SECTION 2: Hit rate by player tier (all stats combined)")
    print(f"{'=' * 72}")

    for tier in ['STAR', 'CORE', 'ROTATION']:
        t_df = active_df[active_df['tier'] == tier]
        if t_df.empty:
            continue
        hr = t_df['hit_actual'].mean()
        n  = len(t_df)
        avg_pred_mult = t_df['pred_mult'].mean()
        avg_std_mult  = t_df['std_mult'].mean()
        print(f"  {tier:<10} hit rate: {hr:.1%}  n={n:,}  "
              f"pred_mult_applied={avg_pred_mult:.3f}  std_mult_applied={avg_std_mult:.3f}")

    # ── Section 3: Suppression check (full minutes range) ────────────────────
    print(f"\n{'=' * 72}")
    print(f"  SECTION 3: Suppression check — full minutes audit")
    print(f"  NOTE: Hit-rate comparison (sections 1-2) only sees min≥{MIN_MINUTES_PLAYOFF} games.")
    print(f"  This section loads ALL appearances (min≥0) to test suppression directly.")
    print(f"{'=' * 72}")

    conn_temp = sqlite3.connect(str(DB_PATH))
    all_po_df = load_playoff_games(conn_temp, seasons, min_minutes=0)

    suppress_rows = []
    for _, row in all_po_df.drop_duplicates(subset=['player_id', 'game_date']).iterrows():
        pid = int(row['player_id'])
        game_date = str(row['game_date'])
        ctx = get_player_context_features(conn_temp, pid, game_date)
        if not ctx:
            continue
        ctx_series = pd.Series(ctx)
        tier = get_player_tier(float(ctx.get('min_L10', 0)))
        suppressed = should_suppress(ctx_series, tier)
        suppress_rows.append({
            'player_id': pid,
            'game_date': game_date,
            'actual_min': float(row['min']),
            'tier': tier,
            'suppressed': suppressed,
        })
    conn_temp.close()

    if suppress_rows:
        supp_df = pd.DataFrame(suppress_rows)
        for label, mask in [('Active (not suppressed)', ~supp_df['suppressed']),
                             ('Suppressed',              supp_df['suppressed'])]:
            sub = supp_df[mask]
            if sub.empty:
                continue
            n = len(sub)
            pct_low  = (sub['actual_min'] < 20).mean()
            pct_dnp  = (sub['actual_min'] < 5).mean()
            avg_min  = sub['actual_min'].mean()
            print(f"\n  {label}  (n={n:,})")
            print(f"    avg actual minutes : {avg_min:.1f}")
            print(f"    % with < 20 min    : {pct_low:.1%}")
            print(f"    % with <  5 min    : {pct_dnp:.1%}")

        s = supp_df[supp_df['suppressed']]
        a = supp_df[~supp_df['suppressed']]
        if not s.empty and not a.empty:
            diff = s['actual_min'].mean() - a['actual_min'].mean()
            pct_diff = (s['actual_min'] < 20).mean() - (a['actual_min'] < 20).mean()
            verdict = (
                "✓ GOOD — suppressed players get meaningfully fewer minutes"
                if s['actual_min'].mean() < a['actual_min'].mean() - 3
                else "✗ WEAK — suppressed players play similar minutes to active ones"
            )
            print(f"\n  Minute gap (suppressed vs active): {diff:+.1f} min  {verdict}")
            print(f"  Extra <20-min rate for suppressed : {pct_diff:+.1%}")
    else:
        print(f"  No suppression data available.")

    # ── Section 4: Calibration — P(over) vs actual hit rate ──────────────────
    print(f"\n{'=' * 72}")
    print(f"  SECTION 4: Calibration — baseline vs playoffs mode")
    print(f"  (Does higher model confidence correspond to higher actual hit rate?)")
    print(f"{'=' * 72}")
    print()

    thresholds = [0.55, 0.57, 0.60, 0.63, 0.65]
    print(f"  {'Threshold':>10}  {'Base N':>7}  {'Base HR':>8}  {'PO N':>7}  {'PO HR':>8}  {'Delta':>7}")
    print(f"  {'-'*60}")

    for thresh in thresholds:
        base_above = active_df[active_df['p_base'] >= thresh]
        po_above   = active_df[active_df['p_playoffs'] >= thresh]
        if len(base_above) == 0 and len(po_above) == 0:
            continue
        base_hr = base_above['hit_actual'].mean() if len(base_above) > 0 else float('nan')
        po_hr   = po_above['hit_actual'].mean()   if len(po_above)   > 0 else float('nan')
        delta   = po_hr - base_hr if not np.isnan(base_hr) and not np.isnan(po_hr) else float('nan')
        base_n  = len(base_above)
        po_n    = len(po_above)

        delta_str = f"{delta:+.1%}" if not np.isnan(delta) else "  N/A"
        base_hr_s = f"{base_hr:.1%}" if not np.isnan(base_hr) else "  N/A"
        po_hr_s   = f"{po_hr:.1%}"   if not np.isnan(po_hr)   else "  N/A"
        print(f"  {thresh:.0%}         {base_n:>7,}  {base_hr_s:>8}  {po_n:>7,}  {po_hr_s:>8}  {delta_str:>7}")

    # ── Section 5: Multiplier calibration check ───────────────────────────────
    print(f"\n{'=' * 72}")
    print(f"  SECTION 5: Are the tier multipliers directionally correct?")
    print(f"  (Compare applied pred_mult direction vs actual over rate)")
    print(f"{'=' * 72}")
    print()
    print(f"  If Stars truly outperform L10 in playoffs, their hit rate should be > 50%.")
    print(f"  If Rotation players truly underperform, their hit rate should be < 50%.")
    print()

    for tier in ['STAR', 'CORE', 'ROTATION']:
        t_df = active_df[active_df['tier'] == tier]
        if t_df.empty:
            continue
        hr = t_df['hit_actual'].mean()
        avg_mult = t_df['pred_mult'].mean()
        direction_correct = (avg_mult > 1.0 and hr > 0.50) or (avg_mult < 1.0 and hr < 0.50)
        verdict = "✓ Direction correct" if direction_correct else "✗ Direction wrong"
        print(f"  {tier:<10} applied mult: {avg_mult:.3f}  actual HR: {hr:.1%}  {verdict}")

    # ── Section 6: Stat-level multiplier check ────────────────────────────────
    print(f"\n{'=' * 72}")
    print(f"  SECTION 6: Stat-level breakdown — Stars only")
    print(f"{'=' * 72}")
    print()

    star_df = active_df[active_df['tier'] == 'STAR']
    if not star_df.empty:
        print(f"  {'Stat':<8} {'Applied mult':>12}  {'Actual HR':>10}  {'N':>6}")
        print(f"  {'-' * 42}")
        for stat in STATS:
            s_df = star_df[star_df['stat'] == stat]
            if s_df.empty:
                continue
            hr = s_df['hit_actual'].mean()
            mult = s_df['pred_mult'].mean()
            n = len(s_df)
            print(f"  {stat.upper():<8} {mult:>12.3f}  {hr:>10.1%}  {n:>6,}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print(f"  SUMMARY")
    print(f"{'=' * 72}")

    overall_hr = active_df['hit_actual'].mean()
    star_hr    = active_df[active_df['tier'] == 'STAR']['hit_actual'].mean()
    core_hr    = active_df[active_df['tier'] == 'CORE']['hit_actual'].mean()
    rot_hr     = active_df[active_df['tier'] == 'ROTATION']['hit_actual'].mean()

    print(f"  Overall hit rate (active plays): {overall_hr:.1%}")
    print(f"  Star hit rate:                   {star_hr:.1%}")
    print(f"  Core hit rate:                   {core_hr:.1%}")
    print(f"  Rotation hit rate:               {rot_hr:.1%}")

    if playoff_ratios:
        players_with_history = active_df['player_id'].isin(playoff_ratios).sum()
        print(f"\n  Players with historical elevation ratio: {players_with_history:,} / {len(active_df):,} rows")
    else:
        print(f"\n  No historical elevation ratios loaded (no playoff DB data yet).")

    print()
    print(f"  Calibration note: the 'line' here is each player's own reg-season L10")
    print(f"  average — not a real market line. Treat as directional signal only.")
    print(f"  For market calibration, grade real picks after actual playoff games.")
    print()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Validate playoff adjustment layer vs baseline")
    parser.add_argument('--seasons', nargs='+', default=['2024-25', '2023-24', '2022-23'],
                        help='Playoff seasons to validate (default: last 3)')
    parser.add_argument('--min-prob', type=float, default=0.57,
                        help='Minimum probability threshold for calibration buckets')
    parser.add_argument('--show-suppressed', action='store_true',
                        help='Include suppressed plays in output')
    args = parser.parse_args()

    print(f"\n{'=' * 72}")
    print(f"  NBA EDGE MODEL — PLAYOFF VALIDATION")
    print(f"{'=' * 72}")

    print(f"\n  Loading per-stat playoff elevation ratios from DB...")
    playoff_ratios = load_playoff_elevation_ratios_per_stat(DB_PATH)
    if playoff_ratios:
        print(f"  Found per-stat elevation ratios for {len(playoff_ratios)} players")
    else:
        # Fall back to legacy PTS-only ratios
        playoff_ratios = load_playoff_elevation_ratios(DB_PATH)
        if playoff_ratios:
            print(f"  Found legacy PTS-based ratios for {len(playoff_ratios)} players")
        else:
            print(f"  No playoff history yet — run: python scripts/pull_season_data.py --playoffs")

    run_validation(
        seasons=args.seasons,
        min_prob=args.min_prob,
        playoff_ratios=playoff_ratios,
        show_suppressed=args.show_suppressed,
    )


if __name__ == "__main__":
    main()
