"""
Log and grade NBA team spread/ML pick results.

After games finish, fetches final scores from ESPN and grades the picks
saved by generate_todays_team_picks.py.

Grading rules:
  Spread: home_margin > market_spread → home covered (standard convention:
          market_spread = -5.5 means home needed to win by 5.5+)
          Grade is from the perspective of the pick (home or away side).
  ML:     pick the winning team → win.
  Pushes: if margin == |market_spread| exactly, grade as PUSH.

Outputs:
  Console summary: record, ATS%, ML ROI, calibration
  Appends to data/team_picks/results_log.csv

Usage:
    # Grade yesterday's picks
    python scripts/log_team_results.py --date 2026-04-14

    # Grade all saved pick files
    python scripts/log_team_results.py --all

    # Summary report across all graded games
    python scripts/log_team_results.py --report
"""

import argparse
import json
import sys
import warnings
from pathlib import Path
from datetime import date, datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

import pandas as pd

from src.data.espn_odds import fetch_odds, moneyline_to_implied_prob

PICKS_DIR    = Path(__file__).parent.parent / 'data' / 'team_picks'
RESULTS_LOG  = PICKS_DIR / 'results_log.csv'

RESULT_COLS = [
    'grade_date', 'game_date', 'game_id', 'home', 'away',
    'tier',
    'spread_pick_side', 'spread_pick_team', 'market_spread',
    'model_spread', 'actual_margin', 'spread_result',    # WIN/LOSS/PUSH/NO_PICK/NO_LINE
    'ml_pick_side',    'ml_pick_team',    'home_ml', 'away_ml',
    'actual_winner',   'ml_result',                       # WIN/LOSS/NO_PICK
    'model_wp', 'home_implied', 'away_implied',
    'sp_edge', 'home_ml_edge',
    'is_playoffs',
]


# ── Grading ──────────────────────────────────────────────────────────────────

def grade_spread(pick_side: str | None, market_spread: float | None, actual_margin: float) -> str:
    """
    Grade a spread pick.
    market_spread: home-team standard convention (negative = home favored)
    actual_margin: home_score - away_score (positive = home won)
    pick_side: 'home' | 'away' | None
    """
    if pick_side is None:
        return 'NO_PICK'
    if market_spread is None:
        return 'NO_LINE'

    # From home perspective: home covers if actual_margin > -market_spread
    # i.e. market says CHA -5.5 → market_spread = -5.5 → home needs to win by 5.5+
    home_cover_line = -market_spread   # +5.5 → home must beat this to cover

    if actual_margin > home_cover_line:
        home_covered = True
    elif actual_margin < home_cover_line:
        home_covered = False
    else:
        return 'PUSH'

    if pick_side == 'home':
        return 'WIN' if home_covered else 'LOSS'
    else:
        return 'WIN' if not home_covered else 'LOSS'


def grade_ml(pick_side: str | None, actual_margin: float) -> str:
    """Grade ML pick. actual_margin > 0 → home won."""
    if pick_side is None:
        return 'NO_PICK'
    home_won = actual_margin > 0
    if actual_margin == 0:
        return 'PUSH'   # overtime tie? rare but handle
    if pick_side == 'home':
        return 'WIN' if home_won else 'LOSS'
    else:
        return 'WIN' if not home_won else 'LOSS'


def ml_payout(ml: int | None, result: str) -> float:
    """
    Compute P&L for a $100 unit bet on this ML result.
    WIN on +150 → +150.  WIN on -200 → +50.  LOSS → -100.  PUSH → 0.
    """
    if result == 'NO_PICK' or ml is None:
        return 0.0
    if result == 'PUSH':
        return 0.0
    if result == 'LOSS':
        return -100.0
    # WIN
    ml = int(ml)
    if ml > 0:
        return float(ml)
    else:
        return round(10000.0 / abs(ml), 2)


# ── Fetch results ─────────────────────────────────────────────────────────────

