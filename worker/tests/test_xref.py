"""player_xref: parsers (offline) and seed / link / unmatched report (throwaway Postgres)."""

import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

from conftest import FIXTURES
from prop_worker import xref
from prop_worker.xref import External, Linker

ROSTER = json.loads((FIXTURES / "espn" / "roster_ny_2026-11-10.json").read_text())
KALSHI = json.loads((FIXTURES / "kalshi" / "markets_2026-11-10.json").read_text())
PROPLINE = json.loads((FIXTURES / "propline" / "event_odds_642879.json").read_text())
ESPN_TO_ABBR = {"NY": "NYK", "GS": "GSW", "UTAH": "UTA", "MIL": "MIL"}
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)      # season start 2026-10-01


def L(pg, **kw):
    kw.setdefault("overrides", {})
    return Linker(pg, now=NOW, **kw)


# ── parsers ───────────────────────────────────────────────────────────────────

def test_parse_espn_roster_maps_team_to_canonical():
    items = xref.parse_espn_roster(ROSTER, ESPN_TO_ABBR)
    assert len(items) == 17 and {i.team_abbr for i in items} == {"NYK"}
    assert External("espn", "3934672", "Jalen Brunson", "NYK") in items


def test_parse_espn_roster_unknown_team_gives_none():
    assert {i.team_abbr for i in xref.parse_espn_roster(ROSTER, {})} == {None}


def test_parse_kalshi_props_one_row_per_fragment_and_skips_game_markets():
    items = {i.external_id: i for i in xref.parse_kalshi_props(KALSHI["markets"])}
    assert set(items) == {"NYKLSHAMET44", "MILJJAQUEZ24", "MILMTURNER3", "UTABSENSABAUGH28"}
    assert items["MILJJAQUEZ24"] == External("kalshi", "MILJJAQUEZ24", "Jaime Jaquez Jr.", "MIL")


def test_parse_propline_players_uses_nba_ids():
    items = xref.parse_propline_players([PROPLINE])
    assert items and all(i.external_id.startswith("nba:") for i in items)
    assert len({i.external_id for i in items}) == len(items)


def test_load_overrides(tmp_path):
    p = tmp_path / "o.csv"
    p.write_text("source,external_id,nba_id,note\nespn,999,1628973,traded\n")
    assert xref.load_overrides(p) == {("espn", "999"): "1628973"}
    assert xref.load_overrides(tmp_path / "missing.csv") == {}


# ── seed from a tiny nba_edge.db ──────────────────────────────────────────────

def make_sqlite(path, players, logs):
    db = sqlite3.connect(path)
    db.executescript("""
        create table players (player_id integer primary key, full_name text, is_active integer);
        create table game_logs (id integer primary key autoincrement, player_id integer, game_date text,
                                team_abbr text);""")
    db.executemany("insert into players values (?, ?, ?)", players)
    db.executemany("insert into game_logs (player_id, game_date, team_abbr) values (?, ?, ?)", logs)
    db.commit()
    db.close()
    return str(path)


PLAYERS = [(1628973, "Jalen Brunson", 1), (1626166, "Jordan Clarkson", 1), (1630540, "Miles McBride", 1),
           (1631101, "Jaime Jaquez Jr.", 1), (1641716, "Brice Sensabaugh", 1), (201933, "Retired Guy", 0),
           (202699, "Tobias Harris", 1), (1642000, "Henri Veesaar", 1)]
LOGS = [(1628973, "2026-11-08", "NYK"), (1626166, "2026-04-10", "UTA"), (1626166, "2026-11-07", "NYK"),
        (1630540, "2026-11-07", "NYK"), (1631101, "2026-11-08", "MIL"), (1641716, "2026-11-08", "UTA"),
        (202699, "2026-05-17", "DET")]


@pytest.fixture
def seeded(pg, tmp_path):
    path = make_sqlite(tmp_path / "nba_edge.db", PLAYERS, LOGS)
    out = xref.seed(pg, xref.read_sqlite_players(path))
    return pg, out, path


def _pid(pg, nba_id):
    return pg.execute("select player_id from player_xref where source = 'nba' and external_id = %s",
                      (nba_id,)).fetchone()[0]


def _team(pg, nba_id):
    return pg.execute("select team_abbr, team_as_of from players where player_id = %s",
                      (_pid(pg, nba_id),)).fetchone()


def test_seed_counts_and_latest_team(seeded):
    pg, out, _ = seeded
    assert out == {"source_rows": 8, "inserted": 8, "updated": 0, "players_total": 8}
    assert _team(pg, "1626166") == ("NYK", date(2026, 11, 7))     # latest game log wins over April's
    assert _team(pg, "1642000") == (None, None)                   # no game logs yet


def test_seed_is_idempotent(seeded):
    pg, _, path = seeded
    again = xref.seed(pg, xref.read_sqlite_players(path))
    assert again == {"source_rows": 8, "inserted": 0, "updated": 8, "players_total": 8}
    assert pg.execute("select count(*) from player_xref where source = 'nba'").fetchone()[0] == 8


