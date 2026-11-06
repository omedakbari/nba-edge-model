"""Read-only same-book CLV for first-saved team/prop picks; per-day and cumulative."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.data.context_archive import instant


def report(data):
    closes = []
    for path in (data/'closing_snapshots').glob('*/*.json'):
        closes += [r for r in json.loads(path.read_text()).get('observations', [])
            if r.get('capture_verified') and r.get('freshness', {}).get('verified')
            and instant(r['captured_at']) < instant(r['start_time'])]
    rows, unmatched = [], 0
    for directory, kind in [('team_picks','team'), ('market_picks','prop')]:
        for path in (data/directory).glob('????-??-??.json'):
            payload = json.loads(path.read_text())
            for p in payload.get('picks', []):
                if kind=='team' and not p.get('spread_pick_side') and not p.get('ml_pick_side') and p.get('ou_tier') not in ('LEAN','STRONG'): continue
                book = p.get('odds_provider') if kind=='team' else p.get('book')
                event = p.get('game_id') if kind=='team' else (p.get('quote') or {}).get('event_id')
                matches = [r for r in closes if r['kind']==kind and r['book']==book and r['event_id']==event
                    and instant(r['captured_at']) >= instant(payload['generated_at'])]
                if kind=='prop':
                    matches = [r for r in matches if r['stat']==p['stat_key'] and str(r.get('athlete_id'))==str((p.get('quote') or {}).get('espn_athlete_id'))]
                if not matches: unmatched+=1; continue
                close = max(matches,key=lambda r:instant(r['captured_at'])); values=[]
                if kind=='team':
                    if p.get('spread_pick_side') and close.get('spread_home') is not None:
                        values.append(('spread',(p['market_spread']-close['spread_home'])*(1 if p['spread_pick_side']=='home' else -1)))
                    if p.get('ou_tier') in ('LEAN','STRONG') and close.get('total') is not None:
                        values.append(('total',(close['total']-p['over_under'])*(1 if p['ou_direction']=='OVER' else -1)))
                    if p.get('ml_pick_side') and close.get('home_ml') and close.get('away_ml'):
                        def imp(ml): return 100/(ml+100) if ml>0 else -ml/(100-ml)
                        h,a=imp(close['home_ml']),imp(close['away_ml']); side=p['ml_pick_side']
                        values.append(('moneyline_probability',(h if side=='home' else a)/(h+a)-imp(p['home_ml'] if side=='home' else p['away_ml'])))
                else:
                    values.append((p['stat_key']+'_line',(close['line']-p['line'])*(1 if p['direction'].upper()=='OVER' else -1)))
                    if close['line']==p['line']:
                        io,iu=1/close['over'],1/close['under']
                        prob=(io if p['direction'].upper()=='OVER' else iu)/(io+iu)
                        values.append((p['stat_key']+'_same_line_probability',prob-1/p['decimal_odds']))
                rows += [dict(date=payload['date'],market=m,book=book,event_id=event,clv=v) for m,v in values]
    groups=defaultdict(list)
    for r in rows:
        groups[(r['date'],r['market'])].append(r['clv']); groups[('cumulative',r['market'])].append(r['clv'])
    return dict(matched_values=rows,unmatched_picks=unmatched,
        summaries=[dict(date=d,market=m,n=len(v),mean_clv=sum(v)/len(v)) for (d,m),v in sorted(groups.items())],
        limitations=['Positive means better saved line/price; units grouped separately',
                    'First-saved selected picks only; verified same-book observations; no profitability claim'],approved=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data'); parser.add_argument('--output',type=Path)
    args=parser.parse_args(); text=json.dumps(report(args.data_dir),indent=2)
    if args.output: args.output.write_text(text)
    print(text)


if __name__=='__main__': main()
