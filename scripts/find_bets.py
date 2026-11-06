"""
Find +EV NBA player-prop bets at whatever price each app is offering.

Step 1 — fair board: for every player prop with a sportsbook line today, our
research probability from the versioned appearance-v2 model and a no-vig
market anchor. Single-book anchors are labeled. Saved append-only to data/fair.
Only temporally fitted markets at their anchor line are supported.

Step 2 — price it:
  * Kalshi player-prop ladders (auto-fetched, fee included)
  * DraftKings O/U (auto, via ESPN) — rarely +EV since it's our anchor
  * anything else you type in: --price "Jalen Brunson PTS over 26.5 -115"
      prices: -115 / +120 (sportsbook), 1.87 (decimal), x1.85 (pick'em leg), 52c (Kalshi)
  * whole entries: --entry "A PTS over 20.5 x1.8; B REB under 8.5 x1.9"
      (per-leg multipliers, all must hit) or --entry "...; ..." --payout 3

Usage:
    python scripts/find_bets.py                       # today, auto venues
    python scripts/find_bets.py --price "Luka Doncic PRA over 48.5 +105"
    python scripts/find_bets.py --entry "A PTS over 20.5 x1.8; B AST over 6.5 x1.9"
    python scripts/find_bets.py --bankroll 500 --min-ev 0.04

The existing 4% expected-return screen is a candidate policy, not probability
validation. Earlier ROI research used blend parameters fitted on later seasons
and must not be presented as chronological out-of-sample evidence. Approval
also requires independent KEA, calibration, mapping, injury/role, and quote gates.
"""

import argparse
import os
import json
import re
import sqlite3
import sys
import warnings
import math
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import requests

from config.settings import CURRENT_SEASON, PROJECT_ROOT, DB_PATH, MIN_PROP_HISTORY_GAMES, PROP_HISTORY_DAYS, PROP_PRIOR_SIGNAL_WEIGHT
from src.utils.season import detect_phase
from src.betting import ev as E
from src.models import market_blend as MB
from src.data.espn_props import fetch_book_props
from src.betting.eligibility import approval, conservative_joint, KEA_MARGIN, CSB_JOINT_MIN
from src.utils.slate import revision_path, latest_revision, slate_today, utc_now
from src.models.role_context import load_evidence, role_estimate
from src.models import appearance_daily as V2
from config.settings import APPEARANCE_RESEARCH_BUNDLE
from src.betting.quote_quality import anchor_issues
from generate_todays_picks import (normalize_name, fetch_injury_report, get_todays_player_contexts,
                                   get_todays_games)

FAIR_DIR = PROJECT_ROOT / 'data/fair'
MARKET_PICKS_DIR = PROJECT_ROOT / 'data/market_picks'
BOARD_STATS = ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast', 'pts_reb', 'pts_ast']
STAT_ALIASES = {
    'pts': 'pts', 'points': 'pts', 'reb': 'reb', 'rebounds': 'reb', 'ast': 'ast', 'assists': 'ast',
    '3pm': 'fg3m', '3pt': 'fg3m', 'fg3m': 'fg3m', 'threes': 'fg3m', '3s': 'fg3m',
    'pra': 'pra', 'p+r+a': 'pra', 'pts+reb+ast': 'pra',
    'r+a': 'reb_ast', 'reb+ast': 'reb_ast', 'reb_ast': 'reb_ast',
    'p+r': 'pts_reb', 'pts+reb': 'pts_reb', 'pts_reb': 'pts_reb',
    'p+a': 'pts_ast', 'pts+ast': 'pts_ast', 'pts_ast': 'pts_ast',
}
KALSHI_SERIES = {'KXNBAPTS': 'pts', 'KXNBAREB': 'reb', 'KXNBAAST': 'ast',
                 'KXNBA3PT': 'fg3m', 'KXNBAPRA': 'pra'}
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
OUT_STATUSES = ('out', 'inactive')


# ── Fair board ────────────────────────────────────────────────────────────────

