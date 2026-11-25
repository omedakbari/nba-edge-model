"""Today health panel tiles (Phase 4a): one per source, green/yellow/red, last success, 1b statuses reflected."""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dashboard')]
import health_view as H  # noqa: E402

TODAY = date(2026, 11, 25)


def job(name, level='green', reasons=(), last='2026-11-25T14:00:00+00:00'):
    return {'job': name, 'level': level, 'reasons': list(reasons), 'last_green': last, 'status': level}


HEALTH = {'ok': True, 'error': None, 'jobs': [
    job('boxscores'), job('validate', 'red', ['1 mismatch(es) vs PropLine on 2026-11-23: AJ Green (MIL)']),
    job('rosters'), job('injuries'), job('kalshi'), job('propline'), job('grade'), job('bets')]}


def tiles(**kw):
    base = dict(health=HEALTH, today=TODAY, local_max_game_date='2026-11-24', card={'injury': {'stale': False}},
                feed_lines=['DK via ESPN: 0/506 passed (406 stale, 100 future-skew)'], grading_lag=[],
                team_last_graded='2026-11-24', calibration_warnings=['calibration fit on 14 slates (< 20): provisional'],
                calibration_fit='2026-11-25T10:28:39+00:00', run={'exit': 3, 'started': '2026-11-25T00:42:59+00:00'})
    base.update(kw)
    return {t['source']: t for t in H.sources(**base)}


def test_every_source_has_a_tile():
    assert list(tiles()) == ['Game logs', 'Rosters', 'Injuries', 'DK (ESPN)', 'Kalshi', 'PropLine / Sleeper',
                             'Grading', 'Calibration', 'Daily run']


def test_levels_reflect_todays_problems():
    t = tiles()
    assert t['Game logs']['level'] == 'red' and 'AJ Green' in t['Game logs']['detail']
    assert t['DK (ESPN)']['level'] == 'yellow' and 'legacy' in t['DK (ESPN)']['detail']
    assert t['Calibration']['level'] == 'yellow' and t['Daily run']['level'] == 'yellow'
    assert t['Kalshi']['level'] == 'green' and t['Kalshi']['last_success'] == '2026-11-25T14:00:00+00:00'
    assert t['Grading']['level'] == 'green' and t['Grading']['last_success'] == '2026-11-24'


def test_stale_local_db_grading_lag_injury_stale_missing_calibration_failed_run():
    t = tiles(local_max_game_date='2026-11-20', grading_lag=['team grading lags 3 game day(s) since 2026-11-19: x'],
              card={'injury': {'stale': True}}, calibration_warnings=['calibration artifact missing (x)'],
              run={'exit': 2, 'started': 'x'})
    assert t['Game logs']['level'] == 'red' and '4 days behind' in t['Game logs']['detail']
    assert t['Grading']['level'] == 'yellow' and t['Injuries']['level'] == 'red'
    assert t['Calibration']['level'] == 'red' and t['Daily run']['level'] == 'red'


def test_supabase_unreadable_is_unknown_not_green():
    t = tiles(health={'ok': False, 'error': 'DatabaseUnavailable', 'jobs': []})
    assert t['Kalshi']['level'] == 'unknown' and 'unreadable' in t['Kalshi']['detail']


def test_panel_html_escapes_and_shows_last_success():
    html = H.panel_html(list(tiles().values()), lambda s: str(s).replace('<', '&lt;'), lambda iso: '3h ago')
    assert html.count('class="htile') == 9 and 'last ok: 3h ago' in html and 'last ok: 2026-11-24' in html
