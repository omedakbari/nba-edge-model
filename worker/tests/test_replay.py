"""replay: no lookahead. Only quotes available at pick time; never closing prices or later snapshots."""

from datetime import timedelta

import pytest

from jobkit import seed_players
from prop_worker import replay
from test_slate import D, TIP, game, logs, pid

PICK = TIP - timedelta(minutes=replay.PICK_LEAD_MIN)


def _market(pg, gid, player, line, ticker):
    pg.execute("""insert into kalshi_markets (ticker, event_ticker, series_ticker, market_kind, stat, line, player_id,
                                              game_id, status)
                  values (%s, 'E', 'KXNBAPTS', 'player_prop', 'pts', %s, %s, %s, 'active')""",
               (ticker, line, player, gid))


def _quote(pg, ticker, yes, no, at):
    pg.execute("insert into kalshi_quotes (ticker, yes_ask, no_ask, fetched_at) values (%s, %s, %s, %s)",
               (ticker, yes, no, at))


def _history(pg, gid, player, line, open_yes, open_no, close_yes, close_no, opened_at):
    pg.execute("""insert into quote_history (line_key, sport, venue, source, book, event_key, player_key, market, line,
                                             game_id, player_id, stat, price_unit, open_over, open_under, close_over,
                                             close_under, opened_at, closed_at, rows_compacted, compacted_at)
                  values (gen_random_uuid(), 'nba', 'kalshi', 'kalshi', 'kalshi', 'E', %s, 'KXNBAPTS', %s, %s, %s,
                          'pts', 'dollars', %s, %s, %s, %s, %s, %s, 2, %s)""",
               (f"nba:{player}", line, gid, player, open_yes, open_no, close_yes, close_no, opened_at,
                TIP, TIP + timedelta(days=3)))


def _box(pg, gid, player, team, opp, pts):
    pg.execute("""insert into game_logs (game_id, player_id, team_abbr, opp_abbr, is_home, min, pts, reb, ast, fg3m,
                                         source)
                  values (%s, %s, %s, %s, true, 30, %s, 5, 4, 2, 'espn')""", (gid, player, team, opp, pts))


@pytest.fixture
def slate(pg):
    seed_players(pg, [(1, "A One", "NYK"), (2, "B Two", "BOS"), (3, "C Three", "MIA"), (4, "D Four", "MIA")])
    a, b, c, d4 = (pid(pg, n) for n in ("A One", "B Two", "C Three", "D Four"))
    g1 = game(pg, D, "NYK", "BOS", TIP, status="final")
    g2 = game(pg, D, "MIA", "ORL", TIP + timedelta(hours=1), status="final")
    for p, team in ((a, "NYK"), (b, "BOS"), (c, "MIA"), (d4, "MIA")):
        logs(pg, p, team, [(2 * i + 2, "ATL", 30) for i in range(10)])      # 10/10 over 24.5 before D
    _box(pg, g1, a, "NYK", "BOS", 20)                                       # A: under on the night
    _box(pg, g1, b, "BOS", "NYK", 31)                                       # B: over
    _box(pg, g2, c, "MIA", "ORL", 28)
    _box(pg, g2, d4, "MIA", "ORL", 10)
    # A: intraday. 23:00 quote (in window) is the one; the post-pick 23:45 quote must be ignored.
    _market(pg, g1, a, 24.5, "TA")
    _quote(pg, "TA", 0.60, 0.42, PICK - timedelta(minutes=30))
    _quote(pg, "TA", 0.95, 0.06, PICK + timedelta(minutes=15))
    # B: only a quote older than the stale window -> no row
    _market(pg, g1, b, 24.5, "TB")
    _quote(pg, "TB", 0.60, 0.42, PICK - timedelta(minutes=replay.STALE_MIN + 5))
    # C: compacted; opening seen before pick -> opening prices, never closing
    _history(pg, g2, c, 24.5, 0.55, 0.47, 0.99, 0.02, PICK - timedelta(hours=8))
    # D: compacted, but first seen AFTER pick -> unusable
    _history(pg, g2, d4, 24.5, 0.55, 0.47, 0.01, 0.99, PICK + timedelta(minutes=5))
    pg.commit()
    return {"a": a, "b": b, "c": c, "d": d4}


def test_only_pick_time_quotes_and_opening_prices(slate, pg):
    rows = replay.build(pg, D, D)
    by = {r["player_id"]: r for r in rows}
    assert set(by) == {slate["a"], slate["c"]}
    a, c = by[slate["a"]], by[slate["c"]]
    assert (a["quote_kind"], a["yes_ask"], a["no_ask"], a["quote_age_min"]) == ("intraday", 0.60, 0.42, 30.0)
    assert (c["quote_kind"], c["yes_ask"], c["no_ask"]) == ("opening", 0.55, 0.47)       # not 0.99 / 0.02 close
    assert a["mid"] == pytest.approx(0.59) and a["l10_hits"] == 10 and a["l10_n"] == 10
    assert a["pk"] == pytest.approx(0.35 * 10.5 / 11 + 0.65 * 0.59, abs=1e-5)
    assert a["side"] == "YES" and a["outcome"] == 0 and a["actual"] == 20              # went under
    assert c["outcome"] == 1


def test_history_excludes_the_game_itself(slate, pg):
    # a 0-point night must not leak into its own L10: put it in and check l10 is still 10/10
    rows = replay.build(pg, D, D)
    assert all(r["l10_n"] == 10 and r["l10_hits"] == 10 for r in rows)


def test_unfinished_slate_is_skipped(slate, pg):
    pg.execute("update games set status = 'scheduled' where game_date = %s and home_team = 'MIA'", (D,))
    pg.commit()
    assert replay.build(pg, D, D) == []


def test_cli_writes_csv(slate, pg, tmp_path):
    out = tmp_path / "r.csv"
    assert replay.main(["--since", D.isoformat(), "--until", D.isoformat(), "--out", str(out)], conn=pg) == 0
    lines = out.read_text().splitlines()
    assert lines[0].split(",") == replay.FIELDS and len(lines) == 3
    pg.rollback()
    pg.read_only = False