def fetch_final_scores(game_date: str) -> dict[str, dict]:
    """
    Return {game_id: {'home_score': int, 'away_score': int, 'status': str}}
    for all final games on game_date.
    """
    games = fetch_odds(game_date)
    result = {}
    for g in games:
        if g['status'] == 'final' and g['home_score'] is not None and g['away_score'] is not None:
            result[g['game_id']] = {
                'home_score':  g['home_score'],
                'away_score':  g['away_score'],
                'actual_margin': g['home_score'] - g['away_score'],
                'status': g['status'],
            }
    return result


# ── Grade one day ─────────────────────────────────────────────────────────────

def grade_date(game_date: str) -> list[dict] | None:
    """
    Load picks for game_date, fetch final scores, grade each game.
    Returns list of result dicts (one per game in the picks file).
    Returns None if picks file not found or no final scores yet.
    """
    picks_path = PICKS_DIR / f"{game_date}.json"
    if not picks_path.exists():
        print(f"  No picks file found for {game_date}: {picks_path}")
        return None

    with open(picks_path) as f:
        saved = json.load(f)

    picks = saved.get('picks', [])
    if not picks:
        print(f"  No picks found in {picks_path}")
        return []

    print(f"  Fetching final scores for {game_date}...")
    scores = fetch_final_scores(game_date)
    if not scores:
        print(f"  ⚠️  No final scores available for {game_date}. Games may not have finished yet.")
        return None

    today = date.today().isoformat()
    results = []

    for pick in picks:
        gid = pick.get('game_id', '')
        score = scores.get(gid)
        if score is None:
            # Game not final (in progress or no data)
            actual_margin = None
        else:
            actual_margin = score['actual_margin']

        sp_result = grade_spread(
            pick.get('spread_pick_side'),
            pick.get('market_spread'),
            actual_margin if actual_margin is not None else 0,
        ) if actual_margin is not None else 'PENDING'

        ml_result = grade_ml(
            pick.get('ml_pick_side'),
            actual_margin if actual_margin is not None else 0,
        ) if actual_margin is not None else 'PENDING'

        # Determine which ML we bet and compute payout
        ml_pick_side = pick.get('ml_pick_side')
        ml_odds = pick.get('home_ml') if ml_pick_side == 'home' else pick.get('away_ml')
        ml_pl   = ml_payout(ml_odds, ml_result) if ml_result not in ('PENDING', 'NO_PICK') else 0.0

        results.append({
            'grade_date':       today,
            'game_date':        pick.get('game_date', game_date),
            'game_id':          gid,
            'home':             pick.get('home', ''),
            'away':             pick.get('away', ''),
            'tier':             pick.get('tier', ''),
            'spread_pick_side': pick.get('spread_pick_side'),
            'spread_pick_team': pick.get('spread_pick_team'),
            'market_spread':    pick.get('market_spread'),
            'model_spread':     pick.get('model_spread'),
            'actual_margin':    actual_margin,
            'spread_result':    sp_result,
            'ml_pick_side':     ml_pick_side,
            'ml_pick_team':     pick.get('ml_pick_team'),
            'home_ml':          pick.get('home_ml'),
            'away_ml':          pick.get('away_ml'),
            'actual_winner':    ('home' if actual_margin and actual_margin > 0 else
                                 'away' if actual_margin and actual_margin < 0 else
                                 'push' if actual_margin == 0 else None),
            'ml_result':        ml_result,
            'ml_pl':            ml_pl,
            'model_wp':         pick.get('model_wp'),
            'home_implied':     pick.get('home_implied'),
            'away_implied':     pick.get('away_implied'),
            'sp_edge':          pick.get('sp_edge'),
            'home_ml_edge':     pick.get('home_ml_edge'),
            'is_playoffs':      pick.get('is_playoffs', False),
        })

    return results


# ── Persist & report ──────────────────────────────────────────────────────────

