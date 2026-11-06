"""Regression coverage for daily CLI and pregame market boundaries."""
import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import nba
import find_bets
import generate_todays_picks as picks
import generate_todays_team_picks as teams
import log_pick_results as grader
from src.data import espn_props
from src.ingestion import game_logs


@pytest.mark.parametrize('phase', ['regular', 'playoffs', 'play-in', 'offseason'])
def test_daily_all_phases(monkeypatch, phase):
    calls = []
    monkeypatch.setattr(nba, 'detect_phase', lambda season: phase)
    monkeypatch.setattr(nba, 'refreshed_today', lambda season: True)
    monkeypatch.setattr(nba, 'run', lambda cmd, *a: calls.append(cmd) or 0)
    nba.cmd_daily(None)
    assert len(calls) == 5
    assert calls[-1][0] == 'scripts/build_prop_stacks.py'
    assert calls[2][0] == 'scripts/find_bets.py'
    assert '--save' in calls[2]
    assert '--market' in calls[0]
    assert ('--playoffs' in calls[2]) == (phase == 'playoffs')
    assert ('--play-in' in calls[2]) == (phase == 'play-in')


@pytest.mark.parametrize('pickem,script', [(False, 'scripts/find_bets.py'),
                                        (True, 'scripts/generate_todays_picks.py')])
def test_picks_defaults_to_market_prices(monkeypatch, pickem, script):
    calls = []
    monkeypatch.setattr(nba, 'detect_phase', lambda season: 'regular')
    monkeypatch.setattr(nba, 'run', lambda cmd, *a: calls.append(cmd) or 0)
    nba.cmd_picks(SimpleNamespace(quick=True, team_only=False, props_only=True, pickem=pickem))
    assert len(calls) == (1 if pickem else 2)
    assert calls[0][0] == script
    assert '--save' in calls[0]


def test_default_schedule_never_calls_sleeper(monkeypatch):
    from nba_api.stats.endpoints import scoreboardv3
    monkeypatch.setattr(picks.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(scoreboardv3, 'ScoreboardV3', lambda **kw:
                        SimpleNamespace(get_data_frames=lambda: [None, pd.DataFrame(), pd.DataFrame()]))
    monkeypatch.setattr(picks, '_games_from_espn', lambda d: [])
    monkeypatch.setattr(picks, '_games_from_sleeper', lambda *a: pytest.fail('Sleeper dependency'))
    assert picks.get_todays_games(date.today().isoformat()) == []


def test_market_candidates_save_and_grade_combos(monkeypatch, tmp_path):
    market_dir = tmp_path / 'market_picks'
    monkeypatch.setattr(find_bets, 'MARKET_PICKS_DIR', market_dir)
    monkeypatch.setattr(grader, 'PICKS_DIR', tmp_path / 'picks')
    db = tmp_path / 'test.db'
    monkeypatch.setattr(grader, 'DB_PATH', db)
    d = '2026-10-21'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE game_logs (player_id, game_date, season_type, min, pts, reb, ast, fg3m, pra, reb_ast)')
        conn.execute('INSERT INTO game_logs VALUES (1, ?, "Regular Season", 30, 20, 10, 6, 2, 36, 16)', (d,))
    board = [dict(player='Test Player', player_id=1, key='test player', team='CLE', opp='PHI',
                  stat=s, pred=30, injury='') for s in ['pts_reb', 'pts_ast']]
    bets = [dict(player='Test Player', stat=s, side='over', line=27.5, p=.6,
                 ev=.2, dec=2, price='+100', venue='Book') for s in ['pts_reb', 'pts_ast']]
    path = find_bets.save_market_picks(bets + [dict(bets[0], ev=.1)], board, d, '2026-27', 'regular')
    assert len(json.loads(path.read_text())['picks']) == 2
    grader.grade_date(d, market=True)
    with sqlite3.connect(db) as conn:
        results = dict(conn.execute('SELECT stat_key, result FROM pick_results'))
    assert results == {'pts_reb': 'hit', 'pts_ast': 'miss'}


def test_ingestion_failure_is_not_an_empty_success(monkeypatch):
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise ValueError('invalid NBA response')
    monkeypatch.setattr(game_logs.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(game_logs.playergamelog, 'PlayerGameLog', fail)
    with pytest.raises(RuntimeError, match='Error fetching player'):
        game_logs.fetch_player_game_log(1, '2026-27')
    assert len(calls) == 3
    assert calls[0]['date_from_nullable'] == '07/01/2026'
    assert calls[0]['date_to_nullable']


def test_daily_ingestion_errors_fail_the_command(monkeypatch):
    monkeypatch.setattr(game_logs, 'init_db', lambda: None)
    monkeypatch.setattr(game_logs, 'ingest_season', lambda *a, **kw: {'inserted': 0, 'errors': 1})
    with pytest.raises(SystemExit) as exc:
        game_logs.update_current_season()
    assert exc.value.code == 1


def test_cli_subprocess_failure_is_nonzero(monkeypatch):
    monkeypatch.setattr(nba.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=7))
    with pytest.raises(SystemExit) as exc:
        nba.run(['missing_script.py'])
    assert exc.value.code == 7


def test_failed_refresh_stops_daily_generation(monkeypatch):
    calls = []
    def subprocess_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=1 if 'scripts/update_data.py' in cmd else 0)
    monkeypatch.setattr(nba.subprocess, 'run', subprocess_run)
    monkeypatch.setattr(nba, 'detect_phase', lambda season: 'regular')
    monkeypatch.setattr(nba, 'refreshed_today', lambda season: False)
    with pytest.raises(SystemExit) as exc:
        nba.cmd_daily(None)
    assert exc.value.code == 1
    assert len(calls) == 3
    assert 'scripts/update_data.py' in calls[-1]


