"""Prospective boundaries, immutable contexts and matched CLV."""
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'scripts')]
from src.data import context_archive as C
from src.data.closing import observations, in_window
from src.data.errors import SourceError
import generate_todays_picks as P
import capture_closing_odds as CAP
from report_clv import report

NOW=datetime(2026,11,6,23,55,tzinfo=timezone.utc)


def game(**changes):
    return dict(dict(game_id='401',status='scheduled',start_time='2026-11-07T00:00:00Z',
        captured_at='2026-11-06T23:55:00Z',http_captured_at='2026-11-06T23:55:00Z',
        http_date='Fri, 06 Nov 2026 23:55:00 GMT',source_url='https://example.com',
        odds_provider='Book',spread=-2.5,over_under=220,home_ml=-130,away_ml=110),**changes)


@pytest.mark.parametrize('start,expected',[('2026-11-07T00:00Z',True),('2026-11-07T00:06Z',False),('2026-11-06T23:55Z',False),('2026-11-06T23:54Z',False)])
def test_window(start,expected):
    assert in_window(game(start_time=start),NOW)==expected


@pytest.mark.parametrize('change',[{'odds_provider':'Other'},{'http_date':None},{'status':'in_progress'},
    {'captured_at':'2026-11-06T23:49Z','http_captured_at':'2026-11-06T23:49Z','http_date':'Fri, 06 Nov 2026 23:49:00 GMT'}])
def test_closing_rejects_mismatch_unverified_started_or_outside_capture_window(change):
    result,_=observations([game(**change)],{},[{'game_id':'401','odds_provider':'Book'}],[],NOW)
    assert result==[]


def test_same_book_closing_records_freshness_and_zero_stake():
    result,_=observations([game()],{},[{'game_id':'401','odds_provider':'Book'}],[],NOW)
    assert len(result)==1 and result[0]['freshness']['verified']
    assert result[0]['stake']==0 and not result[0]['approved']


def test_context_archive_selects_only_at_or_before_cutoff(tmp_path):
    for day,ids in [('05',[1]),('06',[1,2]),('07',[2,3])]:
        C.archive('nba_roster','1',{'rows':[{'PLAYER_ID':i} for i in ids]},f'2026-11-{day}T18:00:00Z',source='test',season='2026-27',root=tmp_path)
    result=C.latest('nba_roster','1','2026-11-06T19:00:00Z',season='2026-27',root=tmp_path)
    assert result['payload']['rows']==[{'PLAYER_ID':1},{'PLAYER_ID':2}]
    with pytest.raises(SourceError): C.latest('nba_roster','1','2026-11-04T19:00:00Z',root=tmp_path)


def test_historical_roster_never_calls_live_api(monkeypatch,tmp_path):
    monkeypatch.setattr(C,'PROJECT_ROOT',tmp_path)
    C.archive('nba_roster','1',{'rows':[{'PLAYER_ID':7}]},'2026-11-04T18:00:00Z',source='test',season='2026-27')
    assert P.get_roster_player_ids(1,'2026-27','2026-11-05')==[7]
    with pytest.raises(SourceError): P.get_roster_player_ids(2,'2026-27','2026-11-05')


def test_historical_injuries_use_saved_context(monkeypatch,tmp_path):
    monkeypatch.setattr(C,'PROJECT_ROOT',tmp_path)
    C.archive('injuries','league',{'injuries':[]},'2026-11-04T18:00:00Z',source='test')
    monkeypatch.setattr(P.requests,'get',lambda *a,**kw:pytest.fail('no live historical fetch'))
    assert P.fetch_injury_report(as_of='2026-11-05T18:00:00Z')=={}


def test_closing_command_forbids_backfill(monkeypatch):
    monkeypatch.setattr(sys,'argv',['capture','--date','2026-11-05'])
    with pytest.raises(SystemExit) as e: CAP.main()
    assert e.value.code!=0


def test_clv_matches_same_book_and_signs_and_ignores_revisions(tmp_path):
    for name in ['team_picks','market_picks','closing_snapshots/2026-11-06']:(tmp_path/name).mkdir(parents=True)
    payload={'date':'2026-11-06','generated_at':'2026-11-06T18:00Z','picks':[
        dict(game_id='401',odds_provider='Book',spread_pick_side='home',market_spread=-1.5)]}
    (tmp_path/'team_picks/2026-11-06.json').write_text(json.dumps(payload))
    (tmp_path/'team_picks/2026-11-06.revision-1.json').write_text(json.dumps(payload))
    close=observations([game()],{},payload['picks'],[],NOW)[0][0]
    (tmp_path/'closing_snapshots/2026-11-06/one.json').write_text(json.dumps({'observations':[dict(close,book='Other')]}))
    assert report(tmp_path)['unmatched_picks']==1
    (tmp_path/'closing_snapshots/2026-11-06/one.json').write_text(json.dumps({'observations':[close]}))
    r=report(tmp_path)
    assert len(r['matched_values'])==1 and r['matched_values'][0]['clv']==1
    assert len(r['summaries'])==2


def test_prop_clv_same_book_and_threshold(tmp_path):
    (tmp_path/'market_picks').mkdir();(tmp_path/'closing_snapshots/day').mkdir(parents=True)
    pick=dict(player_name='Player',stat_key='pts',book='Book',direction='OVER',line=20.5,decimal_odds=1.9,
        quote={'event_id':'401','espn_athlete_id':'9'})
    (tmp_path/'market_picks/2026-11-06.json').write_text(json.dumps({'date':'2026-11-06','generated_at':'2026-11-06T18:00Z','picks':[pick]}))
    close=dict(kind='prop',event_id='401',athlete_id='9',stat='pts',book='Book',line=21.5,over=1.9,under=1.9,
        captured_at='2026-11-06T23:55Z',start_time='2026-11-07T00:00Z',capture_verified=True,freshness={'verified':True})
    (tmp_path/'closing_snapshots/day/one.json').write_text(json.dumps({'observations':[close]}))
    assert report(tmp_path)['matched_values'][0]['clv']==1