def append_results(results: list[dict]) -> None:
    """Append graded results to the running CSV log (deduplicates by game_id)."""
    PICKS_DIR.mkdir(parents=True, exist_ok=True)
    new_df = pd.DataFrame(results)

    if RESULTS_LOG.exists():
        existing = pd.read_csv(RESULTS_LOG, dtype=str)
        # Drop any previously logged rows for the same game_ids (re-grade idempotency)
        existing = existing[~existing['game_id'].isin(new_df['game_id'].astype(str))]
        combined = pd.concat([existing, new_df.astype(str)], ignore_index=True)
    else:
        combined = new_df.astype(str)

    combined.to_csv(RESULTS_LOG, index=False)
    print(f"  Results saved/updated in {RESULTS_LOG}")


def print_day_summary(results: list[dict], game_date: str) -> None:
    """Print a compact summary for one day's picks."""
    picks = [r for r in results if r['spread_result'] not in ('NO_PICK', 'PENDING', 'NO_LINE') or
                                    r['ml_result'] not in ('NO_PICK', 'PENDING')]
    if not picks:
        print(f"  No picks to grade for {game_date}")
        return

    sp_results = [r for r in results if r['spread_result'] not in ('NO_PICK', 'PENDING', 'NO_LINE')]
    ml_results = [r for r in results if r['ml_result'] not in ('NO_PICK', 'PENDING')]

    print(f"\n  {'─'*60}")
    print(f"  RESULTS FOR {game_date}")
    print(f"  {'─'*60}")

    for r in results:
        if r['actual_margin'] is None:
            status = 'PENDING'
        else:
            margin_str = f"margin {r['actual_margin']:+d}"
            status = margin_str

        sp_str = f"ATS: {r['spread_result']}" if r['spread_result'] != 'NO_PICK' else "ATS: —"
        ml_str = f"ML: {r['ml_result']}" if r['ml_result'] != 'NO_PICK' else "ML: —"
        tier   = r.get('tier', 'NONE')
        game_s = f"{r['away']} @ {r['home']}"
        print(f"  {game_s:<20} [{tier:<7}]  {sp_str:<12}  {ml_str:<12}  {status}")

    if sp_results:
        wins  = sum(1 for r in sp_results if r['spread_result'] == 'WIN')
        total = sum(1 for r in sp_results if r['spread_result'] in ('WIN', 'LOSS'))
        print(f"\n  ATS record:  {wins}-{total - wins}" +
              (f"  ({wins/total:.1%})" if total > 0 else ''))

    if ml_results:
        ml_wins = sum(1 for r in ml_results if r['ml_result'] == 'WIN')
        ml_tot  = sum(1 for r in ml_results if r['ml_result'] in ('WIN', 'LOSS'))
        ml_pl   = sum(r.get('ml_pl', 0.0) for r in ml_results)
        print(f"  ML record:   {ml_wins}-{ml_tot - ml_wins}" +
              (f"  ({ml_wins/ml_tot:.1%})" if ml_tot > 0 else '') +
              f"  P&L: ${ml_pl:+.0f}/unit")