@pytest.mark.parametrize('offset,expected', [(None, False), (-60, False), (60, True)])
def test_refresh_requires_features_newer_than_team_stats(monkeypatch, tmp_path, offset, expected):
    db = tmp_path / 'nba.db'
    updated = datetime.now().replace(microsecond=0)
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE team_stats (season TEXT, updated_at TEXT)')
        conn.execute('INSERT INTO team_stats VALUES (?, ?)', ('2026-27', updated.isoformat(' ')))
    monkeypatch.setattr(nba, 'DB_PATH', db)
    monkeypatch.setattr(nba, 'PROCESSED_DIR', tmp_path)
    if offset is not None:
        features = tmp_path / 'features_appearance_v2.parquet'
        features.touch()
        os.utime(features, (updated.timestamp() + offset, updated.timestamp() + offset))
    assert nba.refreshed_today('2026-27') == expected


@pytest.mark.parametrize('historical,expected', [(False, ['pre']), (True, ['pre', 'in', 'post'])])
def test_book_props_excludes_live_events(monkeypatch, historical, expected):
    events = [{'id': s, 'date': '2099-11-03T00:30:00Z', 'status': {'type': {'state': s}}} for s in ('pre', 'in', 'post')]
    monkeypatch.setattr(espn_props, '_get', lambda *a: {'events': events})
    fetched = []
    monkeypatch.setattr(espn_props, 'fetch_event', lambda session, eid, d: fetched.append(eid) or [])
    espn_props.fetch_book_props('2020-01-01' if historical else date.today().isoformat())
    assert sorted(fetched) == sorted(expected)


