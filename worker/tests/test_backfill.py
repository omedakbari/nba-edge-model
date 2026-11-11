"""Backfill nba_edge.db -> Postgres: counts must match, unmatched players are held, never dropped."""

import sqlite3
from datetime import timezone

import pytest

from jobkit import seed_players
from prop_worker import backfill

COLS = ("player_id", "game_id", "game_date", "season", "season_type", "matchup", "is_home", "team_abbr",
        "opp_abbr", "wl", "min", "pts", "reb", "ast", "stl", "blk", "tov", "pf", "fgm", "fga", "fg3m", "fg3a",
        "ftm", "fta", "oreb", "dreb", "plus_minus", "inserted_at")


def _row(pid, gid, date, team, opp, home, pts, season="2026-27", stype="Regular Season", mins=31.5, pm=4.0):
    matchup = f"{team} vs. {opp}" if home else f"{team} @ {opp}"
    return (pid, gid, date, season, stype, matchup, int(home), team, opp, "W", mins, pts, 5, 3, 1, 0, 2, 2,
            8, 15, 2, 6, 2, 2, 1, 4, pm, "2026-11-09 08:00:00")


ROWS = [
    _row(1628973, "0022600150", "2026-11-09", "NYK", "BOS", True, 30),
    _row(1630540, "0022600150", "2026-11-09", "NYK", "BOS", True, 9),
    _row(1628369, "0022600150", "2026-11-09", "BOS", "NYK", False, 27),
    _row(1628973, "0022500900", "2026-03-02", "NYK", "MIA", False, 22, season="2025-26"),
    _row(1628973, "0042500101", "2026-04-20", "NYK", "DET", True, 35, season="2025-26", stype="Playoffs"),
    _row(1999999, "0022500900", "2026-03-02", "MIA", "NYK", True, 11, season="2025-26"),  # not in player_xref
]


def _sqlite(tmp_path, rows=ROWS):
    path = tmp_path / "nba_edge.db"
    db = sqlite3.connect(path)
    db.execute(f"create table game_logs (id integer primary key autoincrement, {', '.join(COLS)})")
    db.executemany(f"insert into game_logs ({', '.join(COLS)}) values ({', '.join('?' * len(COLS))})", rows)
    db.commit()
    db.close()
    return str(path)


def _players(pg):
    seed_players(pg, [(1628973, "Jalen Brunson", "NYK"), (1630540, "Miles McBride", "NYK"),
                      (1628369, "Jayson Tatum", "BOS")])


def _run(pg, path):
    with pg.transaction():
        return backfill.backfill(pg, backfill.read_sqlite(path))


def test_counts_match_and_unmatched_is_held(pg, tmp_path):
    _players(pg)
    before = backfill.counts(pg)
    out = _run(pg, _sqlite(tmp_path))
    assert (out["sqlite_rows"], out["matched_rows"], out["unmatched_rows"], out["unmatched_players"]) == (6, 5, 1, 1)
    assert out["after"]["nba_edge_db"] == 5 and out["after"]["pending"] == 1
    assert out["games_created"] == 3 and before["game_logs"] == 0
    assert out["problems"] == []
    held = pg.execute("select dedupe_key, payload->>'pts' from pending_rows where source = 'nba_edge_db'").fetchall()
    assert held == [("nba:0022500900:1999999", "11")]
    text = backfill.render(before, out, dry_run=False)
    assert "UNMATCHED PLAYERS:" in text and "-> OK" in text


def test_games_get_home_away_and_types(pg, tmp_path):
    _players(pg)
    _run(pg, _sqlite(tmp_path))
    rows = pg.execute("""select nba_game_id, home_team, away_team, season, season_type, status, source
                         from games order by game_date""").fetchall()
    assert rows == [("0022500900", "MIA", "NYK", "2025-26", "Regular Season", "final", "nba_edge_db"),
                    ("0042500101", "NYK", "DET", "2025-26", "Playoffs", "final", "nba_edge_db"),
                    ("0022600150", "NYK", "BOS", "2026-27", "Regular Season", "final", "nba_edge_db")]
    log = pg.execute("select l.min, l.plus_minus, l.did_not_play, l.fetched_at from game_logs l join players p using (player_id) "
                     "join games g using (game_id) where p.full_name = 'Jalen Brunson' and g.nba_game_id = '0022600150'")
    mins, pm, dnp, fetched = log.fetchone()
    assert (float(mins), pm, dnp) == (31.5, 4, False)
    assert fetched.astimezone(timezone.utc).isoformat() == "2026-11-09T08:00:00+00:00"   # SQLite inserted_at (UTC)