def build_fair_board(target_date: str, season: str, is_playoffs: bool = False,
                     *, book_props=None, player_contexts=None, injury_report=None, supplemental_quotes=None) -> list[dict]:
    if is_playoffs:
        print('  PASS: appearance-v2 calibrators were fitted on regular-season appearances only.')
        return []
    print("  Fetching sportsbook props (ESPN)...")
    raw = fetch_book_props(target_date) if book_props is None else book_props
    supplement_path = os.getenv('NBA_SUPPLEMENTAL_QUOTES')
    if supplemental_quotes is None and supplement_path:
        supplemental_quotes = json.loads(Path(supplement_path.format(date=target_date)).read_text())
    if supplemental_quotes:
        from src.data.supplemental_quotes import merge_quotes
        raw, rejected = merge_quotes(raw, supplemental_quotes, utc_now())
        print(f'  Supplemental quote pairs rejected: {len(rejected)}')
    book = {(normalize_name(n), s): v for (n, s), v in raw.items()}
    print(f"  {len(book)} book prop lines")
    if not book:
        return []
    if injury_report is None:
        from src.utils.slate import PACIFIC
        cutoff = datetime.fromisoformat(target_date).replace(tzinfo=PACIFIC).isoformat() if target_date != slate_today().isoformat() else None
        injuries = fetch_injury_report(as_of=cutoff)
    else:
        injuries = injury_report
    with sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True) as conn:
        features_df = pd.read_sql('SELECT g.*,p.full_name FROM game_logs g JOIN players p USING(player_id) '
                                  'WHERE game_date<?', conn, params=[target_date])
    bundle = V2.load_bundle(APPEARANCE_RESEARCH_BUNDLE)
    print('  appearance-v2 research diagnostics; probability validation and approval disabled.')
    contexts = get_todays_player_contexts(target_date, season) if player_contexts is None else player_contexts
    names = features_df.sort_values('game_date').groupby('player_id').tail(1).set_index('player_id')['full_name'].to_dict()
    team_injuries = {}
    for ctx in contexts:
        player_name = names.get(ctx['player_id'], '')
        status = injuries.get(normalize_name(player_name))
        if status:
            team_injuries.setdefault(ctx['team_abbr'], []).append(
                dict(player_id=ctx['player_id'], player=player_name, status=status))
    df, raw_pred = V2.predict(features_df, contexts, target_date, season, bundle)
    if df.empty:
        return []

    role_evidence=load_evidence(PROJECT_ROOT/'data/role_evidence')
    role_history=features_df
    comps = {'pra': ['pts', 'reb', 'ast'], 'reb_ast': ['reb', 'ast'],
             'pts_reb': ['pts', 'reb'], 'pts_ast': ['pts', 'ast']}

    board = []
    for idx, row in df.iterrows():
        name = row.get('full_name', '')
        key = normalize_name(name)
        if any(s in injuries.get(key, '').lower() for s in OUT_STATUSES):
            continue
        absent={r['player_id'] for r in team_injuries.get(row.get('team_abbr'),[])
                if r['status'].lower() in OUT_STATUSES}
        role_analysis=(role_estimate(role_history,int(row['player_id']),absent,target_date,role_evidence)
                       if absent and role_history is not None else
                       dict(adjustment_validated=False,reasons=['No timestamped matched absence history']
                            if absent else ['No validated lineup/role probability model']))
        for stat in BOARD_STATS:
            b = book.get((key, stat))
            if not b or stat + '_blend' not in bundle['models']:
                continue
            if b.get('teams') and set(b['teams']) != {row.get('team_abbr'), row.get('opp_abbr')}:
                continue
            r = sum(raw_pred[c].get(idx, np.nan) for c in comps.get(stat, [stat]))
            l10 = row.get(f'{stat}_L10', np.nan)
            if pd.isna(r) or pd.isna(l10):
                continue
            pred = float(r)
            std = row.get(f'{stat}_std_L10', np.nan)
            std = float(std) if not pd.isna(std) else float('nan')
            p_over = V2.probability(bundle, stat, b['line'], pred, std, b['p_over'], row['short_minutes_risk'])
            board.append({
                'player': name, 'player_id': int(row['player_id']), 'key': key, 'team': row.get('team_abbr', ''),
                'opp': row.get('opp_abbr', ''), 'stat': stat, 'pred': round(pred, 2),
                'std': std, 'book_line': b['line'], 'book_p_over': round(b['p_over'], 4),
                'n_books': b['n_books'], 'injury': injuries.get(key, ''),
                'best_over': b.get('best_over'), 'best_under': b.get('best_under'),
                'game_id': str(row.get('game_id', '')), 'season': season,
                'is_home': int(row.get('is_home', 0)), 'slate_date': target_date,
                'quote': b,
                'probability_validated': False,
                'injury_role_reviewed': False,
                'mapping_verified': bool(b.get('side_mapping_verified')),
                'settlement_verified': False,
                'model_version': 'appearance-v2-research',
                'model_bundle': str(APPEARANCE_RESEARCH_BUNDLE),
                'anchor_kind': b.get('anchor_kind', 'single-book' if b['n_books'] == 1 else 'multi-book'),
                'expected_minutes': float(row['expected_minutes']),
                'short_minutes_risk': float(row['short_minutes_risk']),
                'research_p_over': p_over,
                'validation_notes': ['All positive-minute appearances; pregame rotation eligibility',
                                     'Research calibration has not established reliable profitability',
                                     'Role changes require validated conditional distributions'],
                'team_injuries': team_injuries.get(row.get('team_abbr'), []),
                'opponent_injuries': team_injuries.get(row.get('opp_abbr'), []),
                'injury_feed_nonempty': bool(injuries),
                'role_analysis': role_analysis,
                'fair_p_over_book_line': round(p_over, 4),
            })
    FAIR_DIR.mkdir(parents=True, exist_ok=True)
    board = eligible_board(board, target_date, season)
    revision_path(FAIR_DIR / f'{target_date}.json').write_text(json.dumps(board, indent=1, default=float))
    print(f"  Fair board: {len(board)} props; unsupported calibrations omitted.")
    return board


