from datetime import date

from jobkit import ESPN_TO_ABBR, Net, Resp, load, run, scoreboards, seed_players
from prop_worker import xref
from prop_worker.jobs import boxscores, schedule

SUMMARY = load("espn/summary_401909952.json")
ROSTER = [(i, a["athlete"]["displayName"], t["team"]["abbreviation"])
          for t in SUMMARY["boxscore"]["players"]
          for i, a in enumerate(t["statistics"][0]["athletes"], start=len(t["team"]["abbreviation"]) * 1000)]


def _summary_route(payload=None):
    return (lambda u, p: u.endswith("/summary"), Resp(payload or load("espn/summary_401909952.json")))


def _only_atl_final():
    sb = load("espn/scoreboard_20261109.json")
    sb["events"] = [e for e in sb["events"] if e["id"] == "401909952"]
    return scoreboards({"20261109": sb, "20261110": load("espn/scoreboard_20261110.json")})


def _seed(pg, skip=()):
    rows = [(1_000_000 + n, name, team) for n, (_, name, team) in enumerate(ROSTER) if name not in skip]
    seed_players(pg, rows)


def test_parse_summary_rows_dnps_and_splits():
    rows, problems = boxscores.parse_summary(load("espn/summary_401909952.json"), ESPN_TO_ABBR)
    assert problems == [] and len(rows) == 26
    luka = next(r for r in rows if r["name"] == "Luka Doncic")
    assert luka["team_abbr"] == "LAL" and luka["opp_abbr"] == "ATL" and luka["is_home"] is False
    assert luka["pts"] == 32 and luka["fga"] >= luka["fgm"] and luka["fg3a"] >= luka["fg3m"]
    assert luka["reb"] == luka["oreb"] + luka["dreb"]
    dnps = [r for r in rows if r["did_not_play"]]
    assert len(dnps) == 6 and all(r["pts"] is None and r["min"] is None for r in dnps)
    assert dnps[0]["dnp_reason"]
    assert sum(r["pts"] or 0 for r in rows if r["team_abbr"] == "ATL") == 114


def test_points_that_dont_add_up_stop_the_game():
    bad = load("espn/summary_401909952.json")
    bad["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"][1] = "40"
    _, problems = boxscores.parse_summary(bad, ESPN_TO_ABBR)
    assert problems and "final score is 124" in problems[0]


def test_loads_finals_and_holds_unmatched_player(pg):
    _seed(pg, skip={"Luka Doncic"})
    net = Net([_only_atl_final(), _summary_route()])
    run(pg, net, "schedule", schedule.run)
    out = run(pg, net, "boxscores", boxscores.run)
    assert out.status == "green" and out.rows_out == 26 and out.volume == 26
    assert pg.execute("select count(*) from game_logs").fetchone()[0] == 25
    assert any("pending_rows" in w for w in out.warnings)
    held = pg.execute("select dedupe_key, external_name, payload->>'pts' from pending_rows").fetchall()
    assert held == [("espn:401909952:3945274", "Luka Doncic", "32")]
    (um,) = xref.unmatched_rows(pg)
    assert um[:3] == ("espn", "3945274", "Luka Doncic")

    # Next run: nothing new to load (not expected -> green), and once Luka exists the held row lands.
    again = run(pg, net, "boxscores", boxscores.run)
    assert again.status == "green" and again.expected is False and again.rows_out == 0
    seed_players(pg, [(1629029, "Luka Doncic", "LAL")])
    fixed = run(pg, net, "boxscores", boxscores.run)
    assert fixed.meta["pending_reapplied"] == 1
    assert pg.execute("select pts from game_logs l join players p using (player_id) "
                      "where p.full_name = 'Luka Doncic'").fetchone()[0] == 32
    assert pg.execute("select resolved_at is not null from pending_rows").fetchone()[0]


def test_mismatched_box_score_is_red_and_not_loaded(pg):
    _seed(pg)
    bad = load("espn/summary_401909952.json")
    bad["boxscore"]["players"][0]["statistics"][0]["athletes"][0]["stats"][1] = "40"
    net = Net([_only_atl_final(), _summary_route(bad)])
    run(pg, net, "schedule", schedule.run)
    out = run(pg, net, "boxscores", boxscores.run)
    assert out.status == "red" and "not loaded" in out.red_reasons[0]
    assert pg.execute("select count(*) from game_logs").fetchone()[0] == 0


def test_never_overwrites_nba_edge_db_rows(pg):
    _seed(pg)
    net = Net([_only_atl_final(), _summary_route()])
    run(pg, net, "schedule", schedule.run)
    gid = pg.execute("select game_id from games where espn_event_id = '401909952'").fetchone()[0]
    pid = pg.execute("select player_id from players where full_name = 'Luka Doncic'").fetchone()[0]
    pg.execute("""insert into game_logs (game_id, player_id, team_abbr, opp_abbr, is_home, min, pts, source)
                  values (%s, %s, 'LAL', 'ATL', false, 36.5, 31, 'nba_edge_db')""", (gid, pid))
    pg.commit()
    run(pg, net, "boxscores", boxscores.run, date=date(2026, 11, 9))
    assert pg.execute("select pts, min, source from game_logs where player_id = %s", (pid,)).fetchone() == \
        (31, 36.5, "nba_edge_db")
    assert pg.execute("select count(*) from game_logs").fetchone()[0] == 26
