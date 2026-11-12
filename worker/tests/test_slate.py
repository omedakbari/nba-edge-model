"""slate: read-only prop board built from a local copy of the schema."""

import json
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from jobkit import seed_players
from prop_worker import slate

D = date(2026, 11, 12)
TIP = datetime(2026, 11, 13, 0, 30, tzinfo=timezone.utc)       # 7:30 pm ET Nov 12
SEEN = datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc)


def pid(pg, name):
    return pg.execute("select player_id from players where full_name = %s", (name,)).fetchone()[0]


def game(pg, d, home, away, tip=None, status="scheduled"):
    return pg.execute("""insert into games (game_date, home_team, away_team, start_time, status, source)
                         values (%s, %s, %s, %s, %s, 'espn') returning game_id""",
                      (d, home, away, tip, status)).fetchone()[0]


def quote(pg, gid, player, book, line, over, under, market="player_points", stat="pts", seen=SEEN, line_type="main"):
    for side, price in (("over", over), ("under", under)):
        if price is None:
            continue
        pg.execute("""insert into book_quotes (source, book, event_id, game_id, player_key, player_id, market, stat,
                                               line_type, line, side, price_american, provider_ts, fetched_at, last_seen_at)
                      values ('propline', %s, 'e1', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                   (book, gid, f"nba:{player}", player, market, stat, line_type, line, side, price, seen, seen, seen))


def kalshi(pg, gid, player, line, yes, no, stat="pts", series="KXNBAPTS"):
    t = f"{series}-26NOV12BOSNYK-X{player}-{int(line + 0.5)}"
    pg.execute("""insert into kalshi_markets (ticker, event_ticker, series_ticker, market_kind, stat, line, player_id,
                                              game_id, status)
                  values (%s, '26NOV12BOSNYK', %s, 'player_prop', %s, %s, %s, %s, 'active')""",
               (t, series, stat, line, player, gid))
    pg.execute("insert into kalshi_quotes (ticker, yes_ask, no_ask, fetched_at) values (%s, %s, %s, %s)",
               (t, yes, no, SEEN))


def logs(pg, player, team, opp_pts):
    """opp_pts: [(days before D, opp, pts)]"""
    for i, (days, opp, pts) in enumerate(opp_pts):
        home = team if i % 2 else opp
        away = opp if i % 2 else team
        d = D - timedelta(days=days)
        row = pg.execute("""select game_id from games where game_date = %s and
                            ((home_team = %s and away_team = %s) or (home_team = %s and away_team = %s))""",
                         (d, home, away, away, home)).fetchone()
        g = row[0] if row else game(pg, d, home, away, status="final")
        pg.execute("""insert into game_logs (game_id, player_id, team_abbr, opp_abbr, is_home, min, pts, reb, ast,
                                             fg3m, source)
                      values (%s, %s, %s, %s, %s, 30, %s, 5, 4, 2, 'espn')""",
                   (g, player, team, opp, home == team, pts))


def injury(pg, player, name, team, status, source="nba_pdf", seen=SEEN, game_date=D):
    pg.execute("""insert into injuries (source, player_key, player_id, external_name, team_abbr, game_date, status,
                                        fetched_at, last_seen_at)
                  values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
               (source, f"{team}:{name.lower()}", player, name, team, game_date if source == "nba_pdf" else None,
                status, seen, seen))


