"""Boundary, time-instant and as-of corrections without changed model formulas."""
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import pandas as pd
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'scripts')]
from src.data.espn_props import no_vig_consensus
import find_bets as F
import generate_todays_team_picks as T
import backtest_team_model as B


@pytest.mark.parametrize('edge,side',[(3.5,'home'),(-3.5,'away')])
def test_exact_spread_boundary_has_consistent_selection(edge,side):
    engine=SimpleNamespace(_games=pd.DataFrame([(t,i) for t in ['CHA','DET'] for i in range(12)],columns=['team','game_id']),
        predict_spread=lambda *a,**kw:edge,win_probability=lambda *a,**kw:.5,get_rating=lambda *a:0)
    g=dict(home='CHA',away='DET',game_id='401',status='scheduled',home_full='CHA',away_full='DET',spread=0,home_implied=.52,away_implied=.52)
    r=T.analyze_game(g,engine,False,'2026-11-06')
    assert r['ats_strength']=='LEAN' and r['spread_pick_side']==side
    assert not r['approved'] and r['stake_pct']==0


def test_pairing_uses_instant_not_timestamp_spelling():
    rows=[('401','2026-11-06','Book','9','pts',side,20.5,1.9,20.5,1.9,stamp) for side,stamp in [
        ('over','2026-11-06T18:24Z'),('under','2026-11-06T10:24:00-08:00')]]
    result=no_vig_consensus(rows,True,captured_at='2026-11-06T18:32Z')
    assert len(result)==1 and result[('9','pts')]['paired_quotes'][0]['updated_at']=='2026-11-06T18:24:00+00:00'


def test_approval_oldest_quote_uses_aware_minimum(monkeypatch):
    observed=[]
    monkeypatch.setattr(F,'approval',lambda rec,*a,**kw:observed.append(rec) or {'approved':False,'stake_pct':0,'reasons':[]})
    monkeypatch.setattr(F,'anchor_issues',lambda *a:[])
    rec={'quote':{'paired_quotes':[{'updated_at':'2026-11-06T10:25:00-08:00'},{'updated_at':'2026-11-06T18:20Z'}]}}
    F.bet_approval(rec,{'p':.6,'dec':1.9,'venue':'Book'})
    assert observed[0]['quote_updated_at']=='2026-11-06T18:20:00+00:00'


def test_totals_loader_cannot_read_future_snapshot():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE team_stats (team_abbr,off_rtg,def_rtg,pace,season,updated_at)')
    c.executemany('INSERT INTO team_stats VALUES (?,?,?,?,?,?)',[
        ('CHA',110,110,100,'2026-27','2026-11-06T18:00Z'),('DET',120,120,100,'2026-27','2026-11-07T18:00Z')])
    assert set(T.load_team_scoring_stats(c,'2026-27',as_of='2026-11-06T19:00Z'))=={'CHA'}
    assert T.load_team_scoring_stats(c,'2026-27',as_of='2026-11-06T17:00Z')=={}
    with pytest.raises(ValueError):T.load_team_scoring_stats(c,'2026-27',as_of='2026-11-06T19:00')


def test_legacy_backtest_warns_on_execution(monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['legacy','--help'])
    with pytest.raises(SystemExit):B.main()
    assert 'LEGACY / LEAKY' in capsys.readouterr().out
