"""Injury guard for team picks: thresholds, downgrade, saved flag, sources. GSW 11/13 and 11/16 regressions."""
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'scripts')]
from src.models import injury_guard as G

# Worker data as it stood on 11/13 and 11/16 (Supabase injuries + game_logs): Butler, Moody and Porziņģis
# haven't played this season, so only Curry and Brandon Williams are recent absences.
GSW_OUT = [
    {'name': 'Jimmy Butler III', 'status': 'Out', 'ppg': 20.1, 'mpg': 31.07, 'last_played': '2026-01-19'},
    {'name': 'Moses Moody', 'status': 'Out', 'ppg': 10.4, 'mpg': 29.27, 'last_played': '2026-03-23'},
    {'name': 'Stephen Curry', 'status': 'Out', 'ppg': 25.5, 'mpg': 27.76, 'last_played': '2026-11-11'},
    {'name': 'Kristaps Porziņģis', 'status': 'Out', 'ppg': 16.0, 'mpg': 24.0, 'last_played': '2026-04-17'},
    {'name': 'Brandon Williams', 'status': 'Out', 'ppg': 12.2, 'mpg': 19.0, 'last_played': '2026-11-09'},
]
# everyone who played for GSW in the 14 days before 11/13 (top 6 by ppg) and GSW's team ppg
GSW_POOL = [{'name': 'Stephen Curry', 'ppg': 25.5}, {'name': 'Yaxel Lendeborg', 'ppg': 21.7},
            {'name': 'Brandin Podziemski', 'ppg': 18.0}, {'name': 'Brandon Williams', 'ppg': 12.2},
            {'name': "De'Anthony Melton", 'ppg': 9.9}, {'name': 'Georges Niang', 'ppg': 9.8}]
GSW_TEAM_PPG = {'2026-11-13': 117.6, '2026-11-16': 115.9}
D = date(2026, 11, 16)


def ab(name, ppg, mpg, last='2026-11-14', status='Out'):
    return {'name': name, 'status': status, 'ppg': ppg, 'mpg': mpg, 'last_played': last}


def pool(*pairs):
    return [{'name': n, 'ppg': p} for n, p in pairs]


def saved_pick(home, away, spread, sp_edge, total, ou_edge):
    """The fields of a data/team_picks record the guard reads, as saved on those nights."""
    pick_team = away if sp_edge < 0 else home
    return {'home': home, 'away': away, 'tier': 'LEAN', 'ats_strength': 'LEAN', 'ml_strength': 'NONE',
            'spread_pick_team': pick_team, 'spread_pick_side': 'away' if pick_team == away else 'home',
            'market_spread': spread, 'sp_edge': sp_edge, 'ou_tier': 'STRONG', 'ou_direction': 'OVER',
            'over_under': total, 'ou_edge': ou_edge}


# ── rule ────────────────────────────────────────────────────────────────────

def test_top2_scorer_rule_alone():
    team = pool(('Star', 24), ('Second', 18), ('Third', 15), ('Bench', 6))
    f = G.team_flag([ab('Second', 18, 20)], team, 120.0, D)              # 15% share, 20 mpg: only top-2 fires
    assert f and f['reason'] == 'Second top-2 scorer' and f['top2'] == ['Star', 'Second']
    assert G.team_flag([ab('Third', 15, 20)], team, 120.0, D) is None     # third scorer: 12.5%, 20 mpg


def test_share_rule_alone():
    team = pool(('Star', 24), ('Second', 18), ('A', 12.5), ('B', 11.5))
    f = G.team_flag([ab('A', 12.5, 25), ab('B', 11.5, 25)], team, 120.0, D)
    assert f and f['reason'] == '20% of team points (>= 20%)' and f['share'] == 0.2
    assert G.team_flag([ab('A', 12.5, 25), ab('B', 11.4, 25)], team, 120.0, D) is None   # 19.9%


def test_minutes_rule_alone():
    team = pool(('Star', 24), ('Second', 18))
    f = G.team_flag([ab('A', 3, 30), ab('B', 3, 30)], team, 120.0, D)
    assert f and f['reason'] == '60 mpg out (>= 60)'
    assert G.team_flag([ab('A', 3, 30), ab('B', 3, 29.9)], team, 120.0, D) is None


