"""Quote contamination, corrected ingestion and research inference regressions."""
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import numpy as np
import pytest
sys.path[:0] = [str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'scripts')]
from src.data.espn_props import no_vig_consensus
from src.betting.quote_quality import anchor_issues
from src.data.supplemental_quotes import merge_quotes
from src.ingestion import game_logs as G
from src.models import appearance_daily as A
from src.utils.slate import latest_revision
import report_market_diagnostics as R
import generate_todays_team_picks as T

NOW = '2026-11-02T20:12:00+00:00'


def paired(updated='2026-11-02T20:04Z'):
    return [('401','2026-11-02','Book','123','pts',side,20.5,price,20.5,price,updated)
            for side,price in [('over',1.9),('under',1.9)]]


@pytest.mark.parametrize('stamp', ['2026-11-02T21:25Z','2026-11-02T11:51Z','garbage','2026-11-02T20:04'])
def test_invalid_provider_times_cannot_enter_consensus(stamp):
    rejected = []
    assert not no_vig_consensus(paired(stamp),True,captured_at=NOW,rejected=rejected)
    assert len(rejected)==2
    # Historical opening data is deliberately independent of the live freshness rule.
    assert no_vig_consensus(paired(stamp),False,captured_at=NOW)


def quote():
    return dict(no_vig_consensus(paired(),True)[('123','pts')], captured_at=NOW,
                start_time='2026-11-03T00:30:00Z',status='scheduled',teams=['AAA','BBB'])


def test_future_second_book_cannot_hide_behind_older_quote():
    q=quote()
    q['paired_quotes'].append(dict(updated_at='2026-11-02T23:01Z'))
    assert 'future provider timestamp' in anchor_issues(q,NOW)


def test_supplemental_books_require_matching_identity_and_fresh_pairs():
    q=quote();book={('Player','pts'):q}
    record=dict(player='Player',stat='pts',book='Second',line=20.5,over=1.85,under=1.95,
                updated_at='2026-11-02T20:05Z',captured_at=NOW,start_time=q['start_time'],
                teams=q['teams'],event_id='401',source_url='https://example.com/quote',side_mapping_verified=True)
    merged,rejected=merge_quotes(book,[record],NOW)
    assert not rejected and merged[('Player','pts')]['n_books']==2
    assert merged[('Player','pts')]['anchor_kind']=='multi-book'
    assert not merged[('Player','pts')]['side_mapping_verified'] # original inferred sides persist
    for changes in [dict(event_id='wrong'),dict(updated_at='2026-11-03T01:00Z'),dict(book='Book')]:
        merged,rejected=merge_quotes(book,[dict(record,**changes)],NOW)
        assert rejected and merged[('Player','pts')]['n_books']==1


def bulk_frame(points=12):
    return pd.DataFrame([dict(PLAYER_ID=1,PLAYER_NAME='Player',GAME_ID='0022600001',
        GAME_DATE='2026-11-01',MATCHUP='AAA vs. BBB',MIN=30,PTS=points,REB=3,AST=2,
        FGA=10,FTA=2,TOV=1)])


def test_bulk_refresh_applies_corrections_and_counts_only_new_rows(monkeypatch,tmp_path):
    db=tmp_path/'nba.db'
    schema=(Path(__file__).resolve().parents[1]/'data/schema.sql').read_text()
    with sqlite3.connect(db) as c:c.executescript(schema)
    monkeypatch.setattr(G,'get_connection',lambda:sqlite3.connect(db))
    response=bulk_frame()
    calls=[]
    def fetch(*args):
        calls.append(args)
        return response.copy()
    monkeypatch.setattr(G,'fetch_bulk_game_logs',fetch)
    monkeypatch.setattr(G,'get_active_players',lambda *a:pytest.fail('bulk refresh must not fetch every player'))
    first=G.ingest_season('2026-27',force_update=True)
    assert first['inserted']==1 and first['updated']==0
    second=G.ingest_season('2026-27',force_update=True)
    assert second['inserted']==second['updated']==0
    response.loc[0,'PTS']=14
    third=G.ingest_season('2026-27',force_update=True)
    assert third['inserted']==0 and third['updated']==1
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT pts,pra FROM game_logs').fetchone()==(14,19)
    assert calls[1][2]=='10/25/2026'


