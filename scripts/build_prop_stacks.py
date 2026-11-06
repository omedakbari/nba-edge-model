"""Combine saved player/game markets into a probability-first singles/stack report.

Payouts are indicative products of single prices, never executable entry quotes.
No independence assumption or invented probability bounds are used for approval.
"""
import argparse
from itertools import combinations
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.betting.eligibility import conservative_joint, CSB_JOINT_MIN
from src.betting.ev import american_to_decimal
from src.utils.slate import slate_today, utc_now, revision_path, latest_revision


def candidates(player_payload, game_payload, min_probability=.70):
    rows = []
    for r in player_payload.get('picks', []):
        rows.append(dict(kind='player', label=f"{r['player_name']} {r['stat']} {r['direction']} {r['line']}",
                         p=r.get('prob'), decimal=r.get('decimal_odds'), game_id=r.get('game_id'),
                         teams=sorted([r['team'], r['opp']]), book=r.get('book'),
                         approval=r.get('approval', {}), lower_bound=r.get('validated_lower_bound'),
                         source=r))
    for r in game_payload.get('picks', []):
        # Spread gaps and total projections are not calibrated hit probabilities.
        # Keep those markets visible as coverage, without manufacturing a P(hit).
        if r.get('insufficient_history'):
            continue
        for side in ('home', 'away'):
            odds = r.get(f'{side}_ml')
            if odds is None or r.get('model_wp') is None:
                continue
            try:
                decimal = american_to_decimal(float(odds))
            except ValueError:
                continue
            rows.append(dict(kind='game', label=f"{r[side]} moneyline",
                             p=r['model_wp'] if side == 'home' else 1-r['model_wp'],
                             decimal=decimal, game_id=r.get('game_id'), teams=sorted([r['home'], r['away']]),
                             book=r.get('odds_provider'), approval=dict(approved=False,
                             reasons=r.get('approval_reasons', ['Game probability/quote validation required'])),
                             lower_bound=None, source=r))
    valid = [r for r in rows if isinstance(r['p'], (int, float)) and math.isfinite(r['p'])
             and min_probability <= r['p'] <= 1 and isinstance(r['decimal'], (int, float))
             and math.isfinite(r['decimal']) and r['decimal'] > 1]
    for r in valid:
        r['ev'] = r['p'] * r['decimal'] - 1
    return sorted([r for r in valid if r['ev'] >= .04], key=lambda r: (-r['p'], -r['ev']))


def build_report(players, games, day, min_probability=.70):
    singles = candidates(players, games, min_probability)
    stacks = []
    # A bounded shortlist avoids combinatorial growth. One leg per game and one
    # sportsbook avoid known overlap and incompatible best prices across books.
    for size in (2, 3):
        for legs in combinations(singles[:30], size):
            ids = [r['game_id'] for r in legs]
            books = [r['book'] for r in legs]
            if not all(ids) or len(set(ids)) != size or not all(books) or len(set(books)) != 1:
                continue
            if any(set(a['teams']) & set(b['teams']) for a, b in combinations(legs, 2)):
                continue
            joint = conservative_joint([r['lower_bound'] for r in legs])
            payout = math.prod(r['decimal'] for r in legs)
            reasons = ['Executable combined payout and current quote review required']
            if any(r['approval'].get('approved') is not True for r in legs):
                reasons.append('One or more legs lack approval')
            if joint is None:
                reasons.append('Validated simultaneous probability bounds unavailable')
            elif joint < CSB_JOINT_MIN:
                reasons.append('Conservative joint hit probability below policy')
            elif joint * payout - 1 < .04:
                reasons.append('Conservative expected return below policy')
            stacks.append(dict(legs=[r['label'] for r in legs], book=books[0],
                               indicative_gross_multiplier=payout, conservative_joint=joint,
                               approved=False, stake_pct=0, reasons=reasons))
    stacks.sort(key=lambda s: (-(s['conservative_joint'] or 0), len(s['legs']),
                               -s['indicative_gross_multiplier']))
    return dict(date=day, generated_at=utc_now(), minimum_leg_probability=min_probability,
                status='REVIEW' if singles else 'PASS', approved_stacks=0,
                note='Model estimates are not guarantees. Stacking increases the chance of losing. '
                     'Single-price products are indicative, not quoted stack payouts.',
                coverage=dict(player_markets=len(players.get('picks', [])),
                              games=len(games.get('picks', [])),
                              spread_total='Scanned by team pipeline; excluded from stacks until hit probabilities and prices are validated'),
                singles=singles, stacks=stacks[:10])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', default=slate_today().isoformat())
    args = parser.parse_args()
    payloads = []
    for folder in ('market_picks', 'team_picks'):
        path = latest_revision(ROOT / 'data' / folder / f'{args.date}.json')
        payloads.append(json.loads(path.read_text()) if path.exists() else {})
    from src.betting.quote_quality import anchor_issues
    now = utc_now()
    payloads[0]['picks'] = [p for p in payloads[0].get('picks', [])
                            if not anchor_issues(p.get('quote') or {}, now)]
    # Team scoreboard lines do not yet have verified provider update times.
    payloads[1]['picks'] = [p for p in payloads[1].get('picks', []) if p.get('approved') is True]
    report = build_report(*payloads, args.date)
    folder = ROOT / 'data/prop_stacks'
    folder.mkdir(parents=True, exist_ok=True)
    path = revision_path(folder / f'{args.date}.json')
    path.write_text(json.dumps(report, indent=2, allow_nan=False))
    print(f"\n  PLAYER + GAME PROP STACK SCREEN — {report['status']}")
    print(report['note'])
    for r in report['singles'][:10]:
        print(f"  {r['label']}: estimated P {r['p']:.1%}, EV {r['ev']:+.1%}; REVIEW")
    print(f"  {len(report['stacks'])} indicative stacks; 0 approved. {path}")
    if not report['singles']:
        print('  PASS: no eligible higher-probability, positive-value candidates in saved scans.')


if __name__ == '__main__':
    main()
