"""Regression tests for confirmed October 25 feature and validation defects."""
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]),
                str(Path(__file__).resolve().parents[1] / 'scripts')]
import generate_todays_picks as P
import generate_todays_team_picks as T
import log_team_results as G
from src.betting import ev as E
from src.betting.eligibility import approval, conservative_joint
from src.data.espn_props import no_vig_consensus
from src.models.team_ratings import TeamRatingEngine
from src.utils.slate import revision_path


def test_inference_updates_orientation_game_and_both_paces(monkeypatch):
    historical = pd.DataFrame([dict(player_id=1,game_date='2026-10-23',min_L10=30,
                                    team_abbr='OLD',opp_abbr='OLDOPP',is_home=1,
                                    game_id='stale',pace_L15=99,own_pace_L15=88)])
    monkeypatch.setattr(P,'current_player_features',lambda *a:{1:{'min_L10':30}})
    monkeypatch.setattr(P,'get_opponent_def_stats',lambda *a:{'pts_allowed_L15':111})
    monkeypatch.setattr(P,'get_team_pace',lambda team,*a:{'pace_L15':100 if team=='ORL' else 110})
    ctx=dict(player_id=1,team_abbr='ORL',opp_abbr='NYK',is_home=0,game_id='new',matchup='ORL @ NYK')
    row=P.build_todays_features(historical,[ctx],'2026-10-25').iloc[0]
    assert row.pace_L15==110 and row.own_pace_L15==100
    assert row.game_id=='new' and row.is_home==0 and row.opp_abbr=='NYK'
    assert row.team_abbr=='ORL' and row.matchup=='ORL @ NYK'
    with pytest.raises(ValueError,match='Ambiguous'):
        P.build_todays_features(historical,[ctx,dict(ctx,team_abbr='OTHER')],'2026-10-25')


def test_missing_model_feature_fails_explicitly():
    with pytest.raises(ValueError,match='Missing inference features'):
        P.predict_stat_raw(pd.DataFrame({'x':[1]}),'pts',model_cache={
            'pts':dict(feature_cols=['x','missing'],model=None)})


@pytest.mark.parametrize('margin,market,side,actual,result',[
    (-8.3,1.5,'away',-2,'WIN'),(-8.3,1.5,'away',-1,'LOSS'),
    (-1.54,6.5,'home',-6,'WIN'),(11.17,-16.5,'away',16,'WIN'),
    (11.17,-16.5,'away',17,'LOSS'),(8,-8,'home',8,'PUSH')])
def test_handicap_edge_and_grading_agree(margin,market,side,actual,result):
    edge=T.spread_edge(margin,market)
    if edge:
        assert ('home' if edge>0 else 'away')==side
    assert G.grade_spread(side,market,actual)==result


def test_ats_ml_strength_are_separate_and_probabilities_unapproved():
    engine=SimpleNamespace(_games=pd.DataFrame([('PHI',i) for i in range(20)]+
                                              [('DET',i) for i in range(20)],columns=['team','game_id']),
                           predict_spread=lambda *a,**kw:-8.3,win_probability=lambda *a,**kw:.3057,
                           get_rating=lambda t:0)
    a=T.analyze_game(dict(home='PHI',away='DET',game_id='1',status='scheduled',
                          home_full='Philadelphia',away_full='Detroit',spread=1.5,
                          home_implied=.5192,away_implied=.5283),engine,False,'2026-10-25')
    assert a['expected_home_margin']==-8.3 and a['fair_home_handicap']==8.3
    assert a['fair_away_handicap']==-8.3 and a['spread_pick_team']=='DET'
    assert not a['approved'] and a['stake_pct']==0 and not a['probability_validated']
    assert T.pick_tier(4.96,None)=='LEAN' and T.pick_tier(None,.1379)=='STRONG'
    assert T.pick_tier(-5.33,None)=='LEAN'


@pytest.mark.parametrize('p,decimal',[(.5765,1.82),(.602,1.74),(.5685,1.83)])
def test_ev_pass_is_not_kea_pass(p,decimal):
    assert E.ev(p,decimal)>=.04
    assert 0<p-E.breakeven(decimal)<.03
    gate=approval({},p,decimal)
    assert not gate['approved'] and gate['stake_pct']==0
    assert 'KEA probability margin below policy' in gate['reasons']


