"""Returning-minutes flags are as-of bounded and never change rating outputs."""
from pathlib import Path
import json
import sqlite3
import sys
from types import SimpleNamespace
import pandas as pd
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'scripts')]
from src.data.roster_continuity import compute, archived_rosters
import generate_todays_team_picks as T


def fixture():
    conn=sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE players (player_id,full_name)')
    conn.executemany('INSERT INTO players VALUES (?,?)',[(1,'Player One'),(2,'Player Two Jr.')])
    conn.execute('CREATE TABLE game_logs (team_abbr,player_id,min,season,season_type,game_date)')
    conn.executemany('INSERT INTO game_logs VALUES (?,?,?,?,?,?)',[
        ('PHI',1,40,'2025-26','Regular Season','2026-04-01'),('PHI',2,60,'2025-26','Regular Season','2026-04-01'),
        ('PHI',1,999,'2025-26','Playoffs','2026-04-20'),('PHI',1,999,'2026-27','Regular Season','2026-11-01')])
    return conn


def test_prior_regular_minutes_denominator_and_flags():
    roster={'PHI':dict(ok=True,captured_at='2026-11-06T18:00Z',payload={'athletes':[{'fullName':'Player One'}]})}
    r=compute(fixture(),'2026-27','2026-11-06T19:00Z',rosters=roster)
    assert len(r)==30 and r['PHI']['returning_minutes_share']==.4
    assert r['PHI']['flag']=='LOW_CONTINUITY' and not r['PHI']['ratings_adjusted']
    assert r['BOS']['returning_minutes_share'] is None and r['BOS']['flag']=='UNKNOWN_CONTINUITY'


def test_suffix_alias_and_configurable_boundary():
    roster={'PHI':dict(ok=True,captured_at='2026-11-05T18:00Z',payload={'athletes':[{'fullName':'Player Two'}]})}
    r=compute(fixture(),'2026-27','2026-11-06T19:00Z',rosters=roster)
    assert r['PHI']['returning_minutes_share']==.6 and r['PHI']['flag'] is None
    assert r['PHI']['roster_snapshot_status']=='older_observation'
    assert compute(fixture(),'2026-27','2026-11-06T19:00Z',threshold=.7,rosters=roster)['PHI']['flag']=='LOW_CONTINUITY'


def test_archive_ignores_future_roster(tmp_path):
    directory=tmp_path/'live_snapshots/run';directory.mkdir(parents=True)
    record=dict(ok=True,captured_at='2026-11-07T18:00Z',payload={'athletes':[]})
    (directory/'live_evidence.json').write_text(json.dumps({'rosters':{'20':record}}))
    assert archived_rosters(tmp_path,'2026-11-06T19:00Z')=={}


def test_continuity_does_not_change_predictions():
    engine=SimpleNamespace(_games=pd.DataFrame([(t,i) for t in ['PHI','CLE'] for i in range(12)],columns=['team','game_id']),
        predict_spread=lambda *a,**kw:3,win_probability=lambda *a,**kw:.6,get_rating=lambda *a:0)
    game=dict(home='CLE',away='PHI',game_id='401',status='scheduled',home_full='Cleveland',away_full='Philadelphia',spread=2.5,
        home_implied=.45,away_implied=.6)
    before=T.analyze_game(game,engine,False,'2026-11-06')
    after=T.analyze_game(game,engine,False,'2026-11-06',roster_continuity={'PHI':{'flag':'LOW_CONTINUITY','returning_minutes_share':.4}})
    assert after['roster_continuity']['PHI']['flag']=='LOW_CONTINUITY'
    for key in ['model_spread','model_wp','sp_edge','tier','approved','stake_pct']: assert before[key]==after[key]
    assert not after['approved'] and after['stake_pct']==0