def test_nba_schedule_does_not_revive_started_games(monkeypatch):
    from nba_api.stats.endpoints import scoreboardv3
    games = pd.DataFrame([{'gameStatus': 2}, {'gameStatus': 3}])
    monkeypatch.setattr(picks.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(scoreboardv3, 'ScoreboardV3', lambda **kw:
                        SimpleNamespace(get_data_frames=lambda: [None, games, pd.DataFrame()]))
    monkeypatch.setattr(picks, '_games_from_espn', lambda d: pytest.fail('must not revive live games'))
    assert picks.get_todays_games('2026-10-21') == []


def test_espn_schedule_excludes_in_progress(monkeypatch):
    from src.data import espn_odds
    monkeypatch.setattr(espn_odds, 'fetch_odds', lambda d: [
        {'status': s, 'home': 'LAL', 'away': 'GSW', 'game_id': s}
        for s in ('scheduled', 'in_progress', 'final')])
    assert [g['game_id'] for g in picks._games_from_espn('2026-10-21')] == ['scheduled']


def test_scanner_empty_schedule_never_loads_board(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['find_bets'])
    monkeypatch.setattr(find_bets, 'get_todays_games', lambda d: [])
    monkeypatch.setattr(find_bets, 'build_fair_board', lambda *a: pytest.fail('no schedule'))
    find_bets.main()


def test_scanner_filters_cached_board_and_accepts_espn_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'argv', ['find_bets', '--reuse-board'])
    monkeypatch.setattr(find_bets, 'FAIR_DIR', tmp_path)
    monkeypatch.setattr(find_bets, 'eligible_board', lambda board, *a: board)
    monkeypatch.setattr(find_bets, 'get_todays_games', lambda d: [
        {'game_id': '401909092', 'home_abbr': 'LAL', 'away_abbr': 'GSW'}])
    board = [{'team': 'LAL', 'opp': 'GSW'}, {'team': 'ATL', 'opp': 'ORL'}]
    (tmp_path / f'{date.today().isoformat()}.json').write_text(json.dumps(board))
    seen = []
    monkeypatch.setattr(find_bets, 'kalshi_bets', lambda b, d: seen.extend(b) or [])
    monkeypatch.setattr(find_bets, 'book_bets', lambda b: [])
    monkeypatch.setattr(find_bets, 'quality_board', lambda b: (b, []))
    monkeypatch.setattr(find_bets, 'PROJECT_ROOT', tmp_path)
    find_bets.main()
    assert seen == board[:1]


@pytest.mark.parametrize('n_home,n_away,blocked', [(0, 0, True), (10, 9, True), (10, 10, False)])
def test_team_picks_require_history(n_home, n_away, blocked):
    games = pd.DataFrame([(team, i) for team, n in [('LAL', n_home), ('GSW', n_away)]
                          for i in range(n)], columns=['team', 'game_id'])
    engine = SimpleNamespace(_games=games, predict_spread=lambda *a, **kw: 20,
                             win_probability=lambda *a, **kw: .9, get_rating=lambda t: 0)
    game = dict(home='LAL', away='GSW', game_id='0022600001', status='scheduled',
                home_full='Lakers', away_full='Warriors', spread=-2.5,
                home_ml=-110, away_ml=100, home_implied=.52, away_implied=.5)
    result = teams.analyze_game(game, engine, False, '2026-10-21')
    assert result['insufficient_history'] == blocked
    if blocked:
        assert result['tier'] == result['ou_tier'] == 'NONE'
        assert result['spread_pick_team'] is result['ml_pick_team'] is None
        assert result['sp_edge'] is result['home_ml_edge'] is result['away_ml_edge'] is None
        assert result['model_spread'] is result['model_total'] is None
    else:
        assert result['tier'] == 'STRONG'


@pytest.mark.parametrize('status', ['in_progress', 'final'])
def test_team_workflow_does_not_reopen_started_slate(monkeypatch, status):
    conn = SimpleNamespace(close=lambda: None)
    engine = SimpleNamespace(all_ratings=lambda: pd.DataFrame({'games': [20]}),
                             _games=pd.DataFrame())
    monkeypatch.setattr(sys, 'argv', ['team_picks'])
    monkeypatch.setattr(teams.sqlite3, 'connect', lambda *a: conn)
    monkeypatch.setattr(teams, 'TeamRatingEngine', lambda *a, **kw: engine)
    monkeypatch.setattr(teams, 'fetch_odds', lambda d: [{'status': status}])
    monkeypatch.setattr(teams, 'persist_odds_snapshot', lambda *a, **kw: 0)
    monkeypatch.setattr(teams, 'analyze_game', lambda *a, **kw: pytest.fail('already started'))
    assert teams.main() == []