def eligible_board(board, target_date, season):
    """Use recent history across seasons, with reduced model influence early on."""
    cutoff = (date.fromisoformat(target_date) - timedelta(days=PROP_HISTORY_DAYS)).isoformat()
    with sqlite3.connect(str(DB_PATH)) as conn:
        history = {pid: (recent, current) for pid, recent, current in conn.execute(
            "SELECT player_id, COUNT(DISTINCT game_id), "
            "COUNT(DISTINCT CASE WHEN season=? THEN game_id END) FROM game_logs "
            "WHERE game_date>=? AND game_date<? AND min>0 "
            "AND season_type IN ('Regular Season', 'Playoffs', 'PlayIn') GROUP BY player_id",
            (season, cutoff, target_date))}
    kept = []
    for original in board:
        recent, current = history.get(original.get('player_id'), (0, 0))
        if recent < MIN_PROP_HISTORY_GAMES:
            continue
        rec = dict(original)
        progress = min(current / MIN_PROP_HISTORY_GAMES, 1.0)
        rec.update(history_games=recent, current_season_games=current,
                   history_mode='current' if progress == 1 else 'prior-season blend',
                   signal_weight=(1.0 if rec.get('model_version') == 'appearance-v2-research' else
                                  PROP_PRIOR_SIGNAL_WEIGHT + (1 - PROP_PRIOR_SIGNAL_WEIGHT) * progress))
        if 'book_line' in rec:
            rec['fair_p_over_book_line'] = round(fair_p(rec, rec['book_line'], 'over'), 4)
        kept.append(rec)
    print(f"  History: {len(kept)}/{len(board)} props eligible "
          f"(need {MIN_PROP_HISTORY_GAMES} games in the past {PROP_HISTORY_DAYS} days; prior season allowed).")
    return kept


def fair_p(rec: dict, line: float, side: str) -> float:
    if rec.get('model_version') == 'appearance-v2-research':
        if line != rec['book_line']:
            raise ValueError('Appearance-v2 supports only the calibrated anchor line')
        p = rec['research_p_over']
        return p if side == 'over' else 1-p
    p = MB.fair_p_over(rec['stat'], line, rec['pred'], rec['std'], rec['book_line'], rec['book_p_over'])
    market = MB.shift_line(rec['stat'], rec['book_line'], rec['book_p_over'], line)
    weight = rec.get('signal_weight', 1.0)
    p = market + weight * (p - market)
    return p if side == 'over' else 1 - p


