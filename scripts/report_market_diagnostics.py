"""Score the first saved daily candidate set, split by model/stat/side/cohort.

Read-only against forecasts and DB. Missing participation stays pending. ROI
is hypothetical at saved prices; this report never certifies profitability.
"""
import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config.settings import DB_PATH
from src.betting.quote_quality import anchor_issues
from src.utils.slate import revision_path, utc_now, slate_today


def summarize(picks_dir, connection, before):
    groups = defaultdict(list)
    counts = defaultdict(int)
    for path in sorted(Path(picks_dir).glob('????-??-??.json')):
        payload = json.loads(path.read_text())
        if payload['date'] >= before:
            continue
        for pick in payload.get('picks', []):
            stat = pick['stat_key']
            if stat not in ('pts','reb','ast','fg3m','pra','reb_ast','pts_reb','pts_ast'):
                counts['unsupported'] += 1
                continue
            rows = connection.execute(f'SELECT {stat}, min, team_abbr, opp_abbr FROM game_logs '
                'WHERE player_id=? AND game_date=?', (pick['player_id'], payload['date'])).fetchall()
            if len(rows) != 1 or rows[0][2:] != (pick['team'],pick['opp']):
                counts['pending_or_identity_mismatch'] += 1
                continue
            actual, minutes, _, _ = rows[0]
            if not minutes or minutes <= 0:
                counts['nonparticipation'] += 1
                continue
            if actual == pick['line']:
                counts['pushes'] += 1
                continue
            try:
                p, dec = float(pick['prob']), float(pick['decimal_odds'])
                if not (0 < p < 1 and 1 < dec < float('inf')):
                    raise ValueError('Invalid estimate')
            except (KeyError, ValueError, TypeError):
                counts['invalid_estimate'] += 1
                continue
            hit = int((actual > pick['line']) == (pick['direction'].upper() == 'OVER'))
            quote = pick.get('quote') or {}
            quality = 'rejected' if anchor_issues(quote, payload.get('generated_at','')) else 'fresh_at_forecast'
            current = pick.get('current_season_games')
            cohort = 'unknown' if current is None else 'first_ten' if current < 10 else 'later'
            key = (pick.get('model_version','legacy'), stat, pick['direction'].upper(), cohort, quality)
            groups[key].append((payload['date'], p, hit, dec))
    result = []
    import math
    for key, rows in sorted(groups.items()):
        n = len(rows)
        result.append(dict(zip(['model_version','stat','side','cohort','quote_quality'],key),
            n=n, dates=len({r[0] for r in rows}), mean_probability=sum(r[1] for r in rows)/n,
            hit_rate=sum(r[2] for r in rows)/n,
            brier=sum((r[1]-r[2])**2 for r in rows)/n,
            log_loss=-sum(r[2]*math.log(r[1])+(1-r[2])*math.log(1-r[1]) for r in rows)/n,
            hypothetical_unit_roi=(sum(r[2]*r[3]-1 for r in rows)/n if key[-1]=='fresh_at_forecast' else None)))
    return dict(generated_at=utc_now(), before=before, groups=result, unscored=dict(counts),
        population='First saved daily selected candidates only; revisions excluded to prevent duplicate/tuned scoring',
        limitations=['Selected-candidate calibration does not measure the whole board',
            'Historical source-side mappings may be unverified',
            'ROI is hypothetical; no profitability approval or independence claim'], approved=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', default=slate_today().isoformat())
    args = parser.parse_args()
    with sqlite3.connect(f'file:{DB_PATH}?mode=ro',uri=True) as conn:
        report = summarize(ROOT/'data/market_picks', conn, args.before)
    output = ROOT/'data/market_diagnostics'
    output.mkdir(parents=True,exist_ok=True)
    path = revision_path(output/f'calibration-through-{args.before}.json')
    path.write_text(json.dumps(report,indent=2,allow_nan=False))
    print(f"Market diagnostics: {len(report['groups'])} scored model/stat/side/cohort/quote groups; {path}")


if __name__ == '__main__':
    main()