@pytest.fixture
def board(pg):
    seed_players(pg, [(1628973, "Jalen Brunson", "NYK"), (1628369, "Jayson Tatum", "BOS"),
                      (1630540, "Miles McBride", "NYK"), (1627759, "Jaylen Brown", "BOS")])
    jb, jt, mm, br = (pid(pg, n) for n in ("Jalen Brunson", "Jayson Tatum", "Miles McBride", "Jaylen Brown"))
    gid = game(pg, D, "NYK", "BOS", TIP)
    # Brunson: books split 26.5 x2 / 27.5 x2 with Pinnacle on 27.5 -> consensus 27.5 (2 books)
    quote(pg, gid, jb, "draftkings", 26.5, -110, -110)
    quote(pg, gid, jb, "fanduel", 26.5, -115, -105)
    quote(pg, gid, jb, "betmgm", 27.5, -105, -115)
    quote(pg, gid, jb, "pinnacle", 27.5, 100, -120)
    quote(pg, gid, jb, "sleeper", 27.0, -120, -110)
    quote(pg, gid, jb, "draftkings", 29.5, -300, 220, line_type="alternate")     # alt line: ignored
    quote(pg, gid, jb, "draftkings", 25.5, -110, -110, seen=SEEN - timedelta(hours=3))  # older line: ignored
    for line, yes, no in ((19.5, 0.9, 0.12), (24.5, 0.66, 0.36), (27.5, 0.5, 0.52), (29.5, 0.38, 0.64), (34.5, 0.1, 0.92)):
        kalshi(pg, gid, jb, line, yes, no)
    logs(pg, jb, "NYK", [(2 * i + 2, "BOS" if i in (0, 4) else "MIA", p)
                         for i, p in enumerate([30, 25, 31, 28, 22, 35, 29, 18, 33, 27, 40, 20])])
    # Tatum: only Sleeper has a line -> ref = Sleeper
    quote(pg, gid, jt, "sleeper", 24.5, -110, -110)
    logs(pg, jt, "BOS", [(2 * i + 2, "NYK", p) for i, p in enumerate([20, 21, 22, 30, 31])])
    # McBride: listed Questionable on the current official report -> hidden by default
    quote(pg, gid, mm, "draftkings", 9.5, -110, -110)
    logs(pg, mm, "NYK", [(2, "MIA", 12), (4, "MIA", 8)])
    injury(pg, mm, "Miles McBride", "NYK", "Questionable")
    # Brown: was Questionable on an earlier report but isn't on the newest one -> shown, no status
    quote(pg, gid, br, "draftkings", 22.5, -110, -110, market="player_rebounds", stat="reb")
    logs(pg, br, "BOS", [(2, "NYK", 20)])
    injury(pg, br, "Jaylen Brown", "BOS", "Questionable", seen=SEEN - timedelta(hours=2))
    injury(pg, jb, "Jalen Brunson", "NYK", "Probable")                                   # newest report
    pg.commit()
    return {"gid": gid, "jb": jb, "jt": jt, "mm": mm, "br": br}


def _by(props):
    return {(p.name, p.stat): p for p in props}


def test_consensus_sleeper_kalshi_and_hit_rates(board, pg):
    props, info = slate.build(pg, D)
    assert info["games"] == 1
    p = _by(props)[("Jalen Brunson", "pts")]
    assert (p.consensus, p.consensus_n, p.ref) == (27.5, 2, "cons")
    assert p.books["sleeper"]["line"] == 27.0
    assert set(p.books) == {"draftkings", "fanduel", "betmgm", "pinnacle", "sleeper"}
    assert p.books["draftkings"]["line"] == 26.5                    # newest line, not the older 25.5 or the alt
    from prop_worker.hitrates import implied
    mgm = implied(-105) / (implied(-105) + implied(-115))          # the two books at 27.5
    pin = implied(100) / (implied(100) + implied(-120))
    assert p.no_vig_over == pytest.approx((mgm + pin) / 2)
    assert [k["line"] for k in p.kalshi] == [24.5, 27.5, 29.5]       # 3 nearest 27.5
    assert p.kalshi[1]["yes_ask"] == 0.5 and p.kalshi[1]["l10"].n == 10
    r = p.rates                                                     # pts newest first: 30 25 31 28 22 35 29 18 33 27 40 20
    assert (r["l5"].hits, r["l5"].n) == (3, 5)
    assert (r["l10"].hits, r["l10"].n) == (6, 10)
    assert (r["season"].hits, r["season"].n) == (7, 12)
    assert (r["h2h"].hits, r["h2h"].n) == (1, 2)                    # vs BOS: 30 (hit), 22 (miss)
    assert p.injury["code"] == "P" and p.opp == "BOS" and p.team == "NYK"


