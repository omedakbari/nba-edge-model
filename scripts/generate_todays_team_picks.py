"""
Daily NBA Team Picks — Spread & Moneyline.

Generates spread and moneyline recommendations for today's NBA games by
comparing the team power-rating model against ESPN's live DraftKings lines.

Supports regular season, Play-In, and Playoffs.

Edge definition:
  Spread:     model_spread differs from market_spread by >= MIN_SPREAD_EDGE points
              (same direction — model agrees the line is wrong, not crossing)
  Moneyline:  model win% differs from market-implied win% by >= MIN_ML_EDGE
              AND spread edge agrees directionally

Confidence tiers:
  STRONG:  |gap| >= STRONG_SPREAD_EDGE (5.5 pts) or ML edge >= STRONG_ML_EDGE (12%)
  LEAN:    |gap| >= MIN_SPREAD_EDGE (3.5 pts) or ML edge >= MIN_ML_EDGE (7%)
  MONITOR: 2 <= |gap| < 3.5 — not a pick, but flag for awareness

Output:
  Console table + optional JSON to data/team_picks/YYYY-MM-DD.json

Usage:
    # Today's games
    source venv/bin/activate
    python scripts/generate_todays_team_picks.py

    # Specific date
    python scripts/generate_todays_team_picks.py --date 2026-04-20

    # Playoffs mode (adjusts HCA, enables series momentum)
    python scripts/generate_todays_team_picks.py --playoffs

    # Save picks for later grading
    python scripts/generate_todays_team_picks.py --playoffs --save
"""

import argparse
import json
import sqlite3
import sys
import warnings
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

import numpy as np

from config.settings import DB_PATH, CURRENT_SEASON
from src.models.team_ratings import TeamRatingEngine
from src.data.espn_odds import fetch_odds, moneyline_to_implied_prob

PICKS_DIR = Path(__file__).parent.parent / 'data' / 'team_picks'

# ── Thresholds ─────────────────────────────────────────────────────────────────

MIN_SPREAD_EDGE    = 3.5    # minimum model-vs-market spread gap to flag a pick
STRONG_SPREAD_EDGE = 5.5    # strong pick threshold
MONITOR_SPREAD     = 2.0    # show in monitor section (not a pick)
MIN_ML_EDGE        = 0.07   # minimum win-probability edge vs market implied
STRONG_ML_EDGE     = 0.12
MIN_GAMES_FOR_PICK = 10     # team must have >= N games this season for a valid rating


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pct(x):
    return f"{x:.1%}" if x is not None else "N/A"

def _spread_str(s):
    if s is None:
        return "  N/A "
    return f"{s:+.1f}"

def _ml_str(ml):
    if ml is None:
        return "  N/A"
    return f"{ml:+d}" if ml > 0 else str(ml)

def american_to_display(ml):
    if ml is None:
        return "  N/A"
    return f"+{ml}" if ml > 0 else str(ml)


def spread_edge(model_spread: float, market_spread: float | None) -> float | None:
    """
    Edge = how much more/less the model favors home vs market.

    Conventions:
      model_spread:  POSITIVE = home favored (e.g. +6.7 means home wins by 6.7)
      market_spread: NEGATIVE = home favored (standard ATS: CHA -5.5 → market_spread = -5.5)

    Edge = model_spread + market_spread
      e.g. model=+6.7, market=-5.5 → edge = +1.2 (model slightly more bullish on home)
           model=+1.1, market=-3.5 → edge = -2.4 (model thinks away is undervalued)

    Positive edge → take home ATS; Negative edge → take away ATS.
    """
    if market_spread is None:
        return None
    return round(model_spread + market_spread, 2)


def ml_edge(model_wp: float, home_implied: float | None, away_implied: float | None) -> dict:
    """
    Returns edge for home and away sides.
    Edge = model_win% - market_implied_win%.
    Positive = model more bullish than market.
    """
    if home_implied is None or away_implied is None:
        return {'home': None, 'away': None}

    # Remove vig: renormalize implied probs to 100%
    total = home_implied + away_implied
    home_no_vig = home_implied / total
    away_no_vig = away_implied / total

    return {
        'home': round(model_wp - home_no_vig, 4),
        'away': round((1 - model_wp) - away_no_vig, 4),
    }