def find(board: list[dict], name: str, stat: str) -> dict | None:
    key = normalize_name(name)
    hits = [r for r in board if r['stat'] == stat and (r['key'] == key or key in r['key'])]
    return hits[0] if len(hits) == 1 else (next((r for r in hits if r['key'] == key), None))


def quote_scan_summary(target_date):
    path = latest_revision(PROJECT_ROOT / 'data/quote_snapshots' / f'{target_date}.json')
    if not path.exists():
        return None
    snapshot = json.loads(path.read_text())
    return dict(source_snapshot=str(path), captured_at=snapshot['captured_at'],
                shadow_freshness=snapshot.get('shadow_freshness'))


def save_market_picks(bets, board, target_date, season, phase):
    """Save candidates; price_sources describes selected records, not feed availability."""
    records, seen = [], set()
    for bet in sorted(bets, key=lambda b: -b['ev']):
        rec = find(board, bet['player'], bet['stat'])
        key = (rec['player_id'], bet['stat'], bet['side']) if rec and 'player_id' in rec else None
        if key is None or key in seen:
            continue
        seen.add(key)
        records.append({
            'player_id': rec['player_id'], 'player_name': rec['player'],
            'team': rec['team'], 'opp': rec['opp'], 'stat': bet['stat'].upper(),
            'stat_key': bet['stat'], 'direction': bet['side'].upper(), 'line': bet['line'],
            'model_pred': rec['pred'], 'prob': round(bet['p'], 4),
            'card': 'market_singles', 'n_legs': 1, 'line_source': bet['venue'],
            'injury_flag': rec['injury'], 'price': bet['price'], 'decimal_odds': bet['dec'],
            'ev': bet['ev'], 'venue': bet['venue'],
            'history_mode': rec.get('history_mode', 'unknown'),
            'current_season_games': rec.get('current_season_games'),
            'signal_weight': rec.get('signal_weight', 1.0),
            'model_version': rec.get('model_version', 'legacy'),
            'anchor_kind': rec.get('anchor_kind', 'unknown'),
            'expected_minutes': rec.get('expected_minutes'),
            'short_minutes_risk': rec.get('short_minutes_risk'),
            'price_source': rec.get('price_source', 'live fetch'),
            'game_id': rec.get('game_id'), 'quote': rec.get('quote'),
            'book': bet.get('book'), 'american_odds_approx': bet.get('american_odds_approx'),
            'venue_quote': bet.get('venue_quote'), 'fee_basis': bet.get('fee_basis'),
            'team_injuries': rec.get('team_injuries'),
            'opponent_injuries': rec.get('opponent_injuries'),
            'role_analysis': rec.get('role_analysis'),
            'approval': bet_approval(rec, bet),
            'exposure_group': f"{target_date}:{rec['player_id']}",
        })
    payload = {'date': target_date, 'season': season,
               'generated_at': datetime.now(timezone.utc).isoformat(),
               'is_playoffs': phase == 'playoffs', 'is_play_in': phase == 'play-in',
               'line_source': 'market',
               'freshness_rules': quote_scan_summary(target_date),
               'price_sources': sorted({r['price_source'] for r in records}),
               'scan_summary': {
                   'eligible_props': len(board),
                   'anchor_books': sorted({q['book'] for r in board
                                           for q in (r.get('quote') or {}).get('paired_quotes', [])}),
                   'selected_candidates': len(records),
                   'price_sources_scope': 'Selected candidates only; an empty list does not imply a feed failure',
               },
               'picks': records}
    MARKET_PICKS_DIR.mkdir(parents=True, exist_ok=True)
    path = revision_path(MARKET_PICKS_DIR / f'{target_date}.json')
    path.write_text(json.dumps(payload, indent=2, allow_nan=False))
    print(f'  Saved {len(records)} market candidates → {path}')
    if not records:
        print(f"  No selected candidates from {len(board)} eligible props; price_sources=[] is expected "
              f"(selected-candidate sources only). Anchor books: "
              f"{', '.join(payload['scan_summary']['anchor_books']) or 'none'}. "
              "See quote-source diagnostics for feed coverage/rejections.")
    return path


