"""Player Lookup, current season (Phase 4c)."""
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dashboard')]
import player_view as P  # noqa: E402


def _db(tmp_path):
    db = tmp_path / 'db.sqlite'
    c = sqlite3.connect(db)
    c.execute('create table players (player_id int, full_name text)')
    c.execute('''create table game_logs (player_id int, game_date text, season text, season_type text, matchup text,
                 opp_abbr text, min real, pts int, reb int, ast int, fg3m int)''')
    c.executemany('insert into players values (?, ?)', [(1, 'Nikola Jokić'), (2, 'Bench Guy')])
    rows = [(1, f'2026-11-{d:02d}', '2026-27', 'Regular Season', 'DEN vs. X', 'DAL', 36, 25 + i, 12, 10 + (i % 3), 1)
            for i, d in enumerate(range(1, 23))]
    rows += [(1, '2025-04-01', '2024-25', 'Regular Season', 'DEN vs. Y', 'LAL', 30, 99, 9, 9, 9),
             (2, '2026-11-02', '2026-27', 'Regular Season', 'x', 'y', 0, 0, 0, 0, 0)]
    c.executemany('insert into game_logs values (?,?,?,?,?,?,?,?,?,?,?)', rows)
    c.commit()
    return c


def test_current_season_players_and_log(tmp_path):
    c = _db(tmp_path)
    assert P.current_season(c) == '2026-27'
    assert P.players(c, '2026-27') == {'Nikola Jokić': 1}                      # 0-minute player left out
    log = P.game_log(c, 1, '2026-27')
    assert len(log) == 22 and log.iloc[0]['Date'] == '2026-11-22' and 99 not in set(log['PTS'])
    assert list(log.columns[:8]) == ['Date', 'Opp', 'Matchup', 'MIN', 'PTS', 'REB', 'AST', '3PM']
    assert (log['PRA'] == log['PTS'] + log['REB'] + log['AST']).all()
    avg = P.averages(log)
    assert list(avg['Window']) == ['L5', 'L10', 'L20', 'Season'] and list(avg['Games']) == [5, 10, 20, 22]
    assert avg.iloc[0]['PTS'] == log.head(5)['PTS'].mean()


CARD = {'legs': [{'player': 'Nikola Jokić', 'platform': 'kalshi', 'stat': 'AST', 'line': '12+', 'side': 'NO',
                  'injury': {'code': 'P', 'status': 'Probable', 'source': 'official', 'as_of': 'T'}},
                 {'player': 'Nikola Jokic', 'platform': 'sleeper', 'stat': 'PTS', 'line': '29.5', 'side': 'OVER'}],
        'kalshi_ladders': [{'player': 'Nikola Jokić', 'stat': 'REB', 'strikes': [
            {'strike': 10, 'yes_ask': 0.8, 'p_cal_yes': 0.82}, {'strike': 14, 'yes_ask': 0.2, 'p_cal_yes': None}]}],
        'excluded': [{'player': 'Cade Cunningham', 'reason': 'injury', 'detail': 'OUT (Out, official as of T)'}],
        'injury': {'official_as_of': 'T1', 'espn_as_of': 'T2'}}


def test_props_tonight_and_hit_rates(tmp_path):
    log = P.game_log(_db(tmp_path), 1, '2026-27')
    props = P.props_tonight(CARD, 'Nikola Jokic')
    assert [p['market'] for p in props] == ['AST 12+', 'PTS over 29.5', 'REB 10+', 'REB 14+']
    t = P.hit_table(log, props)
    ast = t[t['Market'] == 'AST 12+'].iloc[0]                  # NO on 12+ = under 11.5; AST is 10/11/12 cycling
    assert ast['Side'] == 'under' and ast['L5'] == f"{int((log.head(5)['AST'] < 11.5).sum())}/5"
    reb = t[t['Market'] == 'REB 10+'].iloc[0]
    assert reb['Season'] == '22/22' and reb['P cal (YES)'] == 0.82


def test_injury_status_sources():
    assert P.injury_status(CARD, 'Cade Cunningham').startswith('EXCLUDED tonight — OUT')
    assert P.injury_status(CARD, 'Nikola Jokic').startswith('P (Probable, official as of T)')
    assert P.injury_status(CARD, 'Someone Else').startswith("Not on tonight's injury reports (official as of T1")
    assert P.injury_status(None, 'x') == 'No card for today yet'
