"""prune: after 2 days each quote line folds into one quote_history row; pick-used rows stay in full."""

from datetime import datetime, timedelta, timezone

import pytest

from jobkit import Net, run as _run, seed_players
from prop_worker.jobs import prune

TIP = datetime(2026, 11, 5, 0, 30, tzinfo=timezone.utc)          # 7:30 pm ET Nov 4
NOW = datetime(2026, 11, 8, 11, 0, tzinfo=timezone.utc)          # 6am ET Nov 8: > 2 days after tip
RECENT_TIP = datetime(2026, 11, 7, 0, 30, tzinfo=timezone.utc)   # < 2 days before NOW: untouched


def _game(pg, tip, home="NYK", away="BOS"):
    return pg.execute("""insert into games (game_date, home_team, away_team, start_time, status, source)
                         values (%s, %s, %s, %s, 'final', 'espn') returning game_id""",
                      ((tip - timedelta(hours=5)).date(), home, away, tip)).fetchone()[0]


def _book(pg, game_id, prices, start, side="over", line=24.5, book="draftkings", key="nba:1",
          step=timedelta(minutes=15)):
    ids = []
    for i, price in enumerate(prices):
        at = start + i * step
        ids.append(pg.execute(
            """insert into book_quotes (source, book, event_id, game_id, player_key, market, stat, line, side,
                                        price_american, provider_ts, fetched_at)
               values ('propline', %s, 'e1', %s, %s, 'player_points', 'pts', %s, %s, %s, %s, %s) returning id""",
            (book, game_id, key, line, side, price, at, at)).fetchone()[0])
    return ids


def _kalshi(pg, game_id, asks, start, ticker="KXNBAPTS-26NOV04BOSNYK-NYKJBRUNSON11-25"):
    pg.execute("""insert into kalshi_markets (ticker, event_ticker, series_ticker, market_kind, stat, line,
                                              kalshi_fragment, game_id)
                  values (%s, 'KXNBAPTS-26NOV04BOSNYK', 'KXNBAPTS', 'player_prop', 'pts', 24.5, 'NYKJBRUNSON11', %s)""",
               (ticker, game_id))
    ids = []
    for i, ask in enumerate(asks):
        at = start + i * timedelta(minutes=15)
        ids.append(pg.execute("""insert into kalshi_quotes (ticker, yes_ask, no_ask, fetched_at, provider_ts)
                                 values (%s, %s, %s, %s, %s) returning id""",
                              (ticker, ask, round(1 - ask + 0.02, 2), at, at)).fetchone()[0])
    return ids


def _ids(pg, table):
    return [r[0] for r in pg.execute(f"select id from {table} order by id")]


def _history(pg, venue="book"):
    return pg.execute("""select book, line, open_over, open_under, close_over, close_under, rows_compacted
                         from quote_history where venue = %s order by book, line""", (venue,)).fetchall()


def run(pg, **kw):
    return _run(pg, Net(), "prune", prune.run, now=NOW, **kw)


def test_line_folds_into_one_row_with_opening_and_closing_for_both_sides(pg):
    g = _game(pg, TIP)
    start = TIP - timedelta(hours=1)
    _book(pg, g, [-110, -115, -120, -125, -130], start)                    # over; last one exactly at tip
    _book(pg, g, [-110, -105, -100, 100, 105], start, side="under")
    _book(pg, g, [-160], TIP + timedelta(minutes=5))                       # after tip: not the closing price
    pg.commit()
    out = run(pg)
    assert out.status == "green"
    assert _history(pg) == [("draftkings", 24.5, -110, -110, -130, 105, 11)]
    assert _ids(pg, "book_quotes") == [] and out.meta["book_quotes"]["deleted"] == 11
    opened, closed = pg.execute("select opened_at, closed_at from quote_history").fetchone()
    assert (opened, closed) == (start, TIP)


def test_pick_used_price_stays_as_a_full_row(pg):
    seed_players(pg, [(1628973, "Jalen Brunson", "NYK")])
    g = _game(pg, TIP)
    ids = _book(pg, g, [-110, -115, -120, -125], TIP - timedelta(hours=1))
    pg.execute("""insert into picks (game_id, player_id, stat, venue, side, line, book_quote_id)
                  values (%s, (select player_id from players), 'pts', 'draftkings', 'over', 24.5, %s)""", (g, ids[1]))
    pg.commit()
    run(pg)
    assert _ids(pg, "book_quotes") == [ids[1]]
    assert _history(pg) == [("draftkings", 24.5, -110, None, -125, None, 4)]


def test_recent_games_keep_full_history(pg):
    g = _game(pg, RECENT_TIP, home="MIA", away="UTA")
    ids = _book(pg, g, [-110, -115, -120, -125], RECENT_TIP - timedelta(hours=1))
    pg.commit()
    out = run(pg)
    assert _ids(pg, "book_quotes") == ids and out.rows_out == 0 and _history(pg) == []


def test_each_line_and_book_gets_its_own_row(pg):
    g = _game(pg, TIP)
    start = TIP - timedelta(hours=1)
    _book(pg, g, [-110, -115, -120], start, line=24.5)
    _book(pg, g, [-110, -112, -114], start, line=25.5)
    _book(pg, g, [100, 105, 110], start, book="fanduel")
    pg.commit()
    run(pg)
    assert [(b, float(l), c) for b, l, _, _, c, _, _ in _history(pg)] == \
        [("draftkings", 24.5, -120), ("draftkings", 25.5, -114), ("fanduel", 24.5, 110)]