def test_reseed_does_not_undo_a_newer_espn_team(seeded):
    pg, _, path = seeded
    linker = L(pg)
    linker.resolve(External("espn", "6440", "Tobias Harris", "SAS"))
    linker.flush()
    xref.seed(pg, xref.read_sqlite_players(path))                 # nba_edge.db still says DET (May)
    assert _team(pg, "202699") == ("SAS", date(2026, 11, 10))


def test_seed_rejects_unknown_team(pg, tmp_path):
    path = make_sqlite(tmp_path / "bad.db", [(1, "X Y", 1)], [(1, "2026-11-01", "SEA")])
    with pytest.raises(SystemExit, match="SEA"):
        xref.seed(pg, xref.read_sqlite_players(path))


# ── linking + unmatched report ───────────────────────────────────────────────

def test_espn_roster_links_by_team_and_name_and_reports_the_rest(seeded):
    pg, _, _ = seeded
    linker = L(pg)
    for ext in xref.parse_espn_roster(ROSTER, ESPN_TO_ABBR):
        linker.resolve(ext)
    linker.flush()
    linked = dict(pg.execute("select external_name, match_method from player_xref where source = 'espn'"))
    assert linked == {"Jalen Brunson": "team_name", "Jordan Clarkson": "team_name", "Miles McBride": "team_name"}
    unmatched = pg.execute("select count(*) from xref_unmatched where source = 'espn' and resolved_at is null")
    assert unmatched.fetchone()[0] == 14           # rest of the roster isn't in this tiny seed: none dropped


def test_offseason_move_and_rookie_link_via_stale_team_fallback(seeded):
    pg, _, _ = seeded
    linker = L(pg)
    assert linker.resolve(External("espn", "6440", "Tobias Harris", "SAS")) == _pid(pg, "202699")
    assert linker.resolve(External("espn", "5105571", "Henri Veesaar", "ATL")) == _pid(pg, "1642000")
    linker.flush()
    assert set(pg.execute("select match_method from player_xref where source = 'espn'").fetchall()) == \
        {("name_unique_stale_team",)}
    assert _team(pg, "202699") == ("SAS", date(2026, 11, 10))
    assert _team(pg, "1642000") == ("ATL", date(2026, 11, 10))


def test_unmatched_player_is_reported_with_candidates_then_fixed_by_override(seeded):
    pg, _, _ = seeded
    # ESPN puts Brunson on BOS, but he has this season's games for NYK: never auto-linked.
    traded = External("espn", "3934672", "Jalen Brunson", "BOS")
    linker = L(pg)
    assert linker.resolve(traded) is None
    linker.flush()
    rows = xref.unmatched_rows(pg)
    assert len(rows) == 1
    source, ext_id, name, team, reason, cands, seen, *_ = rows[0]
    assert (source, ext_id, team, reason) == ("espn", "3934672", "BOS", "other_team_only")
    assert cands[0] == {"player_id": _pid(pg, "1628973"), "name": "Jalen Brunson", "team": "NYK",
                        "team_as_of": "2026-11-08"}
    report = xref.render_report(rows, xref.coverage(pg), xref.methods(pg))
    assert "UNMATCHED (unresolved): 1" in report and "xref_overrides.csv" in report
    assert "NYK as of 2026-11-08" in report

    # Seen again: counted, still unresolved.
    linker = L(pg)
    linker.resolve(traded)
    linker.flush()
    assert pg.execute("select seen_count from xref_unmatched").fetchone()[0] == 2

    # Override: linked, unmatched row resolved, and the player's team moves to BOS.
    linker = L(pg, overrides={("espn", "3934672"): "1628973"})
    assert linker.resolve(traded) == _pid(pg, "1628973")
    linker.flush()
    assert xref.unmatched_rows(pg) == []
    assert pg.execute("select resolved_at is not null from xref_unmatched").fetchone()[0]
    assert pg.execute("select match_method from player_xref where source = 'espn'").fetchone()[0] == "override"
    assert _team(pg, "1628973")[0] == "BOS"


def test_override_to_unknown_nba_id_is_reported(seeded):
    pg, _, _ = seeded
    linker = L(pg, overrides={("espn", "1"): "424242"})
    assert linker.resolve(External("espn", "1", "Nobody", "NYK")) is None
    linker.flush()
    assert xref.unmatched_rows(pg)[0][4] == "override_nba_id_not_in_players"


def test_propline_direct_id_join(seeded):
    pg, _, _ = seeded
    linker = L(pg)
    assert linker.resolve(External("propline", "nba:1628973", "Jalen Brunson", None)) == _pid(pg, "1628973")
    assert linker.resolve(External("propline", "nba:9999999", "Brand New", None)) is None
    assert linker.resolve(External("propline", "pl:123", "Weird Id", None)) is None
    linker.flush()
    assert pg.execute("select match_method from player_xref where source = 'propline'").fetchone()[0] == "direct_id"
    reasons = {r[1]: r[4] for r in xref.unmatched_rows(pg)}
    assert reasons == {"nba:9999999": "nba_id_not_in_players", "pl:123": "nba_id_not_in_players"}