# ── Venues ────────────────────────────────────────────────────────────────────

def kalshi_prop_line(m, stat):
    """Accept only an explicit integer-stat N+ title consistent with its strike.

    E.g. YES on 23+ points is OVER 22.5, NO is UNDER 22.5. The integer
    threshold is not itself an O/U line. Full settlement rules still need review.
    """
    labels = {'pts': 'points', 'reb': 'rebounds', 'ast': 'assists',
              'fg3m': 'three pointers', 'pra': 'points rebounds assists'}
    text = (m.get('yes_sub_title') or m.get('title') or '').lower()
    match = re.search(r'(\d+)\+\s*(points rebounds assists|three pointers|points|rebounds|assists)\b', text)
    if not match or match.group(2) != labels.get(stat):
        return None
    n = int(match.group(1))
    try:
        strike = float(m['floor_strike'])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(strike) or m.get('strike_type') != 'greater' or math.floor(strike)+1 != n:
        return None
    return n - .5

def kalshi_bets(board: list[dict], target_date: str) -> list[dict]:
    """Price every open Kalshi NBA player-prop rung (YES = stat >= strike+0.5)."""
    out = []
    ymd = date.fromisoformat(target_date).strftime('%y%b%d').upper()   # e.g. 26OCT21
    for series, stat in KALSHI_SERIES.items():
        try:
            from src.data.http_archive import archived_json
            from src.data.errors import SourceError
            record = archived_json(f"{KALSHI_API}/markets",
                params={'series_ticker': series, 'status': 'open', 'limit': 1000},
                directory=PROJECT_ROOT / 'data/kalshi_snapshots' / target_date)
            markets = record['payload'].get('markets')
            if not isinstance(markets, list):
                raise SourceError('Missing markets array', source='Kalshi', kind='schema')
        except SourceError:
            raise
        for m in markets:
            if ymd not in m.get('event_ticker', '') or m.get('strike_type') != 'greater':
                continue
            title = m.get('yes_sub_title') or m.get('title', '')
            name = re.split(r':', title)[0].strip()
            rec = find(board, name, stat)
            line = kalshi_prop_line(m, stat)
            if rec is None or line is None or (rec.get('model_version') == 'appearance-v2-research' and line != rec['book_line']):
                continue
            for side, ask in (('over', m.get('yes_ask')), ('under', m.get('no_ask'))):
                if not ask or not 0 < ask < 100:
                    continue
                dec = E.kalshi_decimal(ask / 100)
                p = fair_p(rec, float(line), side)
                out.append({'venue': 'Kalshi', 'player': rec['player'], 'stat': stat, 'side': side,
                            'line': float(line), 'price': f"{'YES' if side == 'over' else 'NO'} {ask}c",
                            'dec': dec, 'p': p, 'ev': E.ev(p, dec), 'ticker': m['ticker'],
                            'injury': rec['injury'], 'settlement_verified': False,
                            'fee_basis': 'estimate: 100 contracts, generic taker schedule; actual order cost unverified',
                            'rules_primary': m.get('rules_primary'), 'rules_secondary': m.get('rules_secondary'),
                            'venue_quote': dict(captured_at=utc_now(),source_url=KALSHI_API+'/markets',
                                               raw=m,fee_estimate_contracts=100),
                            'strike_type': m.get('strike_type'), 'floor_strike': m.get('floor_strike')})
    return out


def book_bets(board: list[dict]) -> list[dict]:
    """Sportsbook O/U at the consensus line, best real price across books ESPN lists."""
    out = []
    for rec in board:
        for side in ('over', 'under'):
            dec = rec.get(f'best_{side}')
            if not dec or not np.isfinite(dec) or dec <= 1:
                continue
            p = fair_p(rec, rec['book_line'], side)
            am = round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))
            out.append({'venue': 'Book', 'player': rec['player'], 'stat': rec['stat'],
                        'side': side, 'line': rec['book_line'], 'price': f'{dec:.4f}',
                        'american_odds_approx': am,
                        'dec': dec, 'p': p, 'ev': E.ev(p, dec), 'injury': rec['injury'],
                        'book': rec.get('quote', {}).get(f'best_{side}_book')})
    return out


