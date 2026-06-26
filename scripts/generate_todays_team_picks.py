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

    # Playoffs — auto-derives series context (game #, series score, G1 winner, momentum)
    python scripts/generate_todays_team_picks.py --playoffs --season 2025-26 --save

    # Manual G1 winner override (only needed if DB is out of date)
    python scripts/generate_todays_team_picks.py --playoffs --season 2025-26 --save \
        --g1-winner 'DET:DET,OKC:OKC'
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
import pandas as pd

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

# ── O/U model thresholds ───────────────────────────────────────────────────────
# Conservative thresholds — model is directionally reliable but not calibrated
# against closing lines. Treat O/U picks as supplementary to spread/ML signals.
MIN_OU_EDGE    = 4.0   # pts vs market O/U line to flag a pick
STRONG_OU_EDGE = 7.0   # strong O/U threshold

# Playoff scoring is ~5% lower than regular season due to defensive intensity.
# Calibrated against 2024-25 playoff averages (~107 pts/team vs ~115 reg season).
PLAYOFF_TOTAL_FACTOR = 0.955

VALID_TEAM_CODES = {
    'ATL', 'BOS', 'BKN', 'CHA', 'CHI', 'CLE', 'DAL', 'DEN', 'DET', 'GSW',
    'HOU', 'IND', 'LAC', 'LAL', 'MEM', 'MIA', 'MIL', 'MIN', 'NOP', 'NYK',
    'OKC', 'ORL', 'PHI', 'PHX', 'POR', 'SAC', 'SAS', 'TOR', 'UTA', 'WAS',
}


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


# ── O/U model ─────────────────────────────────────────────────────────────────

def load_team_scoring_stats(conn: sqlite3.Connection, season: str) -> dict[str, dict]:
    """
    Load off_rtg, def_rtg, pace per team from team_stats.
    Returns {team_abbr: {off_rtg, def_rtg, pace}}.
    """
    try:
        df = pd.read_sql(
            "SELECT team_abbr, off_rtg, def_rtg, pace FROM team_stats WHERE season=?",
            conn, params=(season,)
        )
    except Exception:
        return {}
    if df.empty:
        return {}
    return df.set_index('team_abbr')[['off_rtg', 'def_rtg', 'pace']].to_dict(orient='index')


def predict_game_total(home: str, away: str, stats: dict[str, dict],
                       is_playoffs: bool) -> float | None:
    """
    Predict total points scored (home + away) using ORTG/DRTG/pace blend.

    Formula:
      pace_avg = (home_pace + away_pace) / 2
      home_pts = (home_off_rtg + away_def_rtg) / 2  *  pace_avg / 100
      away_pts = (away_off_rtg + home_def_rtg) / 2  *  pace_avg / 100
      total    = home_pts + away_pts  *  playoff_factor

    Note: off_rtg and def_rtg are per-100-poss, so dividing by 100 and
    multiplying by estimated possessions gives per-game points.
    """
    h = stats.get(home)
    a = stats.get(away)
    if not h or not a:
        return None
    if any(v is None or v == 0 for v in [h.get('off_rtg'), h.get('def_rtg'), h.get('pace'),
                                          a.get('off_rtg'), a.get('def_rtg'), a.get('pace')]):
        return None

    pace_avg = (h['pace'] + a['pace']) / 2
    home_pts = (h['off_rtg'] + a['def_rtg']) / 2 * pace_avg / 100
    away_pts = (a['off_rtg'] + h['def_rtg']) / 2 * pace_avg / 100
    total = home_pts + away_pts

    if is_playoffs:
        total *= PLAYOFF_TOTAL_FACTOR

    return round(total, 1)


def ou_pick_tier(edge: float | None) -> str:
    """Return O/U pick tier based on absolute edge vs market line."""
    if edge is None:
        return 'NONE'
    ae = abs(edge)
    if ae >= STRONG_OU_EDGE:
        return 'STRONG'
    elif ae >= MIN_OU_EDGE:
        return 'LEAN'
    return 'NONE'


