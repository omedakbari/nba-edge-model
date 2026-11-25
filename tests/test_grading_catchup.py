"""Catch-up grading (DECISIONS.md D17): every ungraded date since the last success, idempotent, lag WARN."""
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]

from src.utils import grading as G  # noqa: E402
from src.utils import step_status as S  # noqa: E402


def _files(folder: Path, dates, picks=None, revision=None):
    folder.mkdir(parents=True, exist_ok=True)
    for d in dates:
        (folder / f'{d}.json').write_text(json.dumps({'picks': picks if picks is not None else [{'x': 1}]}))
    if revision:
        d, body = revision
        (folder / f'{d}.revision-20261120T222248298397Z.json').write_text(json.dumps(body))


def _db(path: Path, game_days, graded=()):
    with sqlite3.connect(path) as c:
        c.execute('create table game_logs (game_date text)')
        c.executemany('insert into game_logs values (?)', [(d,) for d in game_days])
        if graded:                       # only for the read-side test; graders create the real table
            c.execute('create table pick_results (pick_date text)')
            c.executemany('insert into pick_results values (?)', [(d,) for d in graded])


def test_today_hole_20_then_24_is_what_gets_graded():
    files = ['2026-11-18', '2026-11-19', '2026-11-20', '2026-11-24']
    graded = {'2026-11-18', '2026-11-19'}
    assert G.to_grade(files, graded, '2026-11-25') == ['2026-11-20', '2026-11-24']
    assert G.to_grade(files, graded | {'2026-11-20', '2026-11-24'}, '2026-11-25') == []      # idempotent
    assert G.to_grade(files, graded, '2026-11-24') == ['2026-11-20']                          # today excluded


def test_older_holes_before_last_success_need_since():
    files = ['2026-11-15', '2026-11-16', '2026-11-17']
    graded = {'2026-11-15', '2026-11-17'}
    assert G.to_grade(files, graded, '2026-11-25') == []
    assert G.to_grade(files, graded, '2026-11-25', since='2026-11-15') == ['2026-11-16']


def test_pick_file_dates_ignore_revisions_and_logs(tmp_path):
    _files(tmp_path, ['2026-11-20'], revision=('2026-11-20', {'picks': []}))
    (tmp_path / 'results_log.csv').write_text('x')
    assert G.pick_file_dates(tmp_path) == ['2026-11-20']


def test_market_zero_pick_newest_revision_counts_as_graded(tmp_path):
    folder = tmp_path / 'market_picks'
    _files(folder, ['2026-11-20'], picks=[{'x': 1}], revision=('2026-11-20', {'picks': []}))
    _files(folder, ['2026-11-24'], picks=[{'x': 1}])
    db = tmp_path / 'db.sqlite'
    _db(db, [], graded=['2026-11-10'])
    assert G.market_graded_dates(db, folder) == {'2026-11-10', '2026-11-20'}


def test_lag_warning_names_days_and_reason(tmp_path):
    db = tmp_path / 'db.sqlite'
    _db(db, ['2026-11-19', '2026-11-20', '2026-11-21', '2026-11-22', '2026-11-23', '2026-11-24'])
    files = ['2026-11-19', '2026-11-20', '2026-11-24']
    w = G.lag_warning('team', files, {'2026-11-19'}, '2026-11-25', db)
    assert w.startswith('team grading lags 5 game day(s) since 2026-11-19: ')
    assert '2026-11-20 (not graded yet)' in w and '2026-11-21 (no picks file: daily not run)' in w
    # once 11-24 is graded, the unfillable 11-21..23 holes no longer count
    assert G.lag_warning('team', files, {'2026-11-19', '2026-11-20', '2026-11-24'}, '2026-11-25', db) is None
    # one game day behind is normal (yesterday's games)
    assert G.lag_warning('team', files, {'2026-11-23'}, '2026-11-25', db) is None


def test_market_catch_up_grades_each_date_once(tmp_path, monkeypatch):
    import log_pick_results as L
    folder = tmp_path / 'market_picks'
    pick = dict(player_id=1, player_name='A', team='X', opp='Y', stat='PTS', stat_key='pts', direction='OVER',
                line=10.5, model_pred=12, prob=0.6, card='', n_legs=1, line_source='', injury_flag=0)
    for d in ('2026-11-20', '2026-11-24'):
        _files(folder, [d], picks=[pick])
    db = tmp_path / 'db.sqlite'
    _db(db, ['2026-11-20', '2026-11-24'])
    monkeypatch.setattr(L, 'DB_PATH', db)
    monkeypatch.setattr(L, 'PICKS_DIR', tmp_path / 'picks')
    monkeypatch.setattr(L, 'fetch_actuals_from_db', lambda *a, **k: {1: {'played': True, 'pts': 15}})
    monkeypatch.setattr('src.utils.slate.slate_today', lambda: __import__('datetime').date(2026, 11, 25))
    warn_file = tmp_path / 'w.jsonl'
    monkeypatch.setenv(S.WARN_ENV, str(warn_file))
    assert L.catch_up(market=True) == ['2026-11-20', '2026-11-24']
    assert L.catch_up(market=True) == []                                  # second run: nothing left
    rows = sqlite3.connect(db).execute('select pick_date from pick_results order by 1').fetchall()
    assert rows == [('2026-11-20',), ('2026-11-24',)]
    assert S.read_warnings(warn_file) == []                               # caught up: no lag


def test_market_catch_up_dry_run_writes_nothing_and_does_not_warn(tmp_path, monkeypatch):
    import log_pick_results as L
    folder = tmp_path / 'market_picks'
    pick = dict(player_id=1, player_name='A', team='X', opp='Y', stat='PTS', stat_key='pts', direction='OVER',
                line=10.5, model_pred=12, prob=0.6, card='', n_legs=1, line_source='', injury_flag=0)
    _files(folder, ['2026-11-20'], picks=[])                             # graded: nothing to grade
    _files(folder, ['2026-11-24'], picks=[pick])
    db = tmp_path / 'db.sqlite'
    _db(db, ['2026-11-24'])
    monkeypatch.setattr(L, 'DB_PATH', db)
    monkeypatch.setattr(L, 'PICKS_DIR', tmp_path / 'picks')
    monkeypatch.setattr(L, 'fetch_actuals_from_db', lambda *a, **k: {1: {'played': True, 'pts': 15}})
    monkeypatch.setattr('src.utils.slate.slate_today', lambda: __import__('datetime').date(2026, 11, 25))
    assert L.catch_up(market=True, save=False) == ['2026-11-24']
    n = sqlite3.connect(db).execute("select count(*) from pick_results where pick_date = '2026-11-24'").fetchone()[0]
    assert n == 0


def test_team_graded_dates_reads_results_log(tmp_path):
    p = tmp_path / 'results_log.csv'
    pd.DataFrame({'game_date': ['2026-11-18', '2026-11-19'], 'game_id': ['1', '2']}).to_csv(p, index=False)
    assert G.team_graded_dates(p) == {'2026-11-18', '2026-11-19'}
    assert G.team_graded_dates(tmp_path / 'none.csv') == set()