def bet_approval(rec, bet):
    evidence = dict(rec, **{k: v for k, v in rec.get('quote', {}).items()
                           if k in ('captured_at', 'start_time', 'status')})
    if bet.get('venue') == 'Kalshi':
        evidence['settlement_verified'] = False  # actual fees and market rules need review
    quotes = rec.get('quote', {}).get('paired_quotes', [])
    evidence['quote_updated_at'] = min((q['updated_at'] for q in quotes if q.get('updated_at')), default=None)
    result = approval(evidence, bet['p'], bet['dec'], min_ev=E.MIN_EV,
                      margin=rec.get('kea_margin', KEA_MARGIN))
    issues = anchor_issues(rec.get('quote') or {}, utc_now())
    if issues:
        result['reasons'] = sorted(set(result['reasons'] + issues))
        result.update(approved=False, stake_pct=0.0)
    return result


# ── Manual pricing ────────────────────────────────────────────────────────────

LEG_RE = re.compile(r'^\s*(?P<name>.+?)\s+(?P<stat>[a-z0-9+_]+)\s+(?P<side>over|under|o|u)\s+'
                    r'(?P<line>\d+(\.\d+)?)\s*(?P<price>\S+)?\s*$', re.I)


def parse_leg(s: str):
    m = LEG_RE.match(s)
    if not m:
        raise ValueError(f'could not parse "{s}" — format: Name STAT over|under LINE [price]')
    stat = STAT_ALIASES.get(m['stat'].lower())
    if not stat:
        raise ValueError(f'unknown stat "{m["stat"]}"')
    side = 'over' if m['side'].lower().startswith('o') else 'under'
    return m['name'], stat, side, float(m['line']), m['price']


def price_one(board, s: str, bankroll: float):
    name, stat, side, line, price = parse_leg(s)
    rec = find(board, name, stat)
    if rec is None:
        print(f"  ✗ {name} {stat.upper()}: no sportsbook line today → no trustworthy fair price")
        return
    if rec.get('model_version') == 'appearance-v2-research' and line != rec['book_line']:
        print('  PASS: no appearance-v2 calibration at that alternate line.')
        return
    p = fair_p(rec, line, side)
    print(f"\n  {rec['player']} {stat.upper()} {side} {line}   (book {rec['book_line']}, "
          f"model pred {rec['pred']})")
    fair_dec = 1 / p
    fair_am = round((fair_dec - 1) * 100) if fair_dec >= 2 else round(-100 / (fair_dec - 1))
    print(f"  diagnostic P (UNVALIDATED) = {p:.1%}; implied fair price {fair_am:+d} / {fair_dec:.2f}x")
    if price:
        dec, kind = E.parse_price(price)
        v = E.ev(p, dec)
        gate = bet_approval(rec, dict(p=p, dec=dec, venue=kind))
        st = gate['stake_pct']
        verdict = 'APPROVED' if gate['approved'] else 'REVIEW/PASS: ' + '; '.join(gate['reasons'])
        print(f"  price {price} ({kind}) → breakeven {E.breakeven(dec):.1%}   EV {v:+.1%}   {verdict}"
              + (f"   stake {st:.1%} = ${st * bankroll:.2f}" if v >= E.MIN_EV else ''))
    if rec['injury']:
        print(f"  ⚠️  injury status: {rec['injury']}")


