"""Target population, feature parity, absence timing and live-source regressions."""
import sys
import sqlite3
from pathlib import Path
from datetime import datetime,timezone

import numpy as np
import pandas as pd
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'scripts')]
from src.features.population import appearance_features,appearance_prediction_features,pregame_rotation,settlement_outcome
from src.models.role_context import role_estimate,eligible_absences
from src.models.team_ratings import TeamRatingEngine
from src.data import espn_props as EP,live_evidence as L
import run_daily


def logs_fixture():
    records=[]
    for i in range(12):
        for pid,team,opp,home,mins,pts in [(1,'AAA','BBB',1,30,20),(2,'BBB','AAA',0,30,15),
                                         (3,'AAA','BBB',1,6,4)]:
            records.append(dict(player_id=pid,game_id=str(i),game_date=f'2025-01-{i+1:02d}',
                 season='2024-25',season_type='Regular Season',team_abbr=team,opp_abbr=opp,
                 is_home=home,wl='W' if home else 'L',full_name=str(pid),
                 min=5 if pid==1 and i==11 else mins,pts=pts,reb=4,ast=3,stl=1,blk=1,
                 fg3m=2,pra=pts+7,pts_reb=pts+4,pts_ast=pts+3,reb_ast=7,
                 fga=pts/2,fg3a=4,fta=2,tov=2,pf=2))
    return pd.DataFrame(records)


def test_pregame_rotation_keeps_later_minutes_cut_and_full_bench_totals():
    logs=logs_fixture()
    d=appearance_features(logs)
    cut=d[(d.player_id==1)&(d.game_id=='11')].iloc[0]
    assert cut['min']==5 and cut.min_L10==30
    assert pregame_rotation(d).loc[cut.name]
    # Training defensive totals include the 6-minute bench player's points.
    assert cut.pts_allowed_L15==24
    opponent=d[(d.player_id==2)&(d.game_id=='11')].iloc[0]
    assert opponent.pts_allowed_L15==15
    assert len(d)==len(logs)


def test_full_population_inference_matches_historical_features_without_current_outcome():
    logs=logs_fixture();day='2025-01-12'
    train=appearance_features(logs)
    ctx=dict(player_id=1,game_id='11',team_abbr='AAA',opp_abbr='BBB',is_home=1)
    infer=appearance_prediction_features(logs,[ctx],day,'2024-25').iloc[0]
    actual=train[(train.player_id==1)&(train.game_id=='11')].iloc[0]
    fields=['pts_L5','pts_L10','min_L10','min_std_L10','low_min_count_L5','season_game_num',
            'days_rest','is_home','pace_L15','own_pace_L15','pts_allowed_L15','ast_allowed_L15']
    np.testing.assert_allclose(infer[fields].astype(float),actual[fields].astype(float),equal_nan=True)
    assert pd.isna(infer['min']) and pd.isna(infer['pts'])
    changed=logs.copy();changed.loc[changed.game_date>=day,['min','pts','fga']]=999
    again=appearance_prediction_features(changed,[ctx],day,'2024-25').iloc[0]
    np.testing.assert_allclose(infer[fields].astype(float),again[fields].astype(float),equal_nan=True)


def test_dnp_cannot_be_an_under_win():
    assert settlement_outcome(0,22.5,'under',participated=False)=='void'
    assert settlement_outcome(0,22.5,'under',participated=False,dnp_rule='custom')=='review'
    assert settlement_outcome(0,22.5,'under',participated=True)=='hit'
    assert settlement_outcome(23,23,'under',participated=True)=='push'


def absence(**overrides):
    return dict(dict(player_id=9,team='AAA',game_id='10',status='out',source_url='https://official.nba.com/report',
                     published_at='2025-01-11T18:00:00Z',start_time='2025-01-11T23:00:00Z'),**overrides)


