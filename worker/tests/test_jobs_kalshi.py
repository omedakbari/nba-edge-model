from datetime import datetime, timedelta, timezone

from jobkit import Net, Resp, load, run as _run, scoreboards, seed_players
from prop_worker import config
from prop_worker import xref
from prop_worker.jobs import kalshi, schedule

PROPS = [m for m in load("kalshi/markets_2026-11-10.json")["markets"] if m["ticker"].startswith("KXNBAPTS-")]
NOW = datetime(2026, 11, 10, 18, 30, tzinfo=config.ET)       # after the fixture was captured (5:20 pm ET)


def run(pg, net, name, fn, now=NOW, **kw):
    return _run(pg, net, name, fn, now=now, **kw)
GAME = load("kalshi/markets_game_2026-11-10.json")["markets"]


def _routes(props=None, game=None, pages=None):
    props = PROPS if props is None else props
    game = GAME if game is None else game

    def markets(url, params):
        s = params["series_ticker"]
        if pages and s in pages:
            return Resp(pages[s][params.get("cursor") or ""])
        if s == "KXNBAPTS":
            return Resp({"markets": [m for m in props if m["ticker"].startswith("KXNBAPTS-")], "cursor": ""})
        return Resp({"markets": [m for m in game if m["ticker"].startswith(s + "-")], "cursor": ""})
    return Net([scoreboards(), (lambda u, p: u.endswith("/markets"), markets)])


def _setup(pg):
    seed_players(pg, [(1, "Jaime Jaquez Jr.", "MIL"), (2, "Myles Turner", "MIL"), (3, "Brice Sensabaugh", "UTA")])
    run(pg, _routes(), "schedule", schedule.run)


def test_parse_prop_market_line_and_player():
    m = next(x for x in PROPS if x["ticker"] == "KXNBAPTS-26NOV10MIAUTA-UTABSENSABAUGH28-25")
    row = kalshi.parse_market(m, "KXNBAPTS")
    assert (row["market_kind"], row["stat"], row["line"], row["team_abbr"]) == ("player_prop", "pts", 24.5, "UTA")
    assert (row["player_name"], row["kalshi_fragment"], row["kalshi_event"]) == \
        ("Brice Sensabaugh", "UTABSENSABAUGH28", "26NOV10MIAUTA")
    assert row["yes_ask"] is not None and row["provider_ts"] is not None


def test_inconsistent_title_leaves_line_null():
    m = dict(next(x for x in PROPS if x["ticker"].endswith("-25")), title="Brice Sensabaugh: 30+ points",
             yes_sub_title="Brice Sensabaugh: 30+")
    assert kalshi.parse_market(m, "KXNBAPTS")["line"] is None


def test_parse_game_spread_total():
    rows = {}
    for m in GAME:
        r = kalshi.parse_market(m, m["ticker"].split("-")[0])
        rows.setdefault(r["market_kind"], r)
    assert rows["game_winner"]["team_abbr"] in ("UTA", "HOU") and rows["game_winner"]["line"] is None
    assert rows["spread"]["team_abbr"] == "POR" and rows["spread"]["line"] == 7.5
    assert rows["total"]["team_abbr"] is None and rows["total"]["line"] == 252.5


def test_job_stores_markets_quotes_change_only_and_unmatched(pg):
    _setup(pg)
    out = run(pg, _routes(), "kalshi", kalshi.run)
    n = len(PROPS) + len(GAME)
    assert out.status == "green" and out.rows_out == n and out.meta["quotes_new"] == n
    assert pg.execute("select count(*) from kalshi_quotes").fetchone()[0] == n
    linked = pg.execute("""select count(*) from kalshi_markets where market_kind = 'player_prop'
                           and player_id is not null""").fetchone()[0]
    assert linked == 11                                      # Jaquez x4, Turner x3, Sensabaugh x4
    assert {r[1] for r in xref.unmatched_rows(pg)} == {"NYKLSHAMET44"}
    assert out.meta["unresolved_players"] == 1
    nyk = pg.execute("select game_id is not null from kalshi_markets where event_ticker like '%MILNYK'").fetchone()
    assert nyk == (True,)

    again = run(pg, _routes(), "kalshi", kalshi.run)          # nothing moved
    assert again.meta["quotes_new"] == 0 and pg.execute("select count(*) from kalshi_quotes").fetchone()[0] == n
    seen = pg.execute("select count(*) from kalshi_quotes where last_seen_at > fetched_at").fetchone()[0]
    assert seen == 0                                          # same 'now' in tests; real runs advance it

    moved = [dict(m) for m in PROPS]
    moved[0]["yes_ask_dollars"] = "0.5500"
    third = run(pg, _routes(props=moved), "kalshi", kalshi.run, now=NOW + timedelta(minutes=15))
    assert third.meta["quotes_new"] == 1
    assert pg.execute("select count(*) from kalshi_quotes").fetchone()[0] == n + 1
    assert pg.execute("select count(*) from kalshi_quotes where last_seen_at > fetched_at").fetchone()[0] == n - 1


def test_future_updated_time_is_stored_and_counted(pg):
    _setup(pg)
    future = [dict(m) for m in PROPS]
    future[0]["updated_time"] = "2026-11-11T23:00:00Z"
    out = run(pg, _routes(props=future), "kalshi", kalshi.run)
    assert out.meta["future_provider_ts"] == 1 and any("future" in w for w in out.warnings)
    ts = pg.execute("select provider_ts from kalshi_quotes where ticker = %s", (future[0]["ticker"],)).fetchone()[0]
    assert ts.astimezone(timezone.utc).isoformat() == "2026-11-11T23:00:00+00:00"


def test_paging_follows_cursor(pg):
    _setup(pg)
    pages = {"KXNBAPTS": {"": {"markets": PROPS[:5], "cursor": "abc"}, "abc": {"markets": PROPS[5:], "cursor": ""}}}
    out = run(pg, _routes(pages=pages), "kalshi", kalshi.run)
    assert out.meta["per_series"]["KXNBAPTS"] == len(PROPS)


def test_missing_markets_array_is_an_error(pg):
    _setup(pg)
    net = Net([scoreboards(), (lambda u, p: u.endswith("/markets"), Resp({"oops": []}))])
    out = run(pg, net, "kalshi", kalshi.run)
    assert out.status == "error" and "no markets array" in out.error


def test_no_open_markets_on_a_game_day_is_red(pg):
    _setup(pg)
    net = Net([scoreboards(), (lambda u, p: u.endswith("/markets"), Resp({"markets": [], "cursor": ""}))])
    out = run(pg, net, "kalshi", kalshi.run)
    assert out.status == "red" and "loaded 0 rows on a game day" in out.red_reasons