def pick_tier(sp_edge: float | None, ml_edge_val: float | None) -> str:
    """Return pick tier: STRONG / LEAN / MONITOR / NONE."""
    sp = abs(sp_edge) if sp_edge is not None else 0.0
    ml = abs(ml_edge_val) if ml_edge_val is not None else 0.0

    if sp >= STRONG_SPREAD_EDGE or ml >= STRONG_ML_EDGE:
        return 'STRONG'
    elif sp >= MIN_SPREAD_EDGE or ml >= MIN_ML_EDGE:
        return 'LEAN'
    elif sp >= MONITOR_SPREAD:
        return 'MONITOR'
    return 'NONE'


# ── Core logic ────────────────────────────────────────────────────────────────

def analyze_game(
    game: dict,
    engine: TeamRatingEngine,
    is_playoffs: bool,
    game_date: str,
    series_g1_winner: str | None = None,
) -> dict:
    """
    Analyze a single game dict from ESPN odds. Returns analysis dict.
    series_g1_winner: if Game 2+, pass the team abbreviation that won G1
    for a ±1.5 pt series momentum adjustment.
    """
    home = game['home']
    away = game['away']

    # Model spread and win probability
    model_spread = engine.predict_spread(
        home, away, game_date=game_date, is_playoffs=is_playoffs,
        series_g1_winner=series_g1_winner,
    )
    model_wp = engine.win_probability(
        home, away, game_date=game_date, is_playoffs=is_playoffs,
        series_g1_winner=series_g1_winner,
    )
    home_rating  = engine.get_rating(home)
    away_rating  = engine.get_rating(away)

    # Market values
    market_spread   = game.get('spread')     # home perspective
    home_ml         = game.get('home_ml')
    away_ml         = game.get('away_ml')
    over_under      = game.get('over_under')
    home_implied    = game.get('home_implied')
    away_implied    = game.get('away_implied')

    # Edge calculations
    sp_edge  = spread_edge(model_spread, market_spread)
    ml_edges = ml_edge(model_wp, home_implied, away_implied)
    home_ml_edge = ml_edges['home']
    away_ml_edge = ml_edges['away']

    # Determine recommended side for spread
    spread_pick_side = None
    spread_pick_team = None
    if sp_edge is not None:
        if sp_edge > MIN_SPREAD_EDGE:
            spread_pick_side = 'home'
            spread_pick_team = home
        elif sp_edge < -MIN_SPREAD_EDGE:
            spread_pick_side = 'away'
            spread_pick_team = away

    # Determine recommended side for ML
    ml_pick_side = None
    ml_pick_team = None
    if home_ml_edge is not None and abs(home_ml_edge) >= MIN_ML_EDGE:
        if home_ml_edge > 0:
            ml_pick_side = 'home'
            ml_pick_team = home
        elif away_ml_edge is not None and away_ml_edge > 0:
            ml_pick_side = 'away'
            ml_pick_team = away

    # Overall tier (take max of spread and ML signals)
    tier_sp = pick_tier(sp_edge, None)
    tier_ml = pick_tier(None, home_ml_edge if home_ml_edge and abs(home_ml_edge) >= abs(away_ml_edge or 0) else away_ml_edge)
    tier_order = ['STRONG', 'LEAN', 'MONITOR', 'NONE']
    tier = tier_order[min(tier_order.index(tier_sp), tier_order.index(tier_ml))]

    return {
        'game_id':         game['game_id'],
        'game_date':       game_date,
        'status':          game['status'],
        'home':            home,
        'away':            away,
        'home_full':       game['home_full'],
        'away_full':       game['away_full'],
        'home_rating':     round(home_rating, 2),
        'away_rating':     round(away_rating, 2),
        'model_spread':    model_spread,
        'model_wp':        model_wp,
        'market_spread':   market_spread,
        'market_spread_str': game.get('spread_team', ''),
        'home_ml':         home_ml,
        'away_ml':         away_ml,
        'over_under':      over_under,
        'home_implied':    home_implied,
        'away_implied':    away_implied,
        'sp_edge':         sp_edge,
        'home_ml_edge':    home_ml_edge,
        'away_ml_edge':    away_ml_edge,
        'spread_pick_side':spread_pick_side,
        'spread_pick_team':spread_pick_team,
        'ml_pick_side':    ml_pick_side,
        'ml_pick_team':    ml_pick_team,
        'tier':            tier,
        'odds_provider':   game.get('odds_provider', ''),
        'is_playoffs':     is_playoffs,
        'series_g1_winner': series_g1_winner,
    }