def test_kalshi_fragments_link_by_team_and_name(seeded):
    pg, _, _ = seeded
    linker = L(pg)
    for ext in xref.parse_kalshi_props(KALSHI["markets"]):
        linker.resolve(ext)
    linker.flush()
    linked = dict(pg.execute("select external_id, player_id from player_xref where source = 'kalshi'"))
    assert linked == {"MILJJAQUEZ24": _pid(pg, "1631101"), "UTABSENSABAUGH28": _pid(pg, "1641716")}
    assert {r[1] for r in xref.unmatched_rows(pg)} == {"NYKLSHAMET44", "MILMTURNER3"}


def test_espn_trade_update_lets_new_kalshi_fragment_match(seeded):
    pg, _, _ = seeded
    linker = L(pg)
    linker.resolve(External("espn", "100", "Brice Sensabaugh", "UTA"))   # linked on current team
    linker.flush()
    linker = L(pg)
    linker.resolve(External("espn", "100", "Brice Sensabaugh", "MIA"))   # known id: team refreshes
    assert linker.resolve(External("kalshi", "MIABSENSABAUGH28", "Brice Sensabaugh", "MIA")) == _pid(pg, "1641716")
    linker.flush()
    frags = pg.execute("select kalshi_fragments from player_xref_wide where player_id = %s",
                       (_pid(pg, "1641716"),)).fetchone()[0]
    assert frags == ["MIABSENSABAUGH28"]
    assert _team(pg, "1641716")[0] == "MIA"


def test_second_espn_id_for_same_player_is_reported_not_overwritten(seeded):
    pg, _, _ = seeded
    linker = L(pg)
    linker.resolve(External("espn", "1", "Jalen Brunson", "NYK"))
    assert linker.resolve(External("espn", "2", "Jalen Brunson", "NYK")) is None
    linker.flush()
    (row,) = xref.unmatched_rows(pg)
    assert row[1] == "2" and row[4] == "player_already_linked" and row[5][0]["espn_id"] == "1"


# ── link() end to end with a fake network ────────────────────────────────────

class Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeNet:
    def __init__(self):
        self.urls = []

    def get(self, url, params=None, headers=None, **kw):
        self.urls.append(url)
        if url.endswith("/teams/18/roster"):
            return Resp(ROSTER)
        if "/roster" in url:
            return Resp({"team": {"abbreviation": "XXX"}, "athletes": []})
        if url.endswith("/markets"):
            return Resp(KALSHI if params["series_ticker"] == "KXNBAPTS" else {"markets": [], "cursor": ""})
        if url.endswith("/odds"):
            assert headers["X-API-Key"] == "k"
            return Resp([PROPLINE])
        raise AssertionError(url)


def test_link_end_to_end(seeded):
    pg, _, _ = seeded
    net = FakeNet()
    out = xref.link(pg, session=net, propline_key="k", now=NOW)
    assert out["fetched"]["espn"] == 17 and out["fetched"]["kalshi"] == 4 and out["fetched"]["propline"] > 0
    assert sum(u.endswith("/roster") for u in net.urls) == 30
    wide = pg.execute("select espn_id from player_xref_wide where player_id = %s",
                      (_pid(pg, "1628973"),)).fetchone()
    assert wide[0] == "3934672"
    # Re-running finds everything already linked; unmatched rows are not duplicated.
    before = pg.execute("select count(*) from xref_unmatched").fetchone()[0]
    xref.link(pg, session=FakeNet(), propline_key="k", now=NOW)
    assert pg.execute("select count(*) from xref_unmatched").fetchone()[0] == before


def test_link_without_propline_key_skips_it(seeded):
    pg, _, _ = seeded
    out = xref.link(pg, session=FakeNet(), propline_key="", skip={"espn", "kalshi"}, now=NOW)
    assert out["fetched"] == {"propline": -1}


def test_manual_players_fill_gaps_and_nba_edge_db_wins(pg, tmp_path):
    csvp = tmp_path / "manual.csv"
    csvp.write_text("nba_id,full_name,team_abbr,note\n1643563,Richie Saunders,MEM,draft\n"
                    "1628973,Jalen Brunson,BOS,stale manual row\n")
    sqlite_rows = xref.read_sqlite_players(make_sqlite(tmp_path / "e.db", PLAYERS, LOGS))
    manual = xref.read_manual_players(csvp, exclude={p[0] for p in sqlite_rows})
    assert manual == [("1643563", "Richie Saunders", "MEM", None, True)]
    xref.seed(pg, sqlite_rows + manual)
    assert _team(pg, "1628973")[0] == "NYK"
    linker = L(pg)
    assert linker.resolve(External("espn", "5105462", "Richie Saunders", "MEM")) == _pid(pg, "1643563")
    linker.flush()
    assert xref.read_manual_players(tmp_path / "missing.csv") == []