def test_only_recent_absences_count_and_status_filter():
    team = pool(('Star', 30), ('Second', 20))
    assert G.team_flag([ab('Star', 30, 36, last='2026-11-01')], team, 110.0, D) is None     # 15 days ago
    assert G.team_flag([ab('Star', 30, 36, last='2026-11-02')], team, 110.0, D)             # 14 days: counts
    assert G.team_flag([ab('Star', 30, 36, last=None)], team, 110.0, D) is None             # never played
    assert G.team_flag([ab('Star', 30, 36, status='Doubtful')], team, 110.0, D)
    for status in ('Questionable', 'Day-To-Day', 'Probable'):
        assert G.team_flag([ab('Star', 30, 36, status=status)], team, 110.0, D) is None
    assert G.team_flag([], team, 110.0, D) is None
    assert G.team_flag([ab('X', None, None)], team, None, D) is None                        # no numbers, no flag


def test_gsw_long_term_absences_alone_are_not_blind():
    long_term = [a for a in GSW_OUT if a['name'] in ('Jimmy Butler III', 'Moses Moody', 'Kristaps Porziņģis')]
    assert G.team_flag(long_term, GSW_POOL, 117.6, date(2026, 11, 13)) is None


@pytest.mark.parametrize('night, home, away, spread, sp_edge, total, ou_edge', [
    ('2026-11-13', 'SAS', 'GSW', -11.5, -5.1, 217.5, 9.5),     # LEAN GSW +11.5, STRONG Over 217.5
    ('2026-11-16', 'NOP', 'GSW', 3.5, -4.38, 223.5, 7.9),      # LEAN GSW -3.5 (lost by 21), STRONG Over 223.5 (209)
])
def test_gsw_regression_blind_because_curry_is_a_top2_scorer(night, home, away, spread, sp_edge, total, ou_edge):
    d = date.fromisoformat(night)
    f = G.team_flag(GSW_OUT, GSW_POOL, GSW_TEAM_PPG[night], d)
    assert f['reason'].startswith('Stephen Curry top-2 scorer')
    assert [p['name'] for p in f['players']] == ['Stephen Curry', 'Brandon Williams']    # recent ones only
    curry_only = G.team_flag([a for a in GSW_OUT if a['name'] == 'Stephen Curry'], GSW_POOL, GSW_TEAM_PPG[night], d)
    assert curry_only['reason'].startswith('Stephen Curry top-2 scorer')                  # Curry alone is enough
    flags = {'GSW': f, home: None}
    a = G.apply_guard(saved_pick(home, away, spread, sp_edge, total, ou_edge), flags, 'worker injuries')
    assert a['tier'] == 'MONITOR' and a['ats_strength'] == 'MONITOR' and a['ou_tier'] == 'MONITOR'
    assert a['spread_pick_team'] is None and a['spread_pick_side'] is None              # no spread pick
    ib = a['injury_blind']
    assert list(ib['teams']) == ['GSW'] and ib['source'] == 'worker injuries'
    assert ib['before_guard'] == {'tier': 'LEAN', 'ats_strength': 'LEAN', 'ou_tier': 'STRONG',
                                  'spread_pick_team': 'GSW', 'spread_pick_side': 'away'}
    line = G.describe(a)
    assert line.startswith(f'INJURY_BLIND GSW @ {home}') and 'Stephen Curry 26p/28m' in line
    assert 'spread LEAN, total STRONG downgraded to MONITOR' in line
    assert json.loads(json.dumps(a))['injury_blind'] == ib                               # saved in the pick JSON


def test_either_side_blind_downgrades_the_game_and_clean_games_are_untouched():
    a = saved_pick('NOP', 'GSW', 3.5, 4.0, 223.5, 7.9)                                  # pick is NOP here
    G.apply_guard(a, {'GSW': None, 'NOP': G.team_flag([ab('x', 3, 61)], [], 110.0, D)})
    assert a['tier'] == 'MONITOR' and list(a['injury_blind']['teams']) == ['NOP']
    clean = saved_pick('BOS', 'NYK', -2.5, 4.0, 220.5, 7.5)
    G.apply_guard(clean, {'BOS': None, 'NYK': None})
    assert clean['tier'] == 'LEAN' and clean['ou_tier'] == 'STRONG' and 'injury_blind' not in clean
    assert G.describe(clean) == ''


def test_non_picks_keep_their_tier_but_carry_the_flag():
    a = {'home': 'NOP', 'away': 'GSW', 'tier': 'NONE', 'ats_strength': 'MONITOR', 'ou_tier': 'NONE',
         'spread_pick_team': None, 'spread_pick_side': None}
    G.apply_guard(a, {'GSW': G.team_flag(GSW_OUT, GSW_POOL, 115.9, D)})
    assert (a['tier'], a['ats_strength'], a['ou_tier']) == ('NONE', 'MONITOR', 'NONE')
    assert a['injury_blind'] and 'downgraded' not in G.describe(a)


# ── sources ─────────────────────────────────────────────────────────────────

