from datetime import date

import pytest

from jobkit import ESPN_TO_ABBR, Net, Resp, load, run, scoreboards
from prop_worker.jobs import schedule


def test_parse_finals():
    games, skipped = schedule.parse_scoreboard(load("espn/scoreboard_20261109.json"), ESPN_TO_ABBR)
    assert len(games) == 8 and skipped == []
    g = next(x for x in games if x["espn_event_id"] == "401909952")
    assert (g["home_team"], g["away_team"], g["home_score"], g["away_score"]) == ("ATL", "LAL", 114, 124)
    assert g["status"] == "final" and g["game_date"] == date(2026, 11, 9)       # 00:00Z = 7 pm ET the day before
    assert (g["season"], g["season_type"]) == ("2026-27", "Regular Season")
    assert g["kalshi_event"] == "26NOV09LALATL"
    gsw = next(x for x in games if x["espn_event_id"] == "401909959")
    assert {gsw["home_team"], gsw["away_team"]} == {"GSW", "NOP"}               # ESPN GS / NO -> canonical


def test_parse_scheduled_has_no_scores():
    games, _ = schedule.parse_scoreboard(load("espn/scoreboard_20261110.json"), ESPN_TO_ABBR)
    assert len(games) == 6 and {g["status"] for g in games} == {"scheduled"}
    assert all(g["home_score"] is None for g in games)
    assert next(g for g in games if g["home_team"] == "UTA")["kalshi_event"] == "26NOV10MIAUTA"


def test_empty_game_day():
    assert schedule.parse_scoreboard(load("espn/scoreboard_20260801.json"), ESPN_TO_ABBR) == ([], [])


def test_non_nba_teams_are_skipped_and_listed():
    sb = load("espn/scoreboard_20261110.json")
    sb["events"][0]["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "EAST"
    games, skipped = schedule.parse_scoreboard(sb, ESPN_TO_ABBR)
    assert len(games) == 5 and len(skipped) == 1 and "EAST" in skipped[0]


@pytest.mark.parametrize("status,expected", [
    ({"type": {"name": "STATUS_FINAL", "state": "post", "completed": True}}, "final"),
    ({"type": {"name": "STATUS_IN_PROGRESS", "state": "in"}}, "in_progress"),
    ({"type": {"name": "STATUS_HALFTIME", "state": "in"}}, "in_progress"),
    ({"type": {"name": "STATUS_SCHEDULED", "state": "pre"}}, "scheduled"),
    ({"type": {"name": "STATUS_POSTPONED", "state": "post"}}, "postponed"),
    ({"type": {"name": "STATUS_CANCELED", "state": "post"}}, "canceled"),
])
def test_status_mapping(status, expected):
    assert schedule.game_status(status) == expected


def test_job_upserts_and_attaches_espn_id_to_backfilled_game(pg):
    pg.execute("""insert into games (game_date, home_team, away_team, status, nba_game_id, source)
                  values ('2026-11-09', 'ATL', 'LAL', 'final', '0022600150', 'nba_edge_db')""")
    pg.commit()
    net = Net([scoreboards()])
    out = run(pg, net, "schedule", schedule.run)
    assert out.status == "green" and out.rows_out == 14
    assert len([c for c in net.calls if c[0].endswith("/scoreboard")]) == 7          # yesterday .. +5 days
    row = pg.execute("select nba_game_id, espn_event_id, source from games where home_team = 'ATL'").fetchone()
    assert row == ("0022600150", "401909952", "espn")
    assert pg.execute("select count(*) from games").fetchone()[0] == 14
    run(pg, net, "schedule", schedule.run)                                           # idempotent
    assert pg.execute("select count(*) from games").fetchone()[0] == 14


def test_rescheduled_event_moves_its_row(pg):
    net = Net([scoreboards()])
    run(pg, net, "schedule", schedule.run)
    moved = load("espn/scoreboard_20261110.json")
    moved["events"][0]["date"] = "2026-11-12T00:30Z"            # MIL@NYK pushed two days
    net = Net([scoreboards({"20261110": moved, "20261109": load("espn/scoreboard_20261109.json")})])
    run(pg, net, "schedule", schedule.run)
    rows = pg.execute("select game_date from games where espn_event_id = '401909960'").fetchall()
    assert rows == [(date(2026, 11, 11),)]


def test_empty_week_is_not_a_game_day_and_stays_green(pg):
    empty = load("espn/scoreboard_20260801.json")
    out = run(pg, Net([(lambda u, p: True, Resp(empty))]), "schedule", schedule.run)
    assert out.status == "green" and out.rows_out == 0 and out.is_game_day is False