def test_unknown_results_remain_pending_and_can_be_retried(monkeypatch, tmp_path, capsys):
    db = tmp_path / 'test.db'
    monkeypatch.setattr(grader, 'DB_PATH', db)
    monkeypatch.setattr(grader, 'PICKS_DIR', tmp_path / 'picks')
    monkeypatch.setattr(grader, 'fetch_actuals_from_db', lambda *a, **kw: {})
    actuals = {1: {'played': True, 'pts': 20}}
    monkeypatch.setattr(grader, 'fetch_actuals_from_api', lambda *a, **kw: actuals.copy())
    directory = tmp_path / 'market_picks'
    monkeypatch.setattr(find_bets, 'MARKET_PICKS_DIR', directory)
    board = [dict(player=f'Player {i}', player_id=i, key=f'player {i}', team='LAL',
                  opp='GSW', stat='pts', pred=20, injury='') for i in (1, 2)]
    bets = [dict(player=f'Player {i}', stat='pts', side='over', line=15.5, p=.6,
                 ev=.2, dec=2, price='+100', venue='Book') for i in (1, 2)]
    find_bets.save_market_picks(bets, board, '2026-10-22', '2026-27', 'regular')
    grader.grade_date('2026-10-22', market=True)
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT player_id,result FROM pick_results').fetchall() == [(1, 'hit')]
    assert 'Pending: 1' in capsys.readouterr().out
    actuals[2] = {'played': False}
    grader.grade_date('2026-10-22', market=True)
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT player_id,result FROM pick_results ORDER BY player_id').fetchall() == [(1, 'hit'), (2, 'void')]


def test_history_gate_excludes_old_history_and_future_but_keeps_short_appearances(monkeypatch, tmp_path):
    db = tmp_path / 'test.db'
    monkeypatch.setattr(find_bets, 'DB_PATH', db)
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE game_logs (player_id,game_id,game_date,season,min,season_type)')
        rows = [(1, str(i), f'2026-10-{i+1:02d}', '2026-27', 30, 'Regular Season') for i in range(10)]
        rows += [(2, str(i), '2025-10-01', '2025-26', 30, 'Regular Season') for i in range(20)]
        rows += [(2, str(i), '2026-10-23', '2026-27', 30, 'Regular Season') for i in range(20)]
        rows += [(3, str(i), '2026-10-01', '2026-27', 5, 'Regular Season') for i in range(20)]
        c.executemany('INSERT INTO game_logs VALUES (?,?,?,?,?,?)', rows)
    result = find_bets.eligible_board([{'player_id': i} for i in (1,2,3)], '2026-10-23', '2026-27')
    assert [r['player_id'] for r in result] == [1, 3]
    assert result[0]['signal_weight'] == 1.0


def test_prediction_rolling_window_includes_last_completed_game(monkeypatch, tmp_path):
    db = tmp_path / 'test.db'
    monkeypatch.setattr(picks, 'DB_PATH', db)
    stats = ['pts','reb','ast','stl','blk','fg3m','pra','pts_reb','reb_ast','pts_ast',
             'min','fga','fg3a','fta','tov','pf']
    rows = pd.DataFrame({'player_id': [1]*12,
                         'game_date': pd.date_range('2026-10-01', periods=12).astype(str),
                         **{s: [float(i) for i in range(1,13)] for s in stats}})
    rows['min'] = 30
    with sqlite3.connect(db) as c:
        rows.to_sql('game_logs', c, index=False)
    result = picks.current_player_features([1], '2026-10-12')[1]
    # Game on the prediction date (12) must not enter; last completed game (11) must.
    assert result['pts_L10'] == pytest.approx(6.5)
    assert result['pts_L5'] == pytest.approx(9)
    assert result['pts_std_L10'] == pytest.approx(pd.Series(range(2,12)).std())


def test_scanner_saves_empty_picks_when_all_history_is_insufficient(monkeypatch, tmp_path):
    db = tmp_path / 'test.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE game_logs (player_id,game_id,game_date,season,min,season_type)')
    monkeypatch.setattr(find_bets, 'DB_PATH', db)
    monkeypatch.setattr(find_bets, 'FAIR_DIR', tmp_path)
    monkeypatch.setattr(find_bets, 'MARKET_PICKS_DIR', tmp_path / 'market_picks')
    monkeypatch.setattr(sys, 'argv', ['find_bets', '--reuse-board', '--save'])
    monkeypatch.setattr(find_bets, 'get_todays_games', lambda d: [
        {'game_id': '401909092', 'home_abbr': 'LAL', 'away_abbr': 'GSW'}])
    board = [{'player_id': 1, 'team': 'LAL', 'opp': 'GSW'}]
    (tmp_path / f'{date.today().isoformat()}.json').write_text(json.dumps(board))
    monkeypatch.setattr(find_bets, 'kalshi_bets', lambda *a: pytest.fail('ineligible props must not be priced'))
    monkeypatch.setattr(find_bets, 'book_bets', lambda *a: pytest.fail('ineligible props must not be priced'))
    find_bets.main()
    result = json.loads((tmp_path / 'market_picks' / f'{date.today().isoformat()}.json').read_text())
    assert result['picks'] == []