def price_entry(board, s: str, payout: float | None, bankroll: float):
    legs = [x for x in s.split(';') if x.strip()]
    probs, mult, games, ok = [], 1.0, [], True
    print(f"\n  ENTRY ({len(legs)} legs, all must hit)")
    for leg in legs:
        name, stat, side, line, price = parse_leg(leg)
        rec = find(board, name, stat)
        if rec is None:
            print(f"    ✗ {name} {stat.upper()}: no book line — can't price"); ok = False; continue
        if rec.get('model_version') == 'appearance-v2-research' and line != rec['book_line']:
            print('    PASS: alternate line lacks appearance-v2 calibration'); ok = False; continue
        p = fair_p(rec, line, side)
        probs.append(p)
        games.append(frozenset((rec['team'], rec['opp'])))
        if price:
            mult *= E.parse_price(price)[0]
        print(f"    {rec['player']:<26} {stat.upper():<7} {side:<5} {line:<5} fair P {p:.1%}"
              + (f"  leg {price}" if price else ''))
    if not ok or not probs:
        return
    total = payout if payout else mult
    # We have point estimates, not validated conservative bounds. Never turn
    # their product into an entry recommendation (same-game or cross-game).
    print(f"    REVIEW: no validated conservative joint probability; CSB requires {CSB_JOINT_MIN:.0%}.")
    print('    No entry approval or sizing. Dependence cannot be assumed away.')


def quality_board(board):
    now = utc_now()
    kept, excluded = [], []
    for rec in board:
        issues = anchor_issues(rec.get('quote') or {}, now)
        if issues:
            excluded.append(dict(player=rec.get('player'), stat=rec.get('stat'), reasons=issues))
        else:
            kept.append(rec)
    return kept, excluded


