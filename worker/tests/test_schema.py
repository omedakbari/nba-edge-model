"""Schema tests against a throwaway Postgres (see conftest.pg)."""

from datetime import datetime, timedelta, timezone

import pytest

from prop_worker import migrate

T0 = datetime(2026, 11, 10, 23, 0, tzinfo=timezone.utc)

REQUIRED = ["players", "player_xref", "teams", "games", "game_logs", "injuries", "kalshi_markets",
            "kalshi_quotes", "book_quotes", "projections", "picks", "results", "job_runs"]


def test_every_requested_table_exists(pg):
    have = {r[0] for r in pg.execute(
        "select table_name from information_schema.tables where table_schema = 'public'")}
    assert set(REQUIRED) <= have


def test_every_table_has_sport_defaulting_to_nba(pg):
    rows = pg.execute("""
        select t.table_name, c.column_default
        from information_schema.tables t
        left join information_schema.columns c
          on c.table_schema = t.table_schema and c.table_name = t.table_name and c.column_name = 'sport'
        where t.table_schema = 'public' and t.table_type = 'BASE TABLE'
          and t.table_name <> 'schema_migrations'""").fetchall()
    missing = [t for t, d in rows if d is None or "'nba'" not in d]
    assert not missing, f"tables without sport default 'nba': {missing}"


def test_quote_and_injury_tables_carry_provenance(pg):
    for table in ("kalshi_quotes", "book_quotes", "injuries", "kalshi_markets", "games", "game_logs"):
        cols = {r[0] for r in pg.execute(
            "select column_name from information_schema.columns where table_name = %s", (table,))}
        assert {"source", "provider_ts", "fetched_at"} <= cols, table


def test_rls_enabled_everywhere(pg):
    off = [r[0] for r in pg.execute("""
        select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity""")]
    assert off == []


def test_thirty_teams_seeded_with_espn_codes(pg):
    rows = dict(pg.execute("select abbr, espn_abbr from teams").fetchall())
    assert len(rows) == 30
    assert rows["GSW"] == "GS" and rows["UTA"] == "UTAH" and rows["WAS"] == "WSH"
    assert pg.execute("select propline_team_id from teams where abbr = 'NYK'").fetchone()[0] == "nba:1610612752"


def test_migrate_is_idempotent_and_detects_edits(pg, tmp_path):
    assert migrate.apply(pg, migrate.discover()) == []         # already applied by the fixture
    (tmp_path / "001_init.sql").write_text("select 1; -- edited")
    with pytest.raises(SystemExit, match="edited"):
        migrate.apply(pg, migrate.discover(tmp_path))


def _market(pg, ticker="KXNBAPTS-26NOV10MIAUTA-UTABSENSABAUGH28-25"):
    pg.execute("""insert into kalshi_markets (ticker, event_ticker, series_ticker, market_kind)
                  values (%s, 'KXNBAPTS-26NOV10MIAUTA', 'KXNBAPTS', 'player_prop')""", (ticker,))
    return ticker


def _kq(pg, ticker, yes_ask, at, provider_ts=None):
    pg.execute("""insert into kalshi_quotes (ticker, yes_bid, yes_ask, no_bid, no_ask, provider_ts, fetched_at)
                  values (%s, %s, %s, %s, %s, %s, %s)""",
               (ticker, yes_ask - 0.02, yes_ask, 0.98 - yes_ask, 1.0 - yes_ask, provider_ts or at, at))


def test_kalshi_quotes_store_only_price_changes(pg):
    t = _market(pg)
    _kq(pg, t, 0.40, T0)
    _kq(pg, t, 0.40, T0 + timedelta(minutes=15))           # unchanged -> no new row
    _kq(pg, t, 0.40, T0 + timedelta(minutes=30))
    rows = pg.execute("select yes_ask, fetched_at, last_seen_at from kalshi_quotes").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == T0 and rows[0][2] == T0 + timedelta(minutes=30)   # freshness still advances
    _kq(pg, t, 0.45, T0 + timedelta(minutes=45))           # price moved -> new row
    assert pg.execute("select count(*) from kalshi_quotes").fetchone()[0] == 2


def test_kalshi_quote_future_provider_ts_is_stored_not_hidden(pg):
    # The DB keeps what the feed said; the freshness gate (Phase 5) turns future timestamps red.
    t = _market(pg)
    _kq(pg, t, 0.40, T0, provider_ts=T0 + timedelta(hours=6))
    assert pg.execute("select provider_ts from kalshi_quotes").fetchone()[0] == T0 + timedelta(hours=6)


def _bq(pg, price, at, line=24.5, book="draftkings"):
    pg.execute("""insert into book_quotes (source, book, event_id, player_key, market, stat, line, side,
                                           price_american, provider_ts, fetched_at)
                  values ('propline', %s, '642879', 'nba:1628973', 'player_points', 'pts', %s, 'over', %s, %s, %s)""",
               (book, line, price, at, at))


def test_book_quotes_store_only_price_changes_per_line_and_book(pg):
    _bq(pg, -110, T0)
    _bq(pg, -110, T0 + timedelta(minutes=15))
    _bq(pg, -115, T0 + timedelta(minutes=30))               # price change
    _bq(pg, -110, T0 + timedelta(minutes=30), line=25.5)    # different line = different quote
    _bq(pg, -110, T0 + timedelta(minutes=30), book="fanduel")
    assert pg.execute("select count(*) from book_quotes").fetchone()[0] == 4


def _inj(pg, status, at, source="nba_pdf", reason="Left Ankle; Sprain"):
    pg.execute("""insert into injuries (source, player_key, external_name, team_abbr, game_date, status,
                                        reason, provider_ts, fetched_at)
                  values (%s, 'NYK:jalen brunson', 'Brunson, Jalen', 'NYK', '2026-11-10', %s, %s, %s, %s)""",
               (source, status, reason, at, at))


def test_injuries_store_only_status_changes_per_source(pg):
    _inj(pg, "Questionable", T0)
    _inj(pg, "Questionable", T0 + timedelta(minutes=15))
    _inj(pg, "Out", T0 + timedelta(minutes=30))
    _inj(pg, "Questionable", T0 + timedelta(minutes=30), source="espn")   # other source kept separately
    rows = pg.execute("select source, status from injuries order by id").fetchall()
    assert rows == [("nba_pdf", "Questionable"), ("nba_pdf", "Out"), ("espn", "Questionable")]