# ── Core logic ────────────────────────────────────────────────────────────────

def analyze_game(
    game: dict,
    engine: TeamRatingEngine,
    is_playoffs: bool,
    game_date: str,
    series_g1_winner: str | None = None,
    series_context: dict | None = None,
    team_scoring_stats: dict | None = None,
) -> dict:
    """
    Analyze a single game dict from ESPN odds. Returns analysis dict.

    series_context: auto-derived series state dict from derive_series_context().
                    When provided, takes priority for the momentum adjustment.
    series_g1_winner: legacy override; used only when series_context is None.
    team_scoring_stats: {team_abbr: {off_rtg, def_rtg, pace}} for O/U model.
    """
    home = game['home']
    away = game['away']

    # Model spread and win probability
    model_spread = engine.predict_spread(
        home, away, game_date=game_date, is_playoffs=is_playoffs,
        series_g1_winner=series_g1_winner,
        series_context=series_context,
    )
    model_wp = engine.win_probability(
        home, away, game_date=game_date, is_playoffs=is_playoffs,
        series_g1_winner=series_g1_winner,
        series_context=series_context,
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

    # Compute momentum pts for display — mirrors predict_spread() logic
    if series_context and series_context.get('game_num', 1) > 1:
        _lead = series_context.get('home_wins', 0) - series_context.get('away_wins', 0)
        series_momentum_pts = round(max(-2.0, min(2.0, _lead * 0.75)), 2)
    elif series_g1_winner:
        series_momentum_pts = 1.5 if series_g1_winner == home else (
            -1.5 if series_g1_winner == away else 0.0)
    else:
        series_momentum_pts = 0.0

    # O/U model: predict total and compare against market line
    model_total = None
    ou_edge_val = None
    ou_direction = None
    ou_tier = 'NONE'
    if team_scoring_stats:
        model_total = predict_game_total(home, away, team_scoring_stats, is_playoffs)
        if model_total is not None and over_under is not None:
            ou_edge_val = round(model_total - float(over_under), 1)
            ou_direction = 'OVER' if ou_edge_val > 0 else 'UNDER'
            ou_tier = ou_pick_tier(ou_edge_val)

    return {
        'game_id':           game['game_id'],
        'game_date':         game_date,
        'status':            game['status'],
        'home':              home,
        'away':              away,
        'home_full':         game['home_full'],
        'away_full':         game['away_full'],
        'home_rating':       round(home_rating, 2),
        'away_rating':       round(away_rating, 2),
        'model_spread':      model_spread,
        'model_wp':          model_wp,
        'market_spread':     market_spread,
        'market_spread_str': game.get('spread_team', ''),
        'home_ml':           home_ml,
        'away_ml':           away_ml,
        'over_under':        over_under,
        'home_implied':      home_implied,
        'away_implied':      away_implied,
        'sp_edge':           sp_edge,
        'home_ml_edge':      home_ml_edge,
        'away_ml_edge':      away_ml_edge,
        'spread_pick_side':  spread_pick_side,
        'spread_pick_team':  spread_pick_team,
        'ml_pick_side':      ml_pick_side,
        'ml_pick_team':      ml_pick_team,
        'tier':              tier,
        'odds_provider':     game.get('odds_provider', ''),
        'is_playoffs':       is_playoffs,
        'series_g1_winner':  (series_context.get('g1_winner') if series_context
                              else series_g1_winner),
        'series_context':    series_context,
        'series_momentum_pts': series_momentum_pts,
        'model_total':       model_total,
        'ou_edge':           ou_edge_val,
        'ou_direction':      ou_direction,
        'ou_tier':           ou_tier,
    }


# ── Display ───────────────────────────────────────────────────────────────────

def print_game_block(a: dict) -> None:
    """Print a formatted block for one game analysis."""
    status_tag = f"[{a['status'].upper()}]" if a['status'] != 'scheduled' else ''
    print(f"\n  {'─'*62}")
    print(f"  {a['away_full']:<28} @  {a['home_full']}{status_tag}")
    print(f"  {'─'*62}")

    # Series context (playoff mode)
    sc = a.get('series_context')
    if sc is not None:
        if sc.get('from_db') and sc.get('game_num', 1) > 1:
            lead_str = (f"{a['home']} leads" if sc['home_leading']
                        else (f"{a['away']} leads" if sc['home_trailing'] else 'tied'))
            print(f"  Series [auto]:  Game {sc['game_num']}  |  "
                  f"{a['home']} {sc['home_wins']}–{sc['away_wins']} {a['away']}  ({lead_str})"
                  f"  |  G1: {sc['g1_winner']}  |  Last: {sc['last_winner']}")
            mp = a.get('series_momentum_pts', 0.0)
            if mp != 0.0:
                side = a['home'] if mp > 0 else a['away']
                print(f"  Series momentum: {mp:+.2f} pts applied  ({side} advantage)")
        elif sc.get('from_db') and sc.get('game_num', 1) == 1:
            print(f"  Series [auto]:  Game 1  |  series starting  (0-0)")
        else:
            print(f"  Series [auto]:  Game 1  (no prior games in DB — series starting)")

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
    ou_tier = a.get('ou_tier', 'NONE')
    if tier in ('STRONG', 'LEAN'):
        picks = []
        if a['spread_pick_team']:
            market_s = a['market_spread']
            if a['spread_pick_side'] == 'home' and market_s is not None:
                line_str = f"{a['home']} {_spread_str(market_s)}"
            elif market_s is not None:
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
        if a['market_spread'] is None and a['home_ml'] is None:
            print(f"\n  ⚠ SKIPPED  (no line data available)")
        elif a['sp_edge'] is not None:
            print(f"\n  — PASS  (model {_spread_str(a['model_spread'])} vs mkt {_spread_str(a['market_spread'])}, "
                  f"gap {a['sp_edge']:+.1f}, below {MIN_SPREAD_EDGE}pt threshold)")
        else:
            ml_e = a['home_ml_edge']
            ml_str = f"ML edge {ml_e:+.1%}" if ml_e is not None else "below threshold"
            print(f"\n  — PASS  (model {_spread_str(a['model_spread'])} vs mkt {_spread_str(a['market_spread'])}, "
                  f"{ml_str})")

    # O/U recommendation (shown for any game with a modeled total)
    if a.get('model_total') is not None and a.get('over_under') is not None:
        mt = a['model_total']
        mk = float(a['over_under'])
        edge = a.get('ou_edge', 0.0)
        direction = a.get('ou_direction', '')
        if ou_tier in ('STRONG', 'LEAN'):
            ou_marker = '★ STRONG' if ou_tier == 'STRONG' else '● LEAN'
            print(f"  {ou_marker}  O/U {direction}: model {mt:.1f} vs line {mk:.1f}  (edge {edge:+.1f})")
        else:
            print(f"  — O/U PASS  (model {mt:.1f} vs line {mk:.1f}, edge {edge:+.1f}, below {MIN_OU_EDGE}pt threshold)")


def print_summary_table(analyses: list[dict]) -> None:
    """Print a compact one-line-per-game summary table."""
    picks   = [a for a in analyses if a['tier'] in ('STRONG', 'LEAN')]
    monitor = [a for a in analyses if a['tier'] == 'MONITOR']
    passes  = [a for a in analyses if a['tier'] == 'NONE'
               and not (a['market_spread'] is None and a['home_ml'] is None)]
    skipped = [a for a in analyses if a['tier'] == 'NONE'
               and a['market_spread'] is None and a['home_ml'] is None]
    ou_picks = [a for a in analyses if a.get('ou_tier') in ('STRONG', 'LEAN')]

    print(f"\n\n  {'='*88}")
    print(f"  PICK SUMMARY")
    print(f"  {'='*88}")
    print(f"  {'Game':<30} {'Tier':<8} {'Spread Pick':<22} {'ML Pick':<20} {'O/U Pick':<16} {'Edge'}")
    print(f"  {'─'*88}")

    all_to_show = picks + [a for a in analyses if a['tier'] not in ('STRONG', 'LEAN')]
    for a in all_to_show:
        sp_pick = ''
        if a.get('spread_pick_team') and a['market_spread'] is not None:
            ms = a['market_spread']
            if a['spread_pick_side'] == 'home':
                sp_pick = f"{a['home']} {_spread_str(ms)}"
            else:
                sp_pick = f"{a['away']} {_spread_str(-ms)}"
        ml_pick = ''
        if a.get('ml_pick_team'):
            ml_odds = a['home_ml'] if a['ml_pick_side'] == 'home' else a['away_ml']
            ml_pick = f"{a['ml_pick_team']} {american_to_display(ml_odds)}"
        ou_pick_str = ''
        if a.get('ou_tier') in ('STRONG', 'LEAN') and a.get('ou_direction') and a.get('over_under'):
            ou_pick_str = f"{a['ou_direction']} {a['over_under']}"
        edge_str = f"sp {a['sp_edge']:+.1f}" if a['sp_edge'] is not None else ''
        game_str = f"{a['away']} @ {a['home']}"
        tier_val = a.get('tier', 'NONE')
        if tier_val == 'STRONG':
            tier_m = '★ STRONG'
        elif tier_val == 'LEAN':
            tier_m = '● LEAN'
        elif tier_val == 'MONITOR':
            tier_m = '○ MONITOR'
        else:
            tier_m = '— PASS'
        print(f"  {game_str:<30} {tier_m:<8} {sp_pick:<22} {ml_pick:<20} {ou_pick_str:<16} {edge_str}")

    if ou_picks:
        print(f"\n  O/U picks ({len(ou_picks)}):")
        for a in ou_picks:
            ou_m = '★' if a['ou_tier'] == 'STRONG' else '●'
            print(f"    {ou_m} {a['away']} @ {a['home']:<12}  {a['ou_direction']} {a['over_under']}  "
                  f"model {a['model_total']:.1f}  edge {a['ou_edge']:+.1f}")

    # Key stats
    print(f"\n  Games analyzed: {len(analyses)}  |  Spread/ML picks: {len(picks)}  |  "
          f"O/U picks: {len(ou_picks)}  |  Monitor: {len(monitor)}  |  Pass: {len(passes)}")


def print_header(target_date: str, season: str, is_playoffs: bool, is_play_in: bool) -> None:
    mode = 'PLAY-IN' if is_play_in else ('PLAYOFFS' if is_playoffs else 'REGULAR SEASON')
    print(f"\n{'='*76}")
    print(f"  NBA TEAM PICKS  —  {target_date}  |  {season}  |  {mode}")
    print(f"  Model: SRS (opp-adj) + L10 form + per-team HCA + rest adj | O/U: ORTG/DRTG pace model")
    print(f"  Lines: ESPN / DraftKings  |  Spread edge: {MIN_SPREAD_EDGE}pt lean / {STRONG_SPREAD_EDGE}pt strong  |  O/U edge: {MIN_OU_EDGE}pt lean / {STRONG_OU_EDGE}pt strong")
    print(f"{'='*76}")


def print_footer(is_playoffs: bool, is_play_in: bool = False) -> None:
    print(f"\n  {'='*76}")
    print(f"  CAVEATS")
    print(f"  Spread/ML edge = model spread vs opening DraftKings line (not closing line).")
    print(f"  Model: SRS opponent-adj rating + L10 form + per-team HCA (historical) + rest/B2B.")
    print(f"  Directional accuracy: 68.2% (prior model backtest — recalibration pending w/ SRS).")
    print(f"  Win-probability calibrated (Platt scaling). Note: WIN_PROB_SCALE needs recal w/ SRS.")
    print(f"  O/U model: ORTG/DRTG/pace blend — directional signal, not calibrated vs closing lines.")
    print(f"  O/U picks are supplementary; combine with spread/ML signal for highest confidence.")
    if is_play_in:
        print(f"  Play-In mode: standard HCA (2.5 pts), no series momentum.")
    elif is_playoffs:
        print(f"  Playoff adjustments active: HCA +3.0 pts, series momentum, O/U factor {PLAYOFF_TOTAL_FACTOR:.3f}x.")
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

def _parse_g1_winners(raw: str | None, scheduled_games: list[dict] | None = None) -> dict[str, str]:
    """
    Parse and validate --g1-winner argument into a {home_abbr: winner_abbr} lookup.
    Format: comma-separated HOME:WINNER pairs — e.g. 'DET:DET,OKC:OKC'
    Exits with a clear error if format or team codes are invalid.
    Warns (does not exit) if team codes don't appear in tonight's schedule.
    """
    if not raw:
        return {}
    result = {}
    for i, entry in enumerate(raw.split(',')):
        entry = entry.strip()
        if not entry:
            continue
        if ':' not in entry:
            print(f"  ✗ --g1-winner: bad format '{entry}' (entry {i + 1}) — expected HOME:WINNER")
            print(f"    Example: --g1-winner 'DET:DET,OKC:OKC'")
            sys.exit(1)
        home, winner = entry.split(':', 1)
        home, winner = home.strip().upper(), winner.strip().upper()
        for code, label in ((home, 'home'), (winner, 'winner')):
            if code not in VALID_TEAM_CODES:
                print(f"  ✗ --g1-winner: '{code}' is not a valid NBA team code ({label} in '{entry}')")
                print(f"    Valid codes: {', '.join(sorted(VALID_TEAM_CODES))}")
                print(f"    Example: --g1-winner 'DET:DET,OKC:OKC'")
                sys.exit(1)
        result[home] = winner

    if scheduled_games:
        game_teams = {g['home'] for g in scheduled_games} | {g['away'] for g in scheduled_games}
        for h, w in result.items():
            if h not in game_teams and w not in game_teams:
                print(f"  ⚠️  --g1-winner '{h}:{w}' — neither team plays tonight; momentum adjustment won't apply")

    return result


def derive_series_context(
    conn: sqlite3.Connection,
    home: str,
    away: str,
    season: str,
    target_date: str,
) -> dict:
    """
    Query DB for all prior playoff games between home and away this season,
    and derive full series context.

    Returns dict with: prior_games_found, game_num, home_wins, away_wins,
    series_score (home perspective), g1_winner, last_winner, home_leading,
    home_trailing, series_tied, from_db.
    """
    rows = conn.execute("""
        SELECT DISTINCT game_id, game_date, wl
        FROM game_logs
        WHERE season = ?
          AND season_type = 'Playoffs'
          AND team_abbr = ?
          AND opp_abbr = ?
          AND game_date < ?
        ORDER BY game_date, game_id
    """, (season, home, away, target_date)).fetchall()

    n = len(rows)

    if n == 0:
        return {
            'home': home, 'away': away,
            'prior_games_found': 0,
            'game_num': 1,
            'home_wins': 0, 'away_wins': 0,
            'series_score': '0-0',
            'g1_winner': None,
            'last_winner': None,
            'home_leading': False,
            'home_trailing': False,
            'series_tied': True,
            'from_db': False,
        }

    home_wins = sum(1 for r in rows if r[2] == 'W')
    away_wins = n - home_wins
    g1_winner = home if rows[0][2] == 'W' else away
    last_winner = home if rows[-1][2] == 'W' else away

    return {
        'home': home, 'away': away,
        'prior_games_found': n,
        'game_num': n + 1,
        'home_wins': home_wins,
        'away_wins': away_wins,
        'series_score': f'{home_wins}-{away_wins}',
        'g1_winner': g1_winner,
        'last_winner': last_winner,
        'home_leading': home_wins > away_wins,
        'home_trailing': home_wins < away_wins,
        'series_tied': home_wins == away_wins,
        'from_db': True,
    }


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
                        help='(deprecated — all games now print by default; this flag has no effect)')
    parser.add_argument(
        '--g1-winner',
        default=None,
        metavar="'HOME:WINNER,...'",
        help=(
            'Manual G1 winner override (optional in --playoffs mode — series context '
            'is auto-derived from DB by default). Use only if DB data is missing or wrong. '
            "Format: HOME_ABBR:WINNER_ABBR — e.g. --g1-winner 'DET:DET,OKC:OKC'"
        ),
    )
    parser.add_argument(
        '--refresh-data',
        action='store_true',
        help='Refresh team_stats from NBA API before generating picks (adds ~2s)',
    )
    parser.add_argument(
        '--show-ratings',
        action='store_true',
        help='Print full SRS/HCA ratings table for all teams',
    )
    args = parser.parse_args()

    target_date = args.date
    is_playoffs = args.playoffs         # True only for actual playoffs, NOT play-in
    is_play_in  = args.play_in
    include_in_form = args.playoffs or args.play_in  # include playoff/play-in results in rolling form

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

    # Build rating engine
    conn = sqlite3.connect(str(DB_PATH))
    print(f"\n  Building team ratings for {args.season} through {target_date}...")
    engine = TeamRatingEngine(
        conn, season=args.season,
        through_date=target_date,
        include_playoffs=include_in_form,
    )
    ratings_df = engine.all_ratings()
    teams_with_data = len(ratings_df[ratings_df['games'] >= MIN_GAMES_FOR_PICK])
    print(f"  Ratings ready: {teams_with_data} teams with >= {MIN_GAMES_FOR_PICK} games")
    if args.show_ratings:
        print(f"\n  {'─'*72}")
        print(f"  TEAM RATINGS  (SRS=opponent-adj season, HCA=per-team home advantage)")
        print(f"  {'─'*72}")
        print(f"  {'Team':<6} {'Rating':>7} {'SRS':>7} {'NetRtg':>7} {'L10':>7} {'HCA':>5}")
        for _, row in ratings_df.iterrows():
            srs_s  = f"{row['srs']:+.1f}"  if row['srs']  is not None else '  N/A'
            sp_s   = f"{row['season_pg']:+.1f}" if row['season_pg'] is not None else '  N/A'
            f10_s  = f"{row['form_l10']:+.1f}" if row['form_l10'] is not None else '  N/A'
            hca_s  = f"{row['hca']:.2f}"   if row['hca']  is not None else '  N/A'
            print(f"  {row['team']:<6} {row['rating']:>+7.2f} {srs_s:>7} {sp_s:>7} {f10_s:>7} {hca_s:>5}")
        print(f"  {'─'*72}")

    # Warn if data may be stale. Playoffs allow up to 7-day gaps (between-series breaks);
    # regular season allows 3 days.
    last_game_date = engine._games['game_date'].max() if not engine._games.empty else None
    if last_game_date is not None:
        from datetime import timedelta
        days_old = (datetime.strptime(target_date, '%Y-%m-%d') - last_game_date.to_pydatetime().replace(tzinfo=None)).days
        stale_threshold = 7 if (is_playoffs or is_play_in) else 3
        if days_old > stale_threshold:
            print(f"  ⚠️  Ratings may be stale — last game in DB: {last_game_date.date()} ({days_old}d ago).")
            print(f"      Run with --refresh-data or update game_logs manually.")

    # Fetch odds
    print(f"  Fetching ESPN lines for {target_date}...")
    games = fetch_odds(target_date)
    if not games:
        from datetime import timedelta
        next_date = (datetime.strptime(target_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        next_games = fetch_odds(next_date)
        if next_games:
            matchups = ', '.join(f"{g['away']} @ {g['home']}" for g in next_games[:3])
            print(f"\n  ⚠️  No games on {target_date} (off-day). Next games {next_date}: {matchups}.")
        else:
            print(f"\n  ⚠️  No games or odds found for {target_date}. Check date or try again.")
        conn.close()
        return

    scheduled = [g for g in games if g['status'] == 'scheduled']
    if not scheduled:
        print(f"  All {len(games)} games already finished — grading instead of picking.")
        # Still show analysis for review
        scheduled = games

    print(f"  {len(scheduled)} game(s) to analyze")

    # Parse + validate G1 winners (manual override — optional in playoff mode)
    g1_winners = _parse_g1_winners(args.g1_winner, scheduled_games=scheduled)
    if g1_winners and is_playoffs:
        print(f"  Manual G1 winner overrides provided: {g1_winners}")

    # Auto-derive playoff series context from DB
    series_contexts: dict[str, dict] = {}
    if is_playoffs:
        print(f"\n  Deriving playoff series context from DB...")
        for g in scheduled:
            home, away = g['home'], g['away']
            ctx = derive_series_context(conn, home, away, args.season, target_date)
            series_contexts[home] = ctx
            if not ctx['from_db']:
                print(f"    {away} @ {home}:  Game 1  (series starting)")
            else:
                lead_tag = (f"{home} leads" if ctx['home_leading']
                            else (f"{away} leads" if ctx['home_trailing'] else 'tied'))
                print(f"    {away} @ {home}:  Game {ctx['game_num']}  |  "
                      f"series {home} {ctx['home_wins']}-{ctx['away_wins']} {away}"
                      f"  ({lead_tag})  |  G1: {ctx['g1_winner']}"
                      f"  |  last: {ctx['last_winner']}")
        # Apply manual G1 winner overrides (--g1-winner), if any
        for override_home, override_winner in g1_winners.items():
            if override_home in series_contexts:
                old = series_contexts[override_home].get('g1_winner')
                series_contexts[override_home]['g1_winner'] = override_winner
                if old and old != override_winner:
                    print(f"    ⚠️  Manual override: {override_home} G1 '{old}' (DB) → '{override_winner}'")
            else:
                series_contexts[override_home] = {
                    'home': override_home, 'away': None,
                    'prior_games_found': 0, 'game_num': 2,
                    'home_wins': 0, 'away_wins': 0, 'series_score': 'N/A',
                    'g1_winner': override_winner, 'last_winner': None,
                    'home_leading': False, 'home_trailing': False, 'series_tied': True,
                    'from_db': False,
                }

    # Load team ORTG/DRTG/pace for O/U model
    print(f"\n  Loading team scoring stats for O/U model...")
    team_scoring_stats = load_team_scoring_stats(conn, args.season)
    if team_scoring_stats:
        print(f"  O/U model ready: {len(team_scoring_stats)} teams with ORTG/DRTG data")
    else:
        print(f"  O/U model unavailable (no team_stats data for {args.season})")

    # Analyze each game
    analyses = []
    for g in scheduled:
        home = g['home']
        away = g['away']
        # Series context: auto-derived for playoffs, None for regular season / play-in
        ctx = series_contexts.get(home) if is_playoffs else None
        # Fallback: if no auto context (shouldn't happen), try legacy g1_winners dict
        g1w = None if ctx else (g1_winners.get(home) or g1_winners.get(away)) if is_playoffs else None
        a = analyze_game(
            g, engine, is_playoffs, target_date,
            series_g1_winner=g1w,
            series_context=ctx,
            team_scoring_stats=team_scoring_stats if team_scoring_stats else None,
        )
        analyses.append(a)

    conn.close()

    # Print detailed blocks for every scheduled game
    print(f"\n{'─'*76}")
    print(f"  GAME ANALYSIS")
    print(f"{'─'*76}")
    for a in analyses:
        print_game_block(a)

    # Summary table
    print_summary_table(analyses)
    print_footer(is_playoffs, is_play_in=is_play_in)

    # Save
    if args.save:
        path = save_picks(analyses, target_date)
        picks_count = sum(1 for a in analyses if a['tier'] in ('STRONG','LEAN'))
        print(f"  Saved {picks_count} pick(s) to {path}\n")

    return analyses


if __name__ == '__main__':
    main()