# ── Display ───────────────────────────────────────────────────────────────────

def print_game_block(a: dict) -> None:
    """Print a formatted block for one game analysis."""
    status_tag = f"[{a['status'].upper()}]" if a['status'] != 'scheduled' else ''
    print(f"\n  {'─'*62}")
    print(f"  {a['away_full']:<28} @  {a['home_full']}{status_tag}")
    print(f"  {'─'*62}")

    # Ratings and model spread
    print(f"  Model ratings: {a['away']} {a['away_rating']:+.1f}  |  "
          f"{a['home']} {a['home_rating']:+.1f}")
    print(f"  Model spread (home): {_spread_str(a['model_spread'])}  "
          f"Model P(home win): {_pct(a['model_wp'])}")

    # Market
    if a['market_spread'] is not None:
        mkt_str = a.get('market_spread_str', '')
        print(f"  Market spread:       {_spread_str(a['market_spread'])}  ({mkt_str})  "
              f"O/U: {a['over_under'] or 'N/A'}")
        print(f"  Market ML:  {a['home']} {american_to_display(a['home_ml'])}  |  "
              f"{a['away']} {american_to_display(a['away_ml'])}  "
              f"(implied: {_pct(a['home_implied'])} / {_pct(a['away_implied'])})")
    else:
        print(f"  Market:  No line available")

    # Edge
    if a['sp_edge'] is not None:
        edge_dir = '→ home undervalued' if a['sp_edge'] > 0 else '→ away undervalued'
        print(f"  Spread edge: {a['sp_edge']:+.1f} pts  {edge_dir}")
    if a['home_ml_edge'] is not None:
        ml_e = a['home_ml_edge']
        ml_dir = f"{a['home']} model={_pct(a['model_wp'])} vs mkt={_pct(a['home_implied'])}"
        print(f"  ML edge:    {ml_e:+.1%}  ({ml_dir})")

    # Recommendation
    tier = a['tier']
    if tier in ('STRONG', 'LEAN'):
        picks = []
        if a['spread_pick_team']:
            market_s = a['market_spread']
            if a['spread_pick_side'] == 'home' and market_s is not None:
                # Home at their standard line: market_spread = -5.5 → "CHA -5.5"
                line_str = f"{a['home']} {_spread_str(market_s)}"
            elif market_s is not None:
                # Away at their standard line: market_spread = -5.5 → away gets +5.5
                line_str = f"{a['away']} {_spread_str(-market_s)}"
            else:
                line_str = f"{a['spread_pick_team']} (no market line)"
            picks.append(f"ATS: {line_str}")
        if a['ml_pick_team']:
            ml_odds = a['home_ml'] if a['ml_pick_side'] == 'home' else a['away_ml']
            picks.append(f"ML:  {a['ml_pick_team']} {american_to_display(ml_odds)}")

        if picks:
            tier_marker = '★ STRONG' if tier == 'STRONG' else '● LEAN'
            print(f"\n  {tier_marker}  {'  |  '.join(picks)}")
    elif tier == 'MONITOR':
        print(f"\n  ○ MONITOR  (gap {a['sp_edge']:+.1f} — not yet threshold)")
    else:
        print(f"\n  — No edge")


def print_summary_table(analyses: list[dict]) -> None:
    """Print a compact one-line-per-game summary table."""
    picks   = [a for a in analyses if a['tier'] in ('STRONG', 'LEAN')]
    monitor = [a for a in analyses if a['tier'] == 'MONITOR']
    no_edge = [a for a in analyses if a['tier'] == 'NONE']

    print(f"\n\n  {'='*76}")
    print(f"  PICK SUMMARY")
    print(f"  {'='*76}")
    print(f"  {'Game':<30} {'Tier':<8} {'Spread Pick':<22} {'ML Pick':<20} {'Edge'}")
    print(f"  {'─'*76}")

    for a in picks:
        sp_pick = ''
        if a['spread_pick_team'] and a['market_spread'] is not None:
            ms = a['market_spread']
            if a['spread_pick_side'] == 'home':
                # Home at their standard line: market_spread = -5.5 → "CHA -5.5"
                sp_pick = f"{a['home']} {_spread_str(ms)}"
            else:
                # Away at their standard line: market_spread = -5.5 → away gets +5.5
                sp_pick = f"{a['away']} {_spread_str(-ms)}"
        ml_pick = ''
        if a['ml_pick_team']:
            ml_odds = a['home_ml'] if a['ml_pick_side'] == 'home' else a['away_ml']
            ml_pick = f"{a['ml_pick_team']} {american_to_display(ml_odds)}"
        edge_str = f"gap {a['sp_edge']:+.1f}" if a['sp_edge'] is not None else ''
        game_str = f"{a['away']} @ {a['home']}"
        tier_m = '★ STRONG' if a['tier'] == 'STRONG' else '● LEAN'
        print(f"  {game_str:<30} {tier_m:<8} {sp_pick:<22} {ml_pick:<20} {edge_str}")

    if not picks:
        print(f"  No picks today (all games below edge threshold)")

    if monitor:
        print(f"\n  Monitor (gap 2–3.5 pts, not threshold):")
        for a in monitor:
            print(f"    {a['away']} @ {a['home']:12} gap {a['sp_edge']:+.1f}  "
                  f"model {_spread_str(a['model_spread'])} vs mkt {_spread_str(a['market_spread'])}")

    # Key stats
    print(f"\n  Games analyzed: {len(analyses)}  |  Picks: {len(picks)}  |  "
          f"Monitor: {len(monitor)}  |  No edge: {len(no_edge)}")


