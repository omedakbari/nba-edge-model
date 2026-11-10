from datetime import datetime, timedelta

import pytest

from jobkit import Net, Resp, load, run as _run, scoreboards, seed_players
from prop_worker import config
from prop_worker.jobs import propline, schedule

NOW = datetime(2026, 11, 10, 18, 30, tzinfo=config.ET)       # after capture (6:24 pm ET), before the 7:30 tip


def run(pg, net, name, fn, now=NOW, **kw):
    return _run(pg, net, name, fn, now=now, **kw)

ODDS = load("propline/odds_bulk_2026-11-10.json")
QUOTA = {"x-daily-remaining": "975", "x-daily-limit": "1000"}


def _net(events=None, headers=QUOTA):
    return Net([scoreboards(), (lambda u, p: u.endswith("/odds"), Resp(ODDS if events is None else events,
                                                                      headers=headers))])


def _ids():
    return sorted({(o["player_id"], o["description"]) for e in ODDS for b in e["bookmakers"]
                   for m in b["markets"] for o in m["outcomes"] if o.get("player_id")})


def _setup(pg, n=None):
    ids = _ids()[:n] if n else _ids()
    seed_players(pg, [(int(pid.split(":")[1]), name, None) for pid, name in ids])
    run(pg, _net(), "schedule", schedule.run)


def test_parse_keeps_main_lines_of_our_books_for_pregame_events():
    quotes, counts = propline.parse_odds(load("propline/odds_bulk_2026-11-10.json"), NOW)
    assert quotes and {q["line_type"] for q in quotes} == {"main"}
    assert {q["book"] for q in quotes} <= set(config.BOOK_QUOTE_BOOKS)
    assert {q["side"] for q in quotes} == {"over", "under"}
    assert counts["other_line_types"] > 0 and counts["events"] == 2 and counts["events_started"] == 0
    assert all(q["player_key"].startswith("nba:") for q in quotes)


def test_started_events_are_dropped_from_storage():
    after_tip = datetime(2026, 11, 10, 20, 15, tzinfo=config.ET)       # MIL@NYK started 7:30, BOS@PHI at 8
    quotes, counts = propline.parse_odds(load("propline/odds_bulk_2026-11-10.json"), after_tip)
    assert counts["events_started"] == 2 and quotes == []


def test_future_timestamp_and_missing_player_id_are_counted_not_dropped():
    odds = load("propline/odds_bulk_2026-11-10.json")
    o = next(m for b in odds[0]["bookmakers"] if b["key"] in config.BOOK_QUOTE_BOOKS
             for m in b["markets"] if m["line_type"] == "main" and m["key"] in config.PROPLINE_MARKETS)["outcomes"][0]
    o["last_change_at"], o["book_updated_at"] = "2026-11-12T00:00:00Z", None
    o["player_id"] = None
    quotes, counts = propline.parse_odds(odds, NOW)
    assert counts["future_ts"] == 1 and counts["no_player_id"] == 1
    q = next(q for q in quotes if q["player_key"].startswith("name:"))
    assert q["provider_ts"].isoformat().startswith("2026-11-12")


def test_job_links_games_and_players_and_is_change_only(pg):
    _setup(pg)
    out = run(pg, _net(), "propline", propline.run, propline_key="k")
    assert out.status == "green", out.red_reasons
    n = pg.execute("select count(*) from book_quotes").fetchone()[0]
    assert n == out.rows_out == out.meta["quotes_new"] > 0
    assert pg.execute("select count(*) from book_quotes where player_id is null").fetchone()[0] == 0
    assert pg.execute("select count(*) from book_quotes where game_id is null").fetchone()[0] == 0
    assert pg.execute("select propline_event_id from games where home_team = 'NYK'").fetchone()[0] == "642879"
    assert out.meta["quota_remaining"] == "975"
    assert pg.execute("select match_method, count(*) from player_xref where source = 'propline' group by 1"
                      ).fetchall() == [("direct_id", len(_ids()))]

    again = run(pg, _net(), "propline", propline.run, propline_key="k", now=NOW + timedelta(minutes=15))
    assert again.meta["quotes_new"] == 0 and pg.execute("select count(*) from book_quotes").fetchone()[0] == n
    assert pg.execute("select count(*) from book_quotes where last_seen_at > fetched_at").fetchone()[0] == n


def test_unknown_player_is_stored_and_reported(pg):
    _setup(pg, n=5)
    out = run(pg, _net(), "propline", propline.run, propline_key="k")
    assert out.status == "green" and out.meta["unresolved_players"] > 0
    assert pg.execute("select count(*) from book_quotes where player_id is null").fetchone()[0] == \
        out.meta["unresolved_players"]
    reasons = {r[0] for r in pg.execute("select reason from xref_unmatched where source = 'propline'")}
    assert reasons == {"nba_id_not_in_players"}


def test_low_quota_warns(pg):
    _setup(pg)
    out = run(pg, _net(headers={"x-daily-remaining": "40"}), "propline", propline.run, propline_key="k")
    assert any("quota low" in w for w in out.warnings)


def test_after_last_tip_empty_response_is_not_red(pg):
    _setup(pg)
    late = datetime(2026, 11, 11, 0, 30, tzinfo=config.ET)                 # 12:30 am, same slate
    out = run(pg, _net(events=[]), "propline", propline.run, propline_key="k", now=late)
    assert out.status == "green" and out.expected is False


def test_empty_response_before_tip_on_a_game_day_is_red(pg):
    _setup(pg)
    out = run(pg, _net(events=[]), "propline", propline.run, propline_key="k")
    assert out.status == "red" and "loaded 0 rows on a game day" in out.red_reasons


def test_missing_key_is_an_error(pg, monkeypatch):
    monkeypatch.setattr(config, "PROPLINE_API_KEY", "")
    _setup(pg)
    out = run(pg, _net(), "propline", propline.run)
    assert out.status == "error" and "PROPLINE_API_KEY" in out.error


@pytest.mark.parametrize("payload", [{"error": "x"}, None])
def test_non_list_response_is_an_error(pg, payload):
    _setup(pg)
    out = run(pg, _net(events=payload if payload is not None else {"events": []}), "propline", propline.run,
              propline_key="k")
    assert out.status == "error"