def test_attaches_to_espn_game_and_nba_row_wins_over_espn(pg, tmp_path):
    _players(pg)
    pg.execute("""insert into games (game_date, home_team, away_team, status, espn_event_id, source)
                  values ('2026-11-09', 'NYK', 'BOS', 'final', '401909999', 'espn')""")
    gid = pg.execute("select game_id from games").fetchone()[0]
    brunson = pg.execute("select player_id from players where full_name = 'Jalen Brunson'").fetchone()[0]
    pg.execute("""insert into game_logs (game_id, player_id, team_abbr, opp_abbr, is_home, min, pts, source)
                  values (%s, %s, 'NYK', 'BOS', true, 32, 29, 'espn')""", (gid, brunson))
    pg.commit()
    out = _run(pg, _sqlite(tmp_path))
    assert out["games_created"] == 2                                    # the ESPN game was reused
    assert pg.execute("select nba_game_id, espn_event_id from games where game_id = %s", (gid,)).fetchone() == \
        ("0022600150", "401909999")
    assert pg.execute("select pts, source from game_logs where game_id = %s and player_id = %s",
                      (gid, brunson)).fetchone() == (30, "nba_edge_db")
    assert out["after"]["espn"] == 0 and out["after"]["nba_edge_db"] == 5


def test_rerun_is_idempotent(pg, tmp_path):
    _players(pg)
    path = _sqlite(tmp_path)
    first = _run(pg, path)
    second = _run(pg, path)
    assert first["after"] == second["after"] and second["games_created"] == 0


def test_unexpected_extra_row_fails_and_rolls_back(pg, tmp_path):
    _players(pg)
    path = _sqlite(tmp_path)
    _run(pg, path)
    # Someone else wrote an 'nba_edge_db' row that nba_edge.db doesn't have: counts no longer match.
    gid = pg.execute("select game_id from games where nba_game_id = '0022500900'").fetchone()[0]
    mcbride = pg.execute("select player_id from players where full_name = 'Miles McBride'").fetchone()[0]
    pg.execute("""insert into game_logs (game_id, player_id, team_abbr, opp_abbr, is_home, pts, source)
                  values (%s, %s, 'NYK', 'MIA', false, 3, 'nba_edge_db')""", (gid, mcbride))
    pg.commit()
    before = backfill.counts(pg)
    with pytest.raises(backfill.BackfillMismatch, match="expected 5"):
        _run(pg, path)
    assert backfill.counts(pg) == before


def test_dry_run_writes_nothing(pg, tmp_path, monkeypatch, capsys):
    _players(pg)
    from prop_worker import backfill as b
    monkeypatch.setattr(b, "connect", lambda: _NoClose(pg))
    monkeypatch.setattr(b.config, "in_ci", lambda: False)
    assert b.main(["--sqlite", _sqlite(tmp_path), "--dry-run"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert backfill.counts(pg)["game_logs"] == 0


def test_refuses_to_run_in_ci(monkeypatch, tmp_path):
    from prop_worker import backfill as b
    monkeypatch.setattr(b.config, "in_ci", lambda: True)
    with pytest.raises(SystemExit, match="Mac only"):
        b.main(["--sqlite", str(tmp_path / "x.db")])


class _NoClose:
    """Context-manager wrapper so main() can use the test connection without closing it."""
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def test_vacuum_gives_up_on_lock_instead_of_hanging(capsys):
    import psycopg

    class Conn:
        autocommit = False

        def __init__(self):
            self.sql = []

        def commit(self):
            pass

        def execute(self, q, *a):
            self.sql.append(q)
            if q.startswith("vacuum"):
                raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")

    conn = Conn()
    backfill.vacuum(conn)
    assert conn.sql[0] == "set lock_timeout = '30s'" and conn.sql[-1] == "reset lock_timeout"
    assert conn.autocommit is False
    assert "vacuum skipped" in capsys.readouterr().out


def test_vacuum_reports_size(pg, capsys):
    backfill.vacuum(pg)
    assert "MB of 500 MB" in capsys.readouterr().out and pg.autocommit is False
