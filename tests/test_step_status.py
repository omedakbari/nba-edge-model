"""Fail loud (AUDIT_2026-11-24.md items 1, 6, 10): a step with input > 0 and output 0 is WARN, never OK."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]

import nba  # noqa: E402
from src.utils import step_status as S  # noqa: E402


def test_warn_appends_to_file_and_prints(tmp_path, monkeypatch, capsys):
    f = tmp_path / 'w.jsonl'
    monkeypatch.setenv(S.WARN_ENV, str(f))
    S.warn('quotes offered, 0 lines', step='x')
    assert 'WARN: quotes offered, 0 lines' in capsys.readouterr().out
    assert S.read_warnings(f) == [{'step': 'x', 'reason': 'quotes offered, 0 lines'}]


def test_warn_without_env_only_prints(monkeypatch, capsys):
    monkeypatch.delenv(S.WARN_ENV, raising=False)
    S.warn('r')
    assert 'WARN: r' in capsys.readouterr().out


def test_warn_if_empty_only_on_input_without_output(tmp_path, monkeypatch):
    f = tmp_path / 'w.jsonl'
    monkeypatch.setenv(S.WARN_ENV, str(f))
    assert S.warn_if_empty(506, 0, 'DK lines') is True
    assert S.warn_if_empty(0, 0, 'nothing offered') is False         # no input: not a silent failure
    assert S.warn_if_empty(5, 2, 'some out') is False
    assert [w['reason'] for w in S.read_warnings(f)] == ['DK lines: 506 in, 0 out']


def test_status_line_format_matches_dashboard_parser():
    sys.path.insert(0, str(ROOT / 'dashboard'))
    import today as T
    lines = [S.status_line('Market props — yesterday', 0),
             S.status_line('Player props — market prices and EV', 0, [{'reason': 'a'}, {'reason': 'b'}]),
             S.status_line('Team spread / ML picks', 9)]
    assert lines[1] == '  WARN (exit 0) — Player props — market prices and EV :: a; b'
    assert T.step_results('\n'.join(lines)) == [
        ('OK', 0, 'Market props — yesterday', ''),
        ('WARN', 0, 'Player props — market prices and EV', 'a; b'),
        ('FAILED', 9, 'Team spread / ML picks', '')]


def test_read_warnings_ignores_garbage(tmp_path):
    f = tmp_path / 'w.jsonl'
    f.write_text('not json\n{"reason": "ok"}\n{"x": 1}\n')
    assert S.read_warnings(f) == [{'reason': 'ok'}]
    assert S.read_warnings(tmp_path / 'missing') == [] and S.read_warnings(None) == []


def test_nba_run_records_warn_and_forwards_to_parent(tmp_path, monkeypatch, capsys):
    parent = tmp_path / 'parent.jsonl'
    monkeypatch.setenv(S.WARN_ENV, str(parent))
    nba.STEP_RESULTS.clear()
    code = ("import json, os; open(os.environ['NBA_WARN_FILE'], 'a')"
            ".write(json.dumps({'reason': '506 offered, 0 lines'}) + chr(10))")
    assert nba.run(['-c', code], 'Player props — market prices and EV') == 0
    assert nba.STEP_RESULTS == [('Player props — market prices and EV', 0, [{'reason': '506 offered, 0 lines'}])]
    assert S.read_warnings(parent) == [{'step': 'Player props — market prices and EV',
                                        'reason': '506 offered, 0 lines'}]
    nba.STEP_RESULTS.clear()


def test_nba_run_clean_step_is_ok(monkeypatch):
    monkeypatch.delenv(S.WARN_ENV, raising=False)
    nba.STEP_RESULTS.clear()
    assert nba.run(['-c', 'pass'], 'Refreshing data') == 0
    assert nba.STEP_RESULTS == [('Refreshing data', 0, [])]
    nba.STEP_RESULTS.clear()


def test_run_daily_finish_exit_codes(capsys):
    import run_daily
    assert run_daily.finish(0, []) == 0
    assert run_daily.finish(0, [{'step': 'Prop card', 'reason': 'x'}]) == S.WARN_EXIT == 3
    assert run_daily.finish(2, [{'reason': 'x'}]) == 2
    out = capsys.readouterr().out
    assert 'RUN STATUS: OK' in out and 'RUN STATUS: WARN — 1 warning(s)' in out and '- Prop card: x' in out


def test_graders_warn_on_missing_picks_file(tmp_path, monkeypatch):
    import log_pick_results
    import log_team_results
    f = tmp_path / 'w.jsonl'
    monkeypatch.setenv(S.WARN_ENV, str(f))
    monkeypatch.setattr(log_pick_results, 'PICKS_DIR', tmp_path / 'picks')
    monkeypatch.setattr(log_team_results, 'PICKS_DIR', tmp_path / 'team_picks')
    log_pick_results.grade_date('2026-11-23', market=True)
    assert log_team_results.grade_date('2026-11-23') is None
    reasons = [w['reason'] for w in S.read_warnings(f)]
    assert reasons == ['no market picks file for 2026-11-23 (daily not run that day, or no games)',
                       'no team picks file for 2026-11-23 (daily not run that day, or no games)']


def test_worker_health_names_the_validate_player_and_flags_stale_jobs():
    from src.data.worker_health import status_lines, summarize
    now = datetime(2026, 11, 24, 23, 40, tzinfo=timezone.utc)
    rows = [
        ('validate', 'red', now - timedelta(hours=3), 10, 9,
         ['1 mismatch(es) vs PropLine on 2026-11-23: AJ Green (MIL): minutes ESPN None vs PropLine 0.15'], None, None),
        ('kalshi', 'green', now - timedelta(minutes=10), 1206, 1206, None, None, None),
        ('injuries', 'green', now - timedelta(hours=5), 186, 186, None, None, None),
    ]
    jobs = summarize(rows, now)
    assert [(j['job'], j['level']) for j in jobs] == [('validate', 'red'), ('injuries', 'yellow'), ('kalshi', 'green')]
    assert 'AJ Green (MIL)' in jobs[0]['reasons'][0]
    assert jobs[1]['reasons'] == ['no run for 5 h']
    lines = status_lines({'ok': True, 'error': None, 'jobs': jobs})
    assert lines[0] == 'Worker jobs: 1/3 green' and 'RED validate' in lines[1] and 'AJ Green' in lines[1]
    assert status_lines({'ok': False, 'error': 'DatabaseUnavailable', 'jobs': []}) == [
        'Worker jobs: UNKNOWN (Supabase unreadable: DatabaseUnavailable)']


def test_worker_health_never_raises():
    from src.data.worker_health import job_status

    class Broken:
        read_only = False

        def execute(self, *a, **k):
            raise RuntimeError('boom')
    out = job_status(Broken())
    assert out['ok'] is False and out['jobs'] == [] and 'RuntimeError' in out['error']


def test_daily_dry_run_saves_nothing_and_normal_daily_saves(monkeypatch):
    calls = []
    monkeypatch.setattr(nba, 'detect_phase', lambda season: 'regular')
    monkeypatch.setattr(nba, 'refreshed_today', lambda season: False)
    monkeypatch.setattr(nba, 'run', lambda cmd, *a: calls.append(cmd) or 0)
    monkeypatch.setenv('NBA_DRY_RUN', '1')
    nba.cmd_daily(None)
    scripts = [c[0] for c in calls]
    assert 'scripts/update_data.py' not in scripts                       # refresh skipped
    assert not any('--save' in c for c in calls)                          # no picks files
    assert all('--no-save' in c for c in calls if c[0].startswith('scripts/log_'))
    calls.clear()
    monkeypatch.delenv('NBA_DRY_RUN')
    nba.cmd_daily(None)
    saved = {c[0] for c in calls if '--save' in c}
    assert saved == {'scripts/find_bets.py', 'scripts/generate_todays_team_picks.py'}
    assert not any('--no-save' in c for c in calls)


def test_nba_picks_still_saves_team_picks(monkeypatch):
    import argparse
    calls = []
    monkeypatch.setattr(nba, 'detect_phase', lambda season: 'regular')
    monkeypatch.setattr(nba, 'refreshed_today', lambda season: True)
    monkeypatch.setattr(nba, 'run', lambda cmd, *a: calls.append(cmd) or 0)
    args = argparse.Namespace(quick=True, team_only=True, props_only=False, pickem=False, lines=None,
                              lines_file=None, template=None)
    nba.cmd_picks(args)
    team = [c for c in calls if c[0] == 'scripts/generate_todays_team_picks.py']
    assert team and '--save' in team[0]


def test_pick_grader_no_save_writes_nothing(tmp_path, monkeypatch):
    import json as _json
    import sqlite3
    import log_pick_results as L
    db = tmp_path / 'db.sqlite'
    monkeypatch.setattr(L, 'DB_PATH', db)
    monkeypatch.setattr(L, 'PICKS_DIR', tmp_path / 'picks')
    market = tmp_path / 'market_picks'
    market.mkdir()
    pick = dict(player_id=1, player_name='A', team='X', opp='Y', stat='PTS', stat_key='pts', direction='OVER',
                line=10.5, model_pred=12, prob=0.6, card='', n_legs=1, line_source='', injury_flag=0)
    (market / '2026-11-20.json').write_text(_json.dumps({'picks': [pick], 'season': '2026-27'}))
    monkeypatch.setattr(L, 'fetch_actuals_from_db', lambda *a, **k: {1: {'played': True, 'pts': 15}})
    L.grade_date('2026-11-20', market=True, save=False)
    assert sqlite3.connect(db).execute('select count(*) from pick_results').fetchone()[0] == 0
    L.grade_date('2026-11-20', market=True, save=True)
    assert sqlite3.connect(db).execute('select count(*) from pick_results').fetchone()[0] == 1