def test_bulk_failure_and_out_of_window_response_are_not_success(monkeypatch):
    monkeypatch.setattr(G.time,'sleep',lambda *a:None)
    response=bulk_frame()
    monkeypatch.setattr(G.playergamelogs,'PlayerGameLogs',lambda **kw:type('Endpoint',(),
                        {'get_data_frames':lambda self:[response]})())
    with pytest.raises(RuntimeError):G.fetch_bulk_game_logs('2026-27','Regular Season','10/01/2026','10/31/2026')


def test_team_display_uses_home_handicap_not_favorite_name(capsys):
    from types import SimpleNamespace
    games=pd.DataFrame([(t,i) for t in ['CHA','DET'] for i in range(12)],columns=['team','game_id'])
    engine=SimpleNamespace(_games=games,predict_spread=lambda *a,**kw:-5.47,
                           win_probability=lambda *a,**kw:.37,get_rating=lambda t:0)
    game=dict(home='CHA',away='DET',game_id='0022600001',status='scheduled',
              home_full='Charlotte',away_full='Detroit',spread=5.5,spread_team='DET',
              home_ml=182,away_ml=-219,home_implied=.35,away_implied=.69)
    row=T.analyze_game(game,engine,False,'2026-11-02')
    T.print_game_block(row)
    output=capsys.readouterr().out
    assert 'Market handicaps: CHA +5.5 / DET -5.5' in output
    assert 'CHA fair handicap +5.5 vs market +5.5' in output


def test_latest_live_view_preserves_original(tmp_path):
    first=tmp_path/'day.json';first.write_text('original')
    revision=tmp_path/'day.revision-1.json';revision.write_text('latest')
    assert latest_revision(first)==revision and first.read_text()=='original'


def test_calibration_report_separates_stale_quotes_and_does_not_count_revisions(tmp_path):
    conn=sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE game_logs (player_id,game_date,pts,min,team_abbr,opp_abbr)')
    conn.execute("INSERT INTO game_logs VALUES (1,'2026-11-02',12,30,'AAA','BBB')")
    pick=dict(player_id=1,stat_key='pts',team='AAA',opp='BBB',line=20.5,prob=.7,
              decimal_odds=1.9,direction='UNDER',current_season_games=5,quote=quote())
    payload=dict(date='2026-11-02',generated_at=NOW,picks=[pick])
    (tmp_path/'2026-11-02.json').write_text(json.dumps(payload))
    (tmp_path/'2026-11-02.revision-1.json').write_text(json.dumps(payload))
    report=R.summarize(tmp_path,conn,'2026-11-03')
    assert report['groups'][0]['n']==1
    assert report['groups'][0]['brier']==pytest.approx(.09)
    assert report['groups'][0]['quote_quality']=='fresh_at_forecast'
    payload['picks'][0]['quote']['paired_quotes'][0]['updated_at']='2026-11-02T23:00Z'
    (tmp_path/'2026-11-02.json').write_text(json.dumps(payload))
    report=R.summarize(tmp_path,conn,'2026-11-03')
    assert report['groups'][0]['hypothetical_unit_roi'] is None


def test_current_quotes_need_a_provider_time_and_independent_books():
    assert not no_vig_consensus(paired(None),True,captured_at=NOW)
    rows=paired()+paired('2026-11-02T20:06Z')
    assert not no_vig_consensus(rows,True,captured_at=NOW)


def test_prop_transport_failure_is_not_an_empty_market(monkeypatch):
    from src.data import espn_props as EP
    monkeypatch.setattr(EP,'_get',lambda *a:dict(events=[dict(id='401',date='2099-11-03T00:30Z',
                        status=dict(type=dict(state='pre')))]))
    monkeypatch.setattr(EP,'fetch_event',lambda *a:None)
    with pytest.raises(RuntimeError,match='transport failed'):
        EP.fetch_book_props('2099-11-02')


def test_research_bundle_cannot_leak_calibration_season_into_historical_forecasts():
    with pytest.raises(ValueError,match='calibration must precede'):
        A.predict(pd.DataFrame(),[],'2025-01-01','2024-25',{'calibration_season':'2024-25'})
