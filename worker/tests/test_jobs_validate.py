"""10-player validation: ESPN game_logs vs PropLine box score (saved real responses for the same game)."""

import sqlite3
from datetime import date, datetime

import pytest

from jobkit import Net, Resp, load, run as _run, scoreboards, seed_players
from prop_worker import config
from prop_worker.jobs import boxscores, schedule, validate

NOW = datetime(2026, 11, 10, 6, 5, tzinfo=config.ET)           # the 6am nightly after GSW-NOP (Nov 9)
SUMMARY = load("espn/summary_401909959.json")
STATS = load("propline/event_stats_641560.json")
ESPN_TEAM = {"GS": "GSW", "NO": "NOP"}


def run(pg, net, name, fn, **kw):
    return _run(pg, net, name, fn, now=NOW, **kw)


def _net(stats=None, summary=None):
    sb = load("espn/scoreboard_20261109.json")
    sb["events"] = [e for e in sb["events"] if e["id"] == "401909959"]
    return Net([scoreboards({"20261109": sb}),
                (lambda u, p: u.endswith("/summary"), Resp(summary or load("espn/summary_401909959.json"))),
                (lambda u, p: u.endswith("/scores"), Resp([{"id": "641560", "commence_time": "2026-11-10T03:00:00Z",
                                                            "home_team_id": "nba:1610612744", "status": "final"}])),
                (lambda u, p: u.endswith("/events/641560/stats"), Resp(stats or load("propline/event_stats_641560.json")))])


def _loaded(pg, net=None):
    net = net or _net()
    seed_players(pg, [(9_000_000 + 100 * k + i, a["athlete"]["displayName"], ESPN_TEAM[t["team"]["abbreviation"]])
                      for k, t in enumerate(SUMMARY["boxscore"]["players"])
                      for i, a in enumerate(t["statistics"][0]["athletes"])])
    run(pg, net, "schedule", schedule.run)
    out = run(pg, net, "boxscores", boxscores.run)
    assert out.status == "green", out.red_reasons
    return net


def test_parse_propline_stats_maps_our_columns():
    players = validate.parse_propline_stats(STATS)
    assert len(players) == 24 and {t for t, _ in players} == {"GSW", "NOP"}
    p = players[("NOP", "AJ Johnson")]
    assert (p["pts"], p["reb"], p["ast"], p["min"], p["fga"]) == (2.0, 2.0, 3.0, 12.0, 3.0)


def test_find_in_matches_accents_within_team_only():
    players = validate.parse_propline_stats(STATS)
    assert validate.find_in(players, "Karlo Matkovic", "NOP") is players[("NOP", "Karlo Matković")]
    assert validate.find_in(players, "Karlo Matkovic", "GSW") is None


def test_espn_matches_propline_for_ten_random_players(pg):
    net = _loaded(pg)
    out = run(pg, net, "validate", validate.run, propline_key="k")
    assert out.status == "green", out.red_reasons
    assert out.rows_in == 10 and out.rows_out == 10 and out.meta["date"] == "2026-11-09"
    again = run(pg, net, "validate", validate.run, propline_key="k")
    assert again.meta["players"] == out.meta["players"]         # same seed -> same players
    assert any(c[0].endswith("/scores") for c in net.calls)     # event found via /scores (no propline_event_id)


def test_any_stat_mismatch_is_red(pg):
    net = _loaded(pg)
    stats = load("propline/event_stats_641560.json")
    for r in stats["stats"]:
        if r["stat_type"] == "points" and r["player_name"]:
            r["stat_value"] += 1                                 # every player off by one point
    out = run(pg, _net(stats=stats), "validate", validate.run, propline_key="k")
    assert out.status == "red" and "10 mismatch(es) vs PropLine on 2026-11-09" in out.red_reasons[0]
    assert all("pts ESPN" in m for m in out.meta["mismatches"]) and net is not None


def test_minutes_within_one_is_fine_beyond_is_red():
    espn = {"name": "X", "team": "NOP", "min": 30, **{c: 1 for c in validate.EXACT}}
    other = {"min": 30.9, **{c: 1.0 for c in validate.EXACT}}
    assert validate.compare(espn, other, "PropLine") == []
    assert "minutes ESPN 30 vs PropLine 31.5" in validate.compare(espn, dict(other, min=31.5), "PropLine")[0]


def test_player_missing_from_second_source_is_red(pg):
    _loaded(pg)
    stats = load("propline/event_stats_641560.json")
    stats["stats"] = [r for r in stats["stats"] if r["team_abbr"] != "NOP"]
    out = run(pg, _net(stats=stats), "validate", validate.run, propline_key="k")
    assert out.status == "red" and any("not found in PropLine" in m for m in out.meta["mismatches"])


def test_no_games_last_night_is_green_not_expected(pg):
    out = run(pg, Net(), "validate", validate.run, propline_key="k")
    assert out.status == "green" and out.expected is False


def test_finals_without_espn_rows_is_red(pg):
    run(pg, _net(), "schedule", schedule.run)                    # final known, box score never loaded
    out = run(pg, _net(), "validate", validate.run, propline_key="k")
    assert out.status == "red" and "no ESPN game_logs rows" in out.red_reasons[0]


def _sqlite(tmp_path, rows):
    path = tmp_path / "nba_edge.db"
    db = sqlite3.connect(path)
    db.execute(f"create table game_logs (player_id int, game_date text, min real, plus_minus real, "
               f"{', '.join(c + ' int' for c in validate.EXACT)})")
    db.executemany(f"insert into game_logs values ({', '.join('?' * (4 + len(validate.EXACT)))})", rows)
    db.commit()
    db.close()
    return str(path)


def test_against_nba_edge_db(pg, tmp_path):
    _loaded(pg)
    logs = pg.execute(f"""select x.external_id::int, l.min, l.plus_minus, {", ".join("l." + c for c in validate.EXACT)}
                          from game_logs l join player_xref x on x.player_id = l.player_id and x.source = 'nba'
                          where not l.did_not_play""").fetchall()
    rows = [(nid, "2026-11-09", float(m) + 0.4, pm, *rest) for nid, m, pm, *rest in logs]
    out = run(pg, Net(), "validate", validate.run, against="nba_edge_db", sqlite=_sqlite(tmp_path, rows))
    assert out.status == "green" and out.rows_out == 10, out.red_reasons

    bad = [(r[0], r[1], r[2], (r[3] or 0) + 5, *r[4:]) for r in rows]        # plus/minus differs
    out = run(pg, Net(), "validate", validate.run, against="nba_edge_db", sqlite=_sqlite_fresh(tmp_path, bad))
    assert out.status == "red" and all("plus_minus" in m for m in out.meta["mismatches"])


def _sqlite_fresh(tmp_path, rows):
    sub = tmp_path / "second"
    sub.mkdir(exist_ok=True)
    return _sqlite(sub, rows)


def test_nba_edge_db_without_that_night_says_so(pg, tmp_path):
    _loaded(pg)
    out = run(pg, Net(), "validate", validate.run, against="nba_edge_db", sqlite=_sqlite(tmp_path, []))
    assert out.status == "red" and "run the old pipeline's ingestion" in out.red_reasons[0]


@pytest.mark.parametrize("d", [date(2026, 11, 9)])
def test_explicit_date(pg, d):
    net = _loaded(pg)
    out = run(pg, net, "validate", validate.run, propline_key="k", date=d)
    assert out.meta["date"] == d.isoformat()