def test_espn_snapshot_parsing_keeps_out_and_doubtful_for_slate_teams():
    raw = {'injuries': [{'id': '9', 'injuries': [
        {'status': 'Out', 'athlete': {'displayName': 'Stephen Curry', 'team': {'abbreviation': 'GS'}}},
        {'status': 'Day-To-Day', 'athlete': {'displayName': 'Draymond Green', 'team': {'abbreviation': 'GS'}}},
        {'status': 'Doubtful', 'athlete': {'displayName': 'Moses Moody', 'team': {'abbreviation': 'GS'}}}]},
        {'id': '2', 'injuries': [
        {'status': 'Out', 'athlete': {'displayName': 'Jayson Tatum', 'team': {'abbreviation': 'BOS'}}}]}]}
    out = G.espn_snapshot_absences(raw, {'GSW', 'NOP'})
    assert [r['name'] for r in out['GSW']] == ['Stephen Curry', 'Moses Moody'] and out['NOP'] == []
    assert 'BOS' not in out                                                              # not on the slate


def test_local_fallback_player_and_team_numbers():
    conn = sqlite3.connect(':memory:')
    conn.execute('create table players (player_id integer, full_name text)')
    conn.execute('create table game_logs (player_id integer, game_id text, game_date text, team_abbr text, '
                 'min real, pts integer)')
    conn.executemany('insert into players values (?, ?)', [(1, 'Kristaps Porzingis'), (2, 'Other Guy')])
    rows = [(1, f'g{d}', f'2026-10-{d:02d}', 'GSW', 20.0, 10) for d in range(1, 16)]
    rows += [(1, 'g16', '2026-10-16', 'GSW', 0.0, 0), (1, 'g17', '2026-11-10', 'GSW', 30.0, 40),
             (1, 'g99', '2026-11-20', 'GSW', 99.0, 99)]                             # 0 min skipped, future ignored
    rows += [(2, 'g17', '2026-11-10', 'GSW', 30.0, 60)]
    conn.executemany('insert into game_logs values (?, ?, ?, ?, ?, ?)', rows)
    ppg, mpg, last = G.local_player(conn, 'Kristaps Porziņģis', '2026-11-16')
    assert ppg == pytest.approx((14 * 10 + 40) / 15) and mpg == pytest.approx((14 * 20 + 30) / 15)
    assert last == '2026-11-10'
    assert G.local_player(conn, 'Nobody', '2026-11-16') == (None, None, None)
    team, team_ppg = G.local_team(conn, 'GSW', D)
    assert {p['name'] for p in team} == {'Kristaps Porzingis', 'Other Guy'}             # played in last 14 days
    assert team_ppg == pytest.approx((100 + 0 + 13 * 10) / 15)                         # g17 (40 + 60), g16, g3-g15


def test_worker_first_then_espn_snapshot_then_none(monkeypatch):
    d, teams = D, {'GSW', 'NOP'}
    gsw = {'absent': GSW_OUT, 'pool': GSW_POOL, 'team_ppg': 115.9}
    nop = {'absent': [], 'pool': [], 'team_ppg': 110.0}
    monkeypatch.setattr(G, 'from_worker', lambda *a, **k: {'GSW': gsw, 'NOP': nop})
    monkeypatch.setattr(G, 'from_espn_snapshot', lambda *a, **k: (_ for _ in ()).throw(AssertionError('not reached')))
    flags, src, errs = G.team_flags(d, teams, Path('x'))
    assert src == 'worker injuries' and flags['GSW'] and flags['NOP'] is None and errs == []

    def down(*a, **k):
        raise ConnectionError('could not connect to postgresql://user:secret@host/db')
    monkeypatch.setattr(G, 'from_worker', down)
    monkeypatch.setattr(G, 'from_espn_snapshot', lambda *a, **k: {'GSW': gsw, 'NOP': nop})
    flags, src, errs = G.team_flags(d, teams, Path('x'))
    assert src == 'ESPN snapshot' and flags['GSW'] and 'secret' not in errs[0]          # error text redacted

    monkeypatch.setattr(G, 'from_espn_snapshot', lambda *a, **k: (_ for _ in ()).throw(LookupError('none')))
    flags, src, errs = G.team_flags(d, teams, Path('x'))
    assert src == 'none' and flags == {'GSW': None, 'NOP': None} and len(errs) == 2


def test_cutoff_is_now_for_today_and_next_6am_et_for_past_slates():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)
    assert G._cutoff(date(2026, 11, 17), now) == now
    assert G._cutoff(date(2026, 11, 16), now) == datetime(2026, 11, 17, 11, 0, tzinfo=timezone.utc)   # 6am EST