def print_full_report() -> None:
    """Print cumulative performance report from all logged results."""
    if not RESULTS_LOG.exists():
        print("  No results log found. Grade some picks first.")
        return

    df = pd.read_csv(RESULTS_LOG)
    # Convert types
    for col in ['market_spread', 'model_spread', 'actual_margin', 'sp_edge',
                'home_ml_edge', 'model_wp', 'ml_pl']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    n_games  = len(df)
    n_dates  = df['game_date'].nunique()

    def _record(sub, result_col):
        wins   = (sub[result_col] == 'WIN').sum()
        losses = (sub[result_col] == 'LOSS').sum()
        total  = wins + losses
        pct    = f"  ({wins/total:.1%})" if total > 0 else ''
        return f"{wins}-{losses}{pct}"

    print(f"\n{'='*66}")
    print(f"  TEAM PICK RESULTS — CUMULATIVE REPORT")
    print(f"  {n_dates} date(s), {n_games} game(s) logged")
    print(f"{'='*66}")

    # Overall ATS
    sp_df = df[df['spread_result'].isin(['WIN', 'LOSS', 'PUSH'])]
    if not sp_df.empty:
        print(f"\n  ATS (all picks): {_record(sp_df, 'spread_result')}")

    # Overall ML
    ml_df = df[df['ml_result'].isin(['WIN', 'LOSS', 'PUSH'])]
    if not ml_df.empty:
        ml_pl = df['ml_pl'].sum() if 'ml_pl' in df.columns else 0.0
        print(f"  ML  (all picks): {_record(ml_df, 'ml_result')}  P&L: ${ml_pl:+.0f}/unit ($100 units)")

    # By tier
    for tier in ['STRONG', 'LEAN']:
        sub_sp = sp_df[sp_df['tier'] == tier]
        sub_ml = ml_df[ml_df['tier'] == tier]
        if len(sub_sp) > 0 or len(sub_ml) > 0:
            print(f"\n  {tier} tier:")
            if not sub_sp.empty:
                print(f"    ATS: {_record(sub_sp, 'spread_result')}")
            if not sub_ml.empty:
                ml_pl_tier = df[df['tier'] == tier]['ml_pl'].sum()
                print(f"    ML:  {_record(sub_ml, 'ml_result')}  P&L: ${ml_pl_tier:+.0f}/unit")

    # Calibration by model_wp bucket
    wp_df = df[df['model_wp'].notna() & df['actual_winner'].notna()]
    if not wp_df.empty:
        wp_df = wp_df.copy()
        wp_df['home_won'] = (wp_df['actual_margin'] > 0).astype(float)
        print(f"\n  Win-probability calibration:")
        print(f"  {'Bucket':<14} {'N':>5} {'Pred P':>7} {'Actual':>7}")
        for lo, hi in [(0.0, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.0)]:
            sub = wp_df[(wp_df['model_wp'] >= lo) & (wp_df['model_wp'] < hi)]
            if len(sub) >= 3:
                pred = sub['model_wp'].mean()
                act  = sub['home_won'].mean()
                print(f"  [{lo:.0%},{hi:.0%})       {len(sub):>5} {pred:>7.1%} {act:>7.1%}")

    # Playoff vs regular season
    if 'is_playoffs' in df.columns:
        for label, val in [('Regular Season', 'False'), ('Playoffs', 'True')]:
            sub = df[df['is_playoffs'].astype(str) == val]
            sp_sub = sub[sub['spread_result'].isin(['WIN', 'LOSS'])]
            if len(sp_sub) >= 3:
                print(f"\n  {label}: ATS {_record(sp_sub, 'spread_result')}")

    print(f"\n  Log: {RESULTS_LOG}")
    print(f"{'='*66}\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Grade NBA team pick results")
    parser.add_argument('--date', default=None,
                        help='Grade picks for YYYY-MM-DD (default: yesterday)')
    parser.add_argument('--all', action='store_true',
                        help='Grade all ungraded pick files in data/team_picks/')
    parser.add_argument('--report', action='store_true',
                        help='Print cumulative performance report')
    parser.add_argument('--no-save', action='store_true',
                        help='Print grades but do not write to results_log.csv')
    args = parser.parse_args()

    if args.report:
        print_full_report()
        return

    if args.all:
        files = sorted(PICKS_DIR.glob('20??-??-??.json'))
        dates = [f.stem for f in files]
        print(f"  Grading {len(dates)} pick file(s)...")
    elif args.date:
        dates = [args.date]
    else:
        # Default: yesterday
        from datetime import timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        dates = [yesterday]
        print(f"  No date specified — grading yesterday's picks ({yesterday})")

    all_results = []
    for d in dates:
        print(f"\n  ── {d} ──")
        results = grade_date(d)
        if results:
            print_day_summary(results, d)
            all_results.extend(results)

    if all_results and not args.no_save:
        finalized = [r for r in all_results if r.get('spread_result') != 'PENDING']
        if finalized:
            append_results(finalized)
        else:
            print("\n  All results still PENDING — nothing written to log.")

    if not args.report and RESULTS_LOG.exists():
        print(f"\n  Run with --report for cumulative stats.\n")


if __name__ == '__main__':
    main()