def test_sleeper_is_the_reference_when_no_consensus_book_has_a_line(board, pg):
    p = _by(slate.build(pg, D)[0])[("Jayson Tatum", "pts")]
    assert (p.consensus, p.ref, p.ref_line) == (None, "slp", 24.5) and p.rates["l5"].hits == 2   # 30, 31
    assert p.opp == "NYK" and p.rates["h2h"].n == 5


def test_injured_hidden_by_default_and_dropped_off_report_counts_as_not_listed(board, pg):
    props, _ = slate.build(pg, D)
    shown, dropped = slate.select(props)
    names = {p.name for p in shown}
    assert "Miles McBride" not in names and dropped["injured"] == 1
    assert "Jaylen Brown" in names and _by(props)[("Jaylen Brown", "reb")].injury is None
    shown, _ = slate.select(props, include_injured=True)
    assert "Miles McBride" in {p.name for p in shown}


def test_espn_status_used_only_when_not_on_the_official_report(board, pg):
    injury(pg, board["jt"], "Jayson Tatum", "BOS", "Day-To-Day", source="espn")
    injury(pg, board["jb"], "Jalen Brunson", "NYK", "Out", source="espn")              # official says Probable
    pg.commit()
    props, _ = slate.build(pg, D)
    by = _by(props)
    assert by[("Jayson Tatum", "pts")].injury["code"] == "GTD" and by[("Jayson Tatum", "pts")].injury["source"] == "espn"
    assert by[("Jalen Brunson", "pts")].injury["code"] == "P"
    shown, dropped = slate.select(props)
    assert "Jayson Tatum" not in {p.name for p in shown} and dropped["injured"] == 2


def test_filters_and_sort(board, pg):
    props, _ = slate.build(pg, D)
    shown, _ = slate.select(props, include_injured=True)
    l10 = [p.rates["l10"].pct for p in shown]
    assert l10 == sorted(l10, reverse=True)
    assert {p.stat for p in slate.select(props, stat="reb")[0]} == {"reb"}
    assert all("BOS" in (p.team, p.opp) for p in slate.select(props, team="bos")[0])
    high = slate.select(props, min_hit=0.65, include_injured=True)[0]
    assert all(p.rates["l10"].pct >= 0.65 for p in high)


def test_under_side_flips_the_rates(board, pg):
    p = _by(slate.build(pg, D, side="under")[0])[("Jalen Brunson", "pts")]
    assert (p.rates["l10"].hits, p.rates["l10"].n) == (4, 10)


def test_connection_is_read_only(board, pg):
    slate.read_only(pg)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        pg.execute("insert into job_runs (job, status) values ('x', 'green')")
    pg.rollback()
    pg.read_only = False


def test_cli_prints_table_and_json_and_changes_nothing(board, pg, capsys):
    before = pg.execute("select (select count(*) from book_quotes), (select count(*) from job_runs)").fetchone()
    assert slate.main(["--date", D.isoformat()], conn=pg) == 0
    out = capsys.readouterr().out
    assert "Jalen Brunson" in out and "Miles McBride" not in out and "1 injured hidden" in out
    assert "27.5: 50/52" in out
    assert slate.main(["--date", D.isoformat(), "--json", "--stat", "pts", "--include-injured"], conn=pg) == 0
    data = json.loads(capsys.readouterr().out)
    assert {p["name"] for p in data["props"]} == {"Jalen Brunson", "Jayson Tatum", "Miles McBride"}
    pg.rollback()
    pg.read_only = False
    after = pg.execute("select (select count(*) from book_quotes), (select count(*) from job_runs)").fetchone()
    assert before == after


def test_ra_explains_that_no_lines_are_collected(board, pg, capsys):
    assert slate.main(["--date", D.isoformat(), "--stat", "ra"], conn=pg) == 0
    assert "no rebounds+assists lines are collected" in capsys.readouterr().err


@pytest.mark.parametrize("v,x", [("60", 0.6), ("0.6", 0.6), ("100", 1.0)])
def test_min_hit_accepts_percent_or_fraction(v, x):
    assert slate.parse_min_hit(v) == x