def test_kalshi_uses_last_pretip_ask_and_removes_settled_market_metadata(pg):
    g = _game(pg, TIP)
    _kalshi(pg, g, [0.40, 0.42, 0.44, 0.46, 0.60, 0.90], TIP - timedelta(minutes=45))  # 4 pre-tip, 2 in-game
    pg.commit()
    out = run(pg)
    row = pg.execute("""select event_key, player_key, market, line, price_unit, open_over, open_under,
                               close_over, close_under, rows_compacted from quote_history where venue = 'kalshi'""").fetchone()
    assert row[:5] == ("KXNBAPTS-26NOV04BOSNYK-NYKJBRUNSON11-25", "NYKJBRUNSON11", "KXNBAPTS", 24.5, "dollars")
    assert [float(x) for x in row[5:9]] == [0.40, 0.62, 0.46, 0.56] and row[9] == 6
    assert _ids(pg, "kalshi_quotes") == []
    assert pg.execute("select count(*) from kalshi_markets").fetchone()[0] == 0
    assert out.meta["kalshi_markets_deleted"] == 1


def test_kalshi_market_with_a_pick_keeps_its_quote_and_metadata(pg):
    seed_players(pg, [(1628973, "Jalen Brunson", "NYK")])
    g = _game(pg, TIP)
    ids = _kalshi(pg, g, [0.40, 0.42, 0.44], TIP - timedelta(minutes=45))
    pg.execute("""insert into picks (game_id, player_id, stat, venue, side, line, kalshi_quote_id)
                  values (%s, (select player_id from players), 'pts', 'kalshi', 'over', 24.5, %s)""", (g, ids[2]))
    pg.commit()
    run(pg)
    assert _ids(pg, "kalshi_quotes") == [ids[2]]
    assert pg.execute("select count(*) from kalshi_markets").fetchone()[0] == 1


def test_quotes_without_a_game_use_their_own_last_seen(pg):
    _book(pg, None, [-110, -115, -120], TIP - timedelta(hours=1))
    pg.commit()
    run(pg)
    assert _history(pg) == [("draftkings", 24.5, -110, None, -120, None, 3)]


def test_rerun_is_a_no_op(pg):
    g = _game(pg, TIP)
    _book(pg, g, [-110, -115, -120], TIP - timedelta(hours=1))
    pg.commit()
    run(pg)
    again = run(pg)
    assert again.meta["book_quotes"]["rows_folded"] == 0 and len(_history(pg)) == 1


def test_dry_run_changes_nothing_and_reports_bytes_per_game(pg):
    g = _game(pg, TIP)
    start = TIP - timedelta(hours=1)
    for line in (22.5, 23.5, 24.5, 25.5):
        for side in ("over", "under"):
            _book(pg, g, [-110 - k for k in range(8)], start, side=side, line=line)
    _kalshi(pg, g, [0.40 + k / 100 for k in range(8)], start)
    pg.commit()
    before_ids = _ids(pg, "book_quotes")
    out = run(pg, dry_run=True)
    assert _ids(pg, "book_quotes") == before_ids and _history(pg) == []
    m = out.meta
    assert m["games"] == 1 and m["bytes_before"] > m["bytes_after"] > 0
    assert m["book_quotes"]["rows_folded"] == 64 and m["book_quotes"]["lines"] == 4
    text = prune.render(m, dry=True)
    assert "bytes per game: before" in text and "smaller" in text and "would delete 64" in text


def test_as_of_previews_unsettled_games_but_only_in_dry_runs(pg):
    g = _game(pg, RECENT_TIP)
    _book(pg, g, [-110, -115], RECENT_TIP - timedelta(hours=1))
    pg.commit()
    later = RECENT_TIP + timedelta(days=3)
    out = run(pg, dry_run=True, as_of=later)
    assert out.meta["book_quotes"]["rows_folded"] == 2 and len(_ids(pg, "book_quotes")) == 2
    err = run(pg, as_of=later)
    assert err.status == "error" and "--as-of is only allowed with --dry-run" in err.error


def test_database_size_alarm(pg, monkeypatch):
    monkeypatch.setattr(prune, "db_mb", lambda conn: 420.0)
    out = run(pg)
    assert out.status == "red" and "420 MB of 500 MB" in out.red_reasons[0]
    monkeypatch.setattr(prune, "db_mb", lambda conn: 320.0)
    out = run(pg)
    assert out.status == "green" and out.warnings == ["database at 320 MB of 500 MB"]


def test_zero_to_prune_on_a_game_day_is_not_red(pg):
    _game(pg, NOW + timedelta(hours=8))
    pg.commit()
    out = run(pg)
    assert out.status == "green" and out.rows_out == 0


@pytest.mark.parametrize("value,iso", [("2026-11-14T12:00", "2026-11-14T17:00:00+00:00"),
                                       ("2026-11-14T12:00+00:00", "2026-11-14T12:00:00+00:00")])
def test_as_of_argument_is_et_unless_offset_given(value, iso):
    assert prune.parse_ts_arg(value).isoformat() == iso


def test_line_key_ignores_numeric_scale_and_differs_by_line(pg):
    k = lambda line: pg.execute("select quote_history_line_key('nba','book','propline','dk','e1','nba:1','player_points',%s)",
                                (line,)).fetchone()[0]
    from decimal import Decimal
    assert k(Decimal("24.5")) == k(Decimal("24.50")) != k(Decimal("25.5"))