@pytest.mark.parametrize('requested', ['2026-10-23', None])
def test_evening_odds_use_nba_slate_date_not_utc(monkeypatch, requested):
    from src.data import espn_odds
    event = {'id': '1', 'date': '2026-10-24T02:00:00Z',
             'competitions': [{'competitors': [
                 {'homeAway': 'home', 'team': {'abbreviation': 'LAL'}},
                 {'homeAway': 'away', 'team': {'abbreviation': 'LAC'}}]}]}
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'events': [event]})
    monkeypatch.setattr(espn_odds.requests, 'get', lambda *a, **kw: response)
    assert espn_odds.fetch_odds(requested)[0]['game_date'] == '2026-10-23'


def test_opening_day_props_use_prior_history_and_reduce_signal(monkeypatch, tmp_path):
    db = tmp_path / 'test.db'
    monkeypatch.setattr(find_bets, 'DB_PATH', db)
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE game_logs (player_id,game_id,game_date,season,min,season_type)')
        rows = [(1,str(i),'2026-04-01','2025-26',30,'Regular Season') for i in range(10)]
        rows += [(1,'current','2026-10-21','2026-27',30,'Regular Season')]
        c.executemany('INSERT INTO game_logs VALUES (?,?,?,?,?,?)',rows)
    rec = dict(player_id=1, stat='pts', pred=30, std=5, book_line=20.5, book_p_over=.5)
    opening = find_bets.eligible_board([rec], '2026-10-20', '2026-27')[0]
    assert opening['current_season_games']==0 and opening['signal_weight']==.5
    day_two = find_bets.eligible_board([rec], '2026-10-23', '2026-27')[0]
    assert day_two['signal_weight']==pytest.approx(.55)
    full = find_bets.fair_p(rec,20.5,'over')
    shrunk = find_bets.fair_p(opening,20.5,'over')
    assert shrunk == pytest.approx(.5+.5*(full-.5))
    assert find_bets.fair_p(opening,20.5,'under') + shrunk == pytest.approx(1)
    # Applying eligibility twice must not compound the shrinkage.
    again = find_bets.eligible_board([day_two], '2026-10-23', '2026-27')[0]
    assert find_bets.fair_p(again,20.5,'over') == find_bets.fair_p(day_two,20.5,'over')


def test_opening_team_rating_shrinks_one_game_outlier_toward_prior():
    from src.models.team_ratings import TeamRatingEngine
    engine = TeamRatingEngine.__new__(TeamRatingEngine)
    engine._games = pd.DataFrame([dict(team='LAL',game_id='1')])
    engine._srs = {};engine._season_ratings = {'LAL':50};engine._form={'LAL':None}
    engine._prior_ratings={'LAL':5};engine._prior_counts={'LAL':82}
    rating=engine._build_combined_ratings()['LAL']
    assert rating==pytest.approx(50/21+20/21*.8*5)
    assert engine.has_rating_history('LAL')
    assert not engine.has_rating_history('UNKNOWN')


def test_team_priors_enable_opening_week_spreads_but_not_totals():
    engine = SimpleNamespace(_games=pd.DataFrame([('LAL','1'),('GSW','1')],columns=['team','game_id']),
                             predict_spread=lambda *a, **kw: 8,
                             win_probability=lambda *a, **kw: .7,get_rating=lambda t:0,
                             has_rating_history=lambda t: True)
    game=dict(home='LAL',away='GSW',game_id='1',status='scheduled',home_full='Lakers',
              away_full='Warriors',spread=-2.5,home_implied=.52,away_implied=.5,over_under=230)
    a=teams.analyze_game(game,engine,False,'2026-10-23',team_scoring_stats={
        t:dict(off_rtg=120,def_rtg=120,pace=110) for t in ('LAL','GSW')})
    assert not a['insufficient_history'] and a['tier']=='STRONG'
    assert a['model_total'] is None and a['ou_tier']=='NONE'