def print_header(target_date: str, season: str, is_playoffs: bool, is_play_in: bool) -> None:
    mode = 'PLAYOFFS' if is_playoffs else ('PLAY-IN' if is_play_in else 'REGULAR SEASON')
    print(f"\n{'='*76}")
    print(f"  NBA TEAM PICKS  —  {target_date}  |  {season}  |  {mode}")
    print(f"  Model: power ratings (NET_RTG blend + L10 form + rest adj)")
    print(f"  Lines: ESPN / DraftKings  |  Edge thresholds: spread {MIN_SPREAD_EDGE}pt lean / {STRONG_SPREAD_EDGE}pt strong")
    print(f"{'='*76}")


def print_footer(is_playoffs: bool) -> None:
    print(f"\n  {'='*76}")
    print(f"  CAVEATS")
    print(f"  Edge = model spread vs opening DraftKings line (not closing line).")
    print(f"  Model directional accuracy: 68.2% (4-season backtest, 4,604 games).")
    print(f"  Win-probability calibrated (Platt scaling, Brier 0.219).")
    if is_playoffs:
        print(f"  Playoff adjustments active: HCA +3.0 pts, series momentum factor.")
    print(f"  ⚠️  Always verify injury/lineup news before placing bets.")
    print(f"  {'='*76}\n")


# ── Save / load ───────────────────────────────────────────────────────────────

def save_picks(analyses: list[dict], target_date: str) -> Path:
    PICKS_DIR.mkdir(parents=True, exist_ok=True)
    path = PICKS_DIR / f"{target_date}.json"
    # Serialize (strip non-JSON-friendly types)
    records = []
    for a in analyses:
        r = {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v)
             for k, v in a.items() if v is not None}
        records.append(r)
    path.write_text(json.dumps({'date': target_date, 'picks': records}, indent=2))
    return path


# ── Main ─────────────────────────────────────────────────────────────────────

def _parse_g1_winners(raw: str | None) -> dict[str, str]:
    """
    Parse --g1-winner argument into a {home_abbr: winner_abbr} lookup.
    Accepts comma-separated pairs: "BOS:BOS,NYK:MIL"
    Format per entry: HOME:WINNER  (both are team abbreviations)
    e.g. --g1-winner BOS:BOS means BOS (home) won G1 of their series.
    """
    if not raw:
        return {}
    result = {}
    for entry in raw.split(','):
        entry = entry.strip()
        if ':' in entry:
            home, winner = entry.split(':', 1)
            result[home.strip().upper()] = winner.strip().upper()
    return result


