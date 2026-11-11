"""prune: after 2 days keep opening, closing (last price at/before tip) and pick-used prices only."""

from datetime import datetime, timedelta, timezone

from jobkit import Net, run as _run
from prop_worker.jobs import prune

TIP = datetime(2026, 11, 5, 0, 30, tzinfo=timezone.utc)          # 7:30 pm ET Nov 4
NOW = datetime(2026, 11, 8, 11, 0, tzinfo=timezone.utc)          # 6am ET Nov 8: > 2 days after tip
RECENT_TIP = datetime(2026, 11, 7, 0, 30, tzinfo=timezone.utc)   # < 2 days before NOW: untouched


def _game(pg, tip, home="NYK", away="BOS"):
    return pg.execute("""insert into games (game_date, home_team, away_team, start_time, status, source)
                         values (%s, %s, %s, %s, 'final', 'espn') returning game_id""",
                      ((tip - timedelta(hours=5)).date(), home, away, tip)).fetchone()[0]


def _book(pg, game_id, prices, start, step=timedelta(minutes=15), line=24.5, book="draftkings", key="nba:1"):
    ids = []
    for i, price in enumerate(prices):
        at = start + i * step
        row = pg.execute("""insert into book_quotes (source, book, event_id, game_id, player_key, market, stat, line,
                                                     side, price_american, provider_ts, fetched_at)
                            values ('propline', %s, 'e1', %s, %s, 'player_points', 'pts', %s, 'over', %s, %s, %s)
                            returning id""", (book, game_id, key, line, price, at, at)).fetchone()
        ids.append(row[0] if row else None)
    return ids


def _kalshi(pg, game_id, asks, start, ticker="KXNBAPTS-26NOV04BOSNYK-NYKJBRUNSON11-25"):
    pg.execute("""insert into kalshi_markets (ticker, event_ticker, series_ticker, market_kind, game_id)
                  values (%s, 'KXNBAPTS-26NOV04BOSNYK', 'KXNBAPTS', 'player_prop', %s)""", (ticker, game_id))
    ids = []
    for i, ask in enumerate(asks):
        at = start + i * timedelta(minutes=15)
        ids.append(pg.execute("""insert into kalshi_quotes (ticker, yes_ask, fetched_at, provider_ts)
                                 values (%s, %s, %s, %s) returning id""", (ticker, ask, at, at)).fetchone()[0])
    return ids


def _remaining(pg, table):
    return [r[0] for r in pg.execute(f"select id from {table} order by id")]


def run(pg, **kw):
    return _run(pg, Net(), "prune", prune.run, now=NOW, **kw)


def test_keeps_opening_and_closing_after_two_days(pg):
    g = _game(pg, TIP)
    ids = _book(pg, g, [-110, -115, -120, -125, -130], start=TIP - timedelta(hours=1))   # last one exactly at tip
    late = _book(pg, g, [-140], start=TIP + timedelta(minutes=5))                         # stored after tip
    pg.commit()
    out = run(pg)
    assert out.status == "green"
    assert _remaining(pg, "book_quotes") == [ids[0], ids[4]]          # opening + closing; post-tip row gone
    assert late[0] not in _remaining(pg, "book_quotes") and out.meta["book_quotes"]["removed"] == 4


def test_pick_referenced_price_survives(pg):
    from jobkit import seed_players
    seed_players(pg, [(1628973, "Jalen Brunson", "NYK")])
    g = _game(pg, TIP)
    ids = _book(pg, g, [-110, -115, -120, -125], start=TIP - timedelta(hours=1))
    pid = pg.execute("select player_id from players").fetchone()[0]
    pg.execute("""insert into picks (game_id, player_id, stat, venue, side, line, book_quote_id)
                  values (%s, %s, 'pts', 'draftkings', 'over', 24.5, %s)""", (g, pid, ids[1]))
    pg.commit()
    run(pg)
    assert _remaining(pg, "book_quotes") == [ids[0], ids[1], ids[3]]


def test_recent_games_keep_full_history(pg):
    g = _game(pg, RECENT_TIP, home="MIA", away="UTA")
    ids = _book(pg, g, [-110, -115, -120, -125], start=RECENT_TIP - timedelta(hours=1))
    pg.commit()
    out = run(pg)
    assert _remaining(pg, "book_quotes") == ids and out.rows_out == 0


def test_each_line_and_book_is_thinned_separately(pg):
    g = _game(pg, TIP)
    a = _book(pg, g, [-110, -115, -120], start=TIP - timedelta(hours=1), line=24.5)
    b = _book(pg, g, [-110, -112, -114], start=TIP - timedelta(hours=1), line=25.5)
    c = _book(pg, g, [100, 105, 110], start=TIP - timedelta(hours=1), book="fanduel")
    pg.commit()
    run(pg)
    assert _remaining(pg, "book_quotes") == sorted([a[0], a[2], b[0], b[2], c[0], c[2]])


def test_kalshi_keeps_opening_and_last_pretip_price(pg):
    g = _game(pg, TIP)
    ids = _kalshi(pg, g, [0.40, 0.42, 0.44, 0.46, 0.60, 0.90], start=TIP - timedelta(minutes=45))  # 4 pre, 2 in-game
    pg.commit()
    out = run(pg)
    assert _remaining(pg, "kalshi_quotes") == [ids[0], ids[3]]             # opening + closing (in-game dropped)
    assert out.meta["kalshi_quotes"]["removed"] == 4


def test_quotes_without_a_game_use_their_own_last_seen(pg):
    ids = _book(pg, None, [-110, -115, -120], start=TIP - timedelta(hours=1))
    pg.commit()
    run(pg)
    assert _remaining(pg, "book_quotes") == [ids[0], ids[2]]


def test_dry_run_counts_without_deleting(pg):
    g = _game(pg, TIP)
    ids = _book(pg, g, [-110, -115, -120, -125], start=TIP - timedelta(hours=1))
    pg.commit()
    out = run(pg, dry_run=True)
    assert out.meta["book_quotes"]["removed"] == 2 and _remaining(pg, "book_quotes") == ids


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