def test_gate_requires_role_validation_and_fresh_pregame_evidence():
    now=datetime(2026,10,25,20,35,tzinfo=timezone.utc)
    evidence=dict(probability_validated=True,injury_role_reviewed=True,mapping_verified=True,
                  settlement_verified=True,captured_at='2026-10-25T20:34:00Z',
                  quote_updated_at='2026-10-25T20:33:00Z',
                  start_time='2026-10-25T23:00:00Z',status='scheduled')
    assert approval(evidence,.65,2,now=now)['approved']
    for update in [dict(injury_role_reviewed=False),dict(probability_validated=False),
                   dict(quote_updated_at='2026-10-25T18:00:00Z'),
                   dict(captured_at='2026-10-25T18:00:00Z'),dict(status='in_progress'),
                   dict(start_time='2026-10-25T20:00:00Z')]:
        assert not approval(dict(evidence,**update),.65,2,now=now)['approved']


def quote(side, *, event='1',updated='2026-10-25T20:00:00Z',current_line=20.5,current_price=1.9):
    return (event,'2026-10-25','Book','12','pts',side,20.5,1.9,current_line,current_price,updated)


def test_no_vig_requires_paired_book_event_line_time_and_price():
    paired=[quote('over'),quote('under')]
    c=no_vig_consensus(paired,True)[('12','pts')]
    assert c['p_over']==.5 and c['event_id']=='1' and c['best_under_book']=='Book'
    for rows in [[quote('over'),quote('under',event='2')],
                 [quote('over'),quote('under',updated='other')],
                 [quote('over'),quote('under',current_line=21.5)],
                 [quote('over'),quote('under',current_price=None)],
                 [quote('over'),quote('under',current_price=float('nan'))],
                 paired+[quote('under')]]:
        assert not no_vig_consensus(rows,True)


@pytest.mark.parametrize('price',['nan','inf','x0','x1','0c','100c','-000'])
def test_invalid_prices_rejected(price):
    with pytest.raises(ValueError):
        E.parse_price(price)


def test_contract_economics_use_actual_cost_and_joint_bound_never_independence():
    assert E.breakeven(E.all_in_decimal(53.75,100))==pytest.approx(.5375)
    assert E.ev(.6,E.all_in_decimal(53.75,100))==pytest.approx(.6/.5375-1)
    assert conservative_joint([.9,.9])==pytest.approx(.8)
    assert conservative_joint([.6,.6])==pytest.approx(.2)
    assert conservative_joint([.9,None]) is None


def test_revision_writes_preserve_first_forecast(tmp_path):
    path=tmp_path/'2026-10-25.json';path.write_text('original')
    revision=revision_path(path);revision.write_text('corrected')
    assert path.read_text()=='original' and revision!=path


def test_current_season_aggregate_future_snapshot_is_not_historical_input():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE team_stats (team_abbr,season,net_rtg,pace,updated_at)')
    c.execute("INSERT INTO team_stats VALUES ('BOS','2025-26',10,100,'2026-06-13T12:00:00Z')")
    e=TeamRatingEngine.__new__(TeamRatingEngine)
    e.conn=c;e.season='2025-26';e.through_date='2025-11-01'
    assert e._load_season_ratings('2025-26')=={}


def test_kalshi_exact_threshold_and_grading_identity():
    import find_bets as F
    import log_pick_results as L
    market=dict(yes_sub_title='Franz Wagner: 23+ points',floor_strike=22.5,strike_type='greater')
    assert F.kalshi_prop_line(market,'pts')==22.5
    assert F.kalshi_prop_line(dict(market,floor_strike=23),'pts') is None
    assert F.kalshi_prop_line(market,'reb') is None
    assert F.kalshi_prop_line(dict(market,yes_sub_title='ambiguous'),'pts') is None
    pick=dict(team='ORL',opp='NYK',game_id='401909863')
    assert L.actual_mapping_matches(pick,dict(team_abbr='ORL',opp_abbr='NYK',game_id='0022600101'))
    assert not L.actual_mapping_matches(pick,dict(team_abbr='ORL',opp_abbr='ATL',game_id='0022600085'))
    assert not L.actual_mapping_matches(pick,dict(team_abbr='ORL',opp_abbr='NYK',game_id='401909999'))
    assert L.grade_pick(22,22.5,'UNDER')==('hit',1)
    assert L.grade_pick(23,22.5,'UNDER')==('miss',0)