def test_role_evidence_must_be_pre_tip_pre_forecast_identity_matched_and_not_contradicted():
    logs=logs_fixture()
    a=absence()
    r=role_estimate(logs,1,{9},'2025-01-12',[a]);assert r['evidence_games']==1
    assert not r['adjustment_validated']
    for bad in [absence(published_at='2025-01-12T01:00:00Z'),absence(team='BBB'),
                absence(start_time='2025-01-10T23:00:00Z'),absence(source_url=''),
                absence(status='questionable')]:
        assert role_estimate(logs,1,{9},'2025-01-12',[bad])['evidence_games']==0
    assert role_estimate(logs,1,{9,8},'2025-01-12',[a])['evidence_games']==0
    contradiction=pd.concat([logs,pd.DataFrame([dict(logs.iloc[0],player_id=9,game_id='10',
                                                       game_date='2025-01-11')])],ignore_index=True)
    assert role_estimate(contradiction,1,{9},'2025-01-12',[a])['evidence_games']==0
    with pytest.raises(ValueError):eligible_absences([a],9,'AAA','2025-01-11')
    assert len(eligible_absences([a],9,'AAA','2025-01-11T20:00:00Z'))==1
    assert not eligible_absences([a],9,'AAA','2025-01-12T00:00:00Z')
    available=absence(status='available',published_at='2025-01-11T19:00:00Z')
    assert not eligible_absences([a,available],9,'AAA','2025-01-11T20:00:00Z')
    assert role_estimate(logs,1,{9},'2025-01-12',[a,available])['evidence_games']==0


def test_reconstructed_team_rating_ignores_mutable_or_future_aggregate():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE game_logs (game_id,game_date,season,season_type,team_abbr,opp_abbr,is_home,pts,wl)')
    c.executemany('INSERT INTO game_logs VALUES (?,?,?,?,?,?,?,?,?)',[
        ('1','2025-01-01','2024-25','Regular Season','AAA','BBB',1,100,'W'),
        ('1','2025-01-01','2024-25','Regular Season','BBB','AAA',0,90,'L'),
        ('2','2025-01-03','2024-25','Regular Season','AAA','BBB',1,999,'W'),
        ('2','2025-01-03','2024-25','Regular Season','BBB','AAA',0,0,'L')])
    engine=TeamRatingEngine.__new__(TeamRatingEngine)
    engine.conn=c;engine.aggregate_mode='game_logs';engine.through_date='2025-01-02'
    assert engine._load_season_ratings('2024-25')=={'AAA':10.,'BBB':-10.}


def test_explicit_quote_side_evidence_is_distinct_from_inferred_order(monkeypatch):
    props=[dict(type={'name':'Total Points'},athlete={'$ref':'https://example/athletes/123'},
                open={side:{'decimal':1.9},'target':{'value':22.5}},
                current={side:{'decimal':1.9},'target':{'value':22.5}},
                lastUpdated='2026-10-25T20:00:00Z') for side in ['over','under']]
    def get(s,url):return {'items':[{'provider':{'id':'1','name':'Book'}}]} if 'propBets' not in url else {'items':props}
    monkeypatch.setattr(EP,'_get',get)
    rows=EP._fetch_event(None,'event','2026-10-25')
    assert EP.no_vig_consensus(rows,True)[('123','pts')]['side_mapping_verified']
    assert not EP.no_vig_consensus([tuple(r) for r in rows],True)[('123','pts')]['side_mapping_verified']
    props[0]['current']={'under':{'decimal':1.9},'target':{'value':22.5}}
    assert len(EP._fetch_event(None,'event','2026-10-25'))==1
    assert EP._side({'over':{},'under':{}})[0] is None


def test_network_failure_is_not_empty_slate_and_daily_never_mutates_on_failure(monkeypatch,tmp_path):
    def fail(*a):raise OSError('resolver unavailable')
    monkeypatch.setattr(L.socket,'getaddrinfo',fail)
    monkeypatch.setattr(L.requests,'get',lambda *a,**kw:pytest.fail('DNS already failed'))
    report=L.fetch_snapshot('https://example.com/api')
    assert not report['ok'] and report['error_kind']=='dns'
    monkeypatch.setattr(run_daily,'collect',lambda *a:({'live_api_ready':False,'responses':{'a':report}},tmp_path))
    monkeypatch.setattr(run_daily.subprocess,'run',lambda *a,**kw:pytest.fail('no grading on network failure'))
    assert run_daily.main()==2


def test_scheduled_status_never_revives_started_event():
    event=dict(date='2026-10-25T21:00:00Z',status={'type':{'state':'pre'}})
    assert not L.pregame_event(event,datetime(2026,10,25,22,tzinfo=timezone.utc))
    assert L.pregame_event(event,datetime(2026,10,25,20,tzinfo=timezone.utc))