def print_board_diagnostics(board, excluded):
    from collections import Counter
    counts = Counter(r.get('anchor_kind', 'unknown') for r in board)
    print('  Market anchors: ' + ', '.join(f'{k}={v}' for k,v in sorted(counts.items())))
    if counts.get('single-book'):
        print('  Single-book anchors are not cross-book consensus.')
    print(f'  Quote quality: {len(board)} retained, {len(excluded)} excluded before pricing.')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global KEA_MARGIN
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=slate_today().isoformat())
    ap.add_argument('--kea-margin', type=float, default=KEA_MARGIN,
                    help='probability advantage over all-in break-even; separate from --min-ev')
    ap.add_argument('--season', default=CURRENT_SEASON)
    ap.add_argument('--price', action='append', default=[], help='"Name STAT over|under LINE PRICE"')
    ap.add_argument('--entry', action='append', default=[], help='"leg; leg; ..." (per-leg prices or --payout)')
    ap.add_argument('--payout', type=float, help='total entry multiplier, e.g. 3 for a 2-pick 3x')
    ap.add_argument('--bankroll', type=float, default=100.0)
    ap.add_argument('--min-ev', type=float, default=E.MIN_EV)
    ap.add_argument('--reuse-board', action='store_true', help='reuse saved data/fair/DATE.json')
    ap.add_argument('--book-quotes', type=Path, help='JSON list of timestamped paired supplemental book quotes')
    ap.add_argument('--save', action='store_true', help='save market candidates for next-day grading')
    phases = ap.add_mutually_exclusive_group()
    phases.add_argument('--playoffs', action='store_true')
    phases.add_argument('--play-in', action='store_true')
    args = ap.parse_args()
    if not math.isfinite(args.kea_margin) or not 0 <= args.kea_margin < 1:
        ap.error('--kea-margin must be a finite fraction in [0, 1)')
    if not math.isfinite(args.min_ev) or args.min_ev < 0:
        ap.error('--min-ev must be a finite nonnegative return threshold')
    if not math.isfinite(args.bankroll) or args.bankroll <= 0:
        ap.error('--bankroll must be positive and finite')
    E.MIN_EV = args.min_ev
    phase = 'playoffs' if args.playoffs else 'play-in' if args.play_in else detect_phase(args.season)

    games = get_todays_games(args.date)
    if not games:
        print('  No upcoming games found — no pregame bets.')
        return
    prefixes = {str(g.get('game_id', ''))[:3] for g in games}
    if prefixes == {'001'}:
        print("\n  Preseason/no regular games today — props aren't priced and minutes are random. No bets.")
        return

    path = latest_revision(FAIR_DIR / f'{args.date}.json')
    board = (json.loads(path.read_text()) if args.reuse_board and path.exists()
             else build_fair_board(args.date, args.season, is_playoffs=phase in ('playoffs', 'play-in'),
                                   supplemental_quotes=json.loads(args.book_quotes.read_text()) if args.book_quotes else None))
    # Saved boards may include games that have tipped off since generation.
    upcoming = {frozenset((g['home_abbr'], g['away_abbr'])) for g in games}
    board = [r for r in board if frozenset((r['team'], r['opp'])) in upcoming]
    if not board:
        if args.save:
            save_market_picks([], [], args.date, args.season, phase)
        print('  No eligible fair board: quotes may be absent, rejected, or unsupported by calibration.')
        return

    board = eligible_board(board, args.date, args.season)
    KEA_MARGIN = args.kea_margin
    if not board:
        if args.save:
            save_market_picks([], [], args.date, args.season, phase)
        print('  PASS: no props meet the recent-appearance history requirement.')
        return

    if args.price or args.entry:
        board, excluded = quality_board(board)
        print_board_diagnostics(board, excluded)
    for s in args.price:
        price_one(board, s, args.bankroll)
    for s in args.entry:
        price_entry(board, s, args.payout, args.bankroll)
    if args.price or args.entry:
        return

    board, excluded = quality_board(board)
    print_board_diagnostics(board, excluded)
    bets = kalshi_bets(board, args.date) + book_bets(board)
    good = sorted([b for b in bets if b['ev'] >= args.min_ev], key=lambda b: -b['ev'])
    from collections import Counter
    print('  Candidate sides: ' + str(dict(Counter(b['side'] for b in good))))
    diagnostics_path = PROJECT_ROOT / 'data/market_diagnostics' / f'{args.date}.json'
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    revision_path(diagnostics_path).write_text(json.dumps(dict(
        generated_at=utc_now(), excluded_quotes=excluded, eligible_props=len(board),
        freshness_rules=quote_scan_summary(args.date),
        model_versions=dict(Counter(r.get('model_version', 'legacy') for r in board)),
        anchors=dict(Counter(r.get('anchor_kind', 'unknown') for r in board)),
        candidate_sides=dict(Counter(b['side'] for b in good))), indent=2))
    if args.save:
        save_market_picks(good, board, args.date, args.season, phase)
    print(f"\n  DIAGNOSTIC +EV CANDIDATES (UNVALIDATED) (EV ≥ {args.min_ev:.0%})  —  {len(good)} found")
    print(f"  {'Venue':<10} {'Player':<24} {'Stat':<7} {'Side':<6} {'Line':>5} {'Price':>9} "
          f"{'Fair P':>7} {'B/E':>6} {'EV':>7} {'Stake':>8}")
    if len(good) > 40:
        print(f'  Showing 40 of {len(good)} candidates; full list is in the saved JSON when --save is used.')
    print('  Stakes are per-candidate calculations, not a combined betting allocation.')
    repeated = {b['player'] for b in good if sum(x['player'] == b['player'] for x in good) > 1}
    if repeated:
        print('  Overlapping player exposure (no combined allocation): ' + ', '.join(sorted(repeated)))
    for b in good[:40]:
        rec = find(board, b['player'], b['stat'])
        gate = bet_approval(rec, b)
        st = gate['stake_pct']
        print(f"  {b['venue']:<10} {b['player'][:24]:<24} {b['stat'].upper():<7} {b['side']:<6} "
              f"{b['line']:>5} {b['price']:>9} {b['p']:>7.1%} {1 / b['dec']:>6.1%} {b['ev']:>+7.1%} "
              f"${st * args.bankroll:>7.2f}" + (f"  ⚠️ {b['injury']}" if b.get('injury') else ''))
        role = rec.get('role_analysis', {})
        absent = [x['player'] for x in rec.get('team_injuries', []) if x['status'].lower() in OUT_STATUSES]
        if absent:
            print(f"    Teammates out: {', '.join(absent)}; matched role games: {role.get('evidence_games', 0)}; no validated role adjustment.")
        print('    ' + ('APPROVED' if gate['approved'] else 'REVIEW/PASS: ' + '; '.join(gate['reasons'])))
    print(f"\n  Price-check any app or sportsbook:")
    print(f'    nba bets --reuse --price "Name PTS over 24.5 -110"')
    print('  Prior-season history allowed. Appearance-v2 probabilities are research estimates; no approval.')


if __name__ == '__main__':
    main()