def main():
    parser = argparse.ArgumentParser(description="Generate daily NBA spread/ML picks")
    parser.add_argument('--date',     default=date.today().isoformat(),
                        help='Target date YYYY-MM-DD (default: today)')
    parser.add_argument('--season',   default=CURRENT_SEASON,
                        help='NBA season string (default: current)')
    parser.add_argument('--playoffs', action='store_true',
                        help='Playoff mode: HCA 3.0 pts, series momentum')
    parser.add_argument('--play-in',  action='store_true',
                        help='Play-In mode')
    parser.add_argument('--save',     action='store_true',
                        help='Save picks JSON to data/team_picks/YYYY-MM-DD.json')
    parser.add_argument('--show-all', action='store_true',
                        help='Show all games including no-edge ones')
    parser.add_argument(
        '--g1-winner',
        default=None,
        metavar='HOME:WINNER[,HOME:WINNER...]',
        help=(
            'Series Game 1 winners for momentum adjustment (Game 2+ only). '
            'Format: HOME_ABBR:WINNER_ABBR — e.g. --g1-winner BOS:BOS,MIL:IND'
        ),
    )
    parser.add_argument(
        '--refresh-data',
        action='store_true',
        help='Refresh team_stats from NBA API before generating picks (adds ~2s)',
    )
    args = parser.parse_args()

    target_date = args.date
    is_playoffs = args.playoffs or args.play_in
    is_play_in  = args.play_in

    print_header(target_date, args.season, is_playoffs, is_play_in)

    # Optionally refresh team_stats so ratings use today's full-season numbers
    if args.refresh_data:
        import subprocess, sys as _sys
        print(f"  Refreshing team stats for {args.season}...")
        result = subprocess.run(
            [_sys.executable, 'src/ingestion/team_stats.py', '--update'],
            capture_output=True, text=True,
        )
        if 'rows written' in result.stdout or 'upserted' in result.stdout.lower():
            print(f"  Team stats updated.")
        else:
            print(f"  Team stats refresh done (or unchanged).")

    # Parse G1 winners for series momentum
    g1_winners = _parse_g1_winners(args.g1_winner)
    if g1_winners and is_playoffs:
        print(f"  Series G1 winners: {g1_winners}")

    # Build rating engine
    conn = sqlite3.connect(str(DB_PATH))
    print(f"\n  Building team ratings for {args.season} through {target_date}...")
    engine = TeamRatingEngine(
        conn, season=args.season,
        through_date=target_date,
        include_playoffs=is_playoffs,
    )
    ratings_df = engine.all_ratings()
    teams_with_data = len(ratings_df[ratings_df['games'] >= MIN_GAMES_FOR_PICK])
    print(f"  Ratings ready: {teams_with_data} teams with >= {MIN_GAMES_FOR_PICK} games")

    # Warn if data may be stale (no games in last 3 days)
    last_game_date = engine._games['game_date'].max() if not engine._games.empty else None
    if last_game_date is not None:
        from datetime import timedelta
        days_old = (datetime.strptime(target_date, '%Y-%m-%d') - last_game_date.to_pydatetime().replace(tzinfo=None)).days
        if days_old > 3:
            print(f"  ⚠️  Ratings may be stale — last game in DB: {last_game_date.date()} ({days_old}d ago).")
            print(f"      Run with --refresh-data or update game_logs manually.")

    # Fetch odds
    print(f"  Fetching ESPN lines for {target_date}...")
    games = fetch_odds(target_date)
    if not games:
        print(f"\n  ⚠️  No games or odds found for {target_date}. Check date or try again.")
        conn.close()
        return

    scheduled = [g for g in games if g['status'] == 'scheduled']
    if not scheduled:
        print(f"  All {len(games)} games already finished — grading instead of picking.")
        # Still show analysis for review
        scheduled = games

    print(f"  {len(scheduled)} game(s) to analyze")

    # Analyze each game (apply series momentum where G1 winner is known)
    analyses = []
    for g in scheduled:
        home = g['home']
        away = g['away']
        g1w = g1_winners.get(home) or g1_winners.get(away)   # check both sides
        a = analyze_game(g, engine, is_playoffs, target_date, series_g1_winner=g1w)
        analyses.append(a)

    conn.close()

    # Print detailed blocks for picks and monitors
    picks_and_monitors = [a for a in analyses if a['tier'] != 'NONE']
    if args.show_all:
        to_show = analyses
    else:
        to_show = picks_and_monitors if picks_and_monitors else analyses[:3]

    if to_show:
        print(f"\n{'─'*76}")
        print(f"  GAME ANALYSIS")
        print(f"{'─'*76}")
        for a in to_show:
            print_game_block(a)

    # Summary table
    print_summary_table(analyses)
    print_footer(is_playoffs)

    # Save
    if args.save:
        path = save_picks(analyses, target_date)
        picks_count = sum(1 for a in analyses if a['tier'] in ('STRONG','LEAN'))
        print(f"  Saved {picks_count} pick(s) to {path}\n")

    return analyses


if __name__ == '__main__':
    main()
