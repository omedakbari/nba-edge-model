"""Returning prior-season team minutes. Flags only; never adjusts ratings."""
from collections import defaultdict
import json
from pathlib import Path
import re
import unicodedata
from src.data.context_archive import ESPN_TEAM_IDS, instant
from src.utils.slate import PACIFIC


def name_key(name):
    value=unicodedata.normalize('NFKD',name).encode('ascii','ignore').decode().lower()
    value=re.sub(r'\s+(jr\.?|sr\.?|ii|iii|iv)$','',value.strip())
    return re.sub('[^a-z0-9]','',value)


def archived_rosters(data, cutoff):
    """Latest source observation <= decision time, with its age always exposed."""
    records={}; cutoff=instant(cutoff)
    def consider(tid,record):
        if not record.get('ok') or 'athletes' not in record.get('payload',{}): return
        time=instant(record['captured_at'])
        if time<=cutoff and (tid not in records or time>instant(records[tid]['captured_at'])):
            records[tid]=record
    for path in (data/'live_snapshots').glob('*/live_evidence.json'):
        payload=json.loads(path.read_text())
        for tid,record in payload.get('rosters',{}).items(): consider(tid,record)
    for path in (data/'asof_context/espn_roster').glob('*/*.json'):
        record=json.loads(path.read_text());consider(record['identity'],record)
    return {abbr:records[tid] for abbr,tid in ESPN_TEAM_IDS.items() if tid in records}


def compute(conn, season, as_of, *, threshold=.6, rosters=None, data=None):
    if not 0<=threshold<=1: raise ValueError('Continuity threshold must be in [0,1]')
    year=int(season.split('-')[0]); prior=f'{year-1}-{str(year)[-2:]}'
    cutoff=instant(as_of)
    rosters=archived_rosters(Path(data),as_of) if rosters is None else rosters
    names=defaultdict(set)
    for pid,name in conn.execute('SELECT player_id,full_name FROM players'): names[name_key(name)].add(pid)
    minutes=defaultdict(dict)
    for team,pid,total in conn.execute("SELECT team_abbr,player_id,SUM(min) FROM game_logs WHERE season=? AND season_type='Regular Season' AND game_date<? GROUP BY team_abbr,player_id",(prior,cutoff.astimezone(PACIFIC).date().isoformat())):
        minutes[team][pid]=float(total or 0)
    result={}
    for team in sorted(ESPN_TEAM_IDS):
        observed=rosters.get(team); denom=sum(minutes[team].values())
        ids=set();unmatched=[]
        if observed:
            for athlete in observed['payload']['athletes']:
                key=name_key(athlete.get('fullName') or athlete.get('displayName') or '')
                matches=names.get(key,set())
                if len(matches)==1: ids.update(matches)
                else: unmatched.append(athlete.get('displayName',key))
        numerator=sum(value for pid,value in minutes[team].items() if pid in ids)
        share=numerator/denom if observed and denom>0 else None
        captured=observed.get('captured_at') if observed else None
        same_day=bool(captured and instant(captured).astimezone(PACIFIC).date()==cutoff.astimezone(PACIFIC).date())
        result[team]=dict(returning_minutes_share=share,returning_minutes=numerator if observed else None,
            prior_season_minutes=denom,prior_season=prior,as_of=as_of,roster_captured_at=captured,
            roster_source=observed.get('source_url') or observed.get('source') if observed else None,
            roster_snapshot_status='same_day_observation' if same_day else 'older_observation' if observed else 'unavailable',
            threshold=threshold,flag='UNKNOWN_CONTINUITY' if share is None else 'LOW_CONTINUITY' if share<threshold else None,
            unmatched_roster_names=unmatched,ratings_adjusted=False)
    return result
