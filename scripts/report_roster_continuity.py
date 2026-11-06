"""Report/store all 30 teams' returning minutes; archive fresh rosters when possible."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from config.settings import DB_PATH,CURRENT_SEASON,ROSTER_CONTINUITY_THRESHOLD
from src.data.roster_continuity import compute
from src.data.context_archive import archive,ESPN_TEAM_IDS
from src.data.live_evidence import fetch_snapshot
from src.utils.slate import utc_now,slate_today,revision_path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh',action='store_true');parser.add_argument('--as-of');parser.add_argument('--season',default=CURRENT_SEASON)
    args=parser.parse_args()
    if args.refresh and args.as_of:parser.error('Historical as-of reports never refresh current rosters')
    refresh_error=None
    if args.refresh:
        probe=fetch_snapshot('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams?limit=100')
        if not probe['ok']: refresh_error=probe
        else:
            def fetch(tid):
                r=fetch_snapshot(f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster')
                archive('espn_roster',tid,r.get('payload',{'error':r.get('error')}),r['captured_at'],source=r['source_url'],ok=r['ok'])
                return r
            with ThreadPoolExecutor(max_workers=2) as pool: responses=list(pool.map(fetch,ESPN_TEAM_IDS.values()))
            if any(not r['ok'] for r in responses):refresh_error={'error':'Some roster refreshes failed','responses':responses}
    cutoff=args.as_of or utc_now()
    with sqlite3.connect(f'file:{DB_PATH}?mode=ro',uri=True) as conn:
        teams=compute(conn,args.season,cutoff,threshold=ROSTER_CONTINUITY_THRESHOLD,data=ROOT/'data')
    payload=dict(as_of=cutoff,teams=teams,refresh_error=refresh_error,ratings_adjusted=False,approved=False,maximum_entry_cost=0)
    directory=ROOT/'data/roster_continuity';directory.mkdir(parents=True,exist_ok=True)
    path=revision_path(directory/f'{slate_today().isoformat()}.json');path.write_text(json.dumps(payload,indent=2))
    for team,row in teams.items():
        share=row['returning_minutes_share'];value=f'{share:.1%}' if share is not None else 'UNKNOWN'
        print(f"{team}: {value} {row['flag'] or ''} | {row['roster_snapshot_status']} | {row['roster_captured_at']}")
    print(path)
    if refresh_error: print('Live refresh failed; values are explicitly dated archived observations, not verified current rosters.')
    return 1 if refresh_error else 0


if __name__=='__main__':raise SystemExit(main())
