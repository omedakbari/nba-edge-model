"""prune: drop data the model never uses, compact settled quote history, and keep the database under
the 500 MB free-tier cap.

Run:  python -m prop_worker.jobs.prune [--dry-run [--as-of 2026-11-14T12:00]] [--no-alert]

Rule (approved 2026-11-11):
  1. Drop first (any age): book_quotes rows from a book not in config.BOOK_QUOTE_BOOKS, a market
     not in config.PROPLINE_MARKETS, or a line_type other than 'main'. Ingest already skips them;
     this clears rows stored before the list changed.
  2. Full intraday history is kept until a game started more than KEEP_DAYS (2) days ago.
  3. After that each quote line is folded into ONE quote_history row (opening and closing price for
     both sides; see migrations/005_quote_history.sql) and its rows are deleted from book_quotes /
     kalshi_quotes. Kalshi game spread / total ladders keep one strike per game and series: the one
     closest to 50c at tip-off, which is the market's spread / total. Player-prop strikes are all kept.
     Settled Kalshi market metadata (kalshi_markets) is deleted once none of its quotes remain
     (ticker, series, line, player and game live on in quote_history).
  4. Rows a pick references (picks.book_quote_id / kalshi_quote_id) are never deleted, by any step.
  5. job_runs rows older than config.JOB_RUNS_KEEP_DAYS (45) are deleted.
  6. VACUUM, then REINDEX quote_history (random 16-byte keys leave its primary key half empty;
     rebuilding packs it). Both use a 30 s lock timeout and are skipped, not failed, if busy.
opening = first stored price per side; closing = last price stored at or before tip-off. Quotes not
attached to a game use their own last-seen time as "tip-off".

--dry-run does everything inside a transaction that is rolled back and reports bytes per game:
before, after compaction alone, and after compaction plus the drops (heap + indexes, measured on
temporary copies whose indexes are built after loading, as a freshly reindexed table would be),
plus an end-of-season projection.
--as-of (dry run only) pretends it is that time, to preview games that haven't settled yet.
Red if the database passes 400 MB of 500; a warning past 300.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone

from .. import config
from ..names import season_start
from ..runner import Context, JobResult, run_all

KEEP_DAYS = 2
PRUNE_WARN_MB = 300
PRUNE_RED_MB = 400
OTHER_KB_PER_GAME = 5      # games, injuries, injury_flags rows per game (measured ~3-5 KB); game_logs is measured live

LINE = "sport, source, book, event_id, player_key, market, line"

_NOT_PICKED_BOOK = "not exists (select 1 from picks k where k.book_quote_id = q.id)"
_NOT_PICKED_KALSHI = "not exists (select 1 from picks k where k.kalshi_quote_id = q.id)"
# line_type null (never written by ingest) counts as main: unknown is kept, not dropped.
_UNUSED_BOOK = "(q.book <> all(%(books)s) or q.market <> all(%(markets)s) or coalesce(q.line_type, 'main') <> 'main')"

_STAGE_BOOK = f"""
create temp table prune_book on commit drop as
with a as (
    select q.id, q.sport, q.source, q.book, q.event_id, q.player_key, q.market, q.line, q.side,
           q.fetched_at, q.price_american, q.game_id, q.player_id, q.stat, {_UNUSED_BOOK} as unused,
           coalesce(g.start_time, max(q.last_seen_at) over (partition by q.sport, q.source, q.book, q.event_id,
                                                                         q.player_key, q.market, q.line)) as tip
    from book_quotes q left join games g on g.game_id = q.game_id
    where q.sport = %(sport)s
)
select a.*,
       row_number() over (partition by {LINE}, side order by fetched_at, id) as rn_open,
       row_number() over (partition by {LINE}, side order by (fetched_at <= tip) desc, fetched_at desc, id desc) as rn_close
from a where tip < %(cutoff)s"""

_STAGE_KALSHI = """
create temp table prune_kalshi on commit drop as
with a as (
    select q.id, q.sport, q.ticker, q.fetched_at, q.yes_ask, q.no_ask, m.series_ticker, m.event_ticker,
           m.market_kind, m.stat, coalesce(m.line, m.floor_strike, 0) as line,
           coalesce(m.kalshi_fragment, '') as player_key, m.player_id, m.game_id,
           coalesce(g.start_time, max(q.last_seen_at) over (partition by q.sport, q.ticker)) as tip
    from kalshi_quotes q
    join kalshi_markets m on m.sport = q.sport and m.ticker = q.ticker
    left join games g on g.game_id = m.game_id
    where q.sport = %(sport)s
)
select a.*,
       row_number() over (partition by sport, ticker order by fetched_at, id) as rn_open,
       row_number() over (partition by sport, ticker order by (fetched_at <= tip) desc, fetched_at desc, id desc) as rn_close
from a where tip < %(cutoff)s"""

# Which settled tickers get a quote_history row: every non-ladder market, and for spread / total
# ladders the strike whose closing mid (yes ask and 1 - no ask) is closest to 50c.
_KALSHI_KEEP = """
create temp table prune_kalshi_keep on commit drop as
select ticker from (
    select ticker, market_kind,
           row_number() over (partition by sport, event_ticker, series_ticker
                              order by abs(mid - 0.5) nulls last, ticker) as rk
    from (select sport, ticker, event_ticker, series_ticker, market_kind,
                 coalesce((yes_ask + (1 - no_ask)) / 2, yes_ask, 1 - no_ask) as mid
          from prune_kalshi where rn_close = 1) c
) r
where market_kind <> all(%(one_strike)s) or rk = 1"""

_HISTORY_COLS = """line_key, sport, venue, source, book, event_key, player_key, market, line, game_id, player_id,
                   stat, price_unit, open_over, open_under, close_over, close_under, opened_at, closed_at,
                   rows_compacted"""

_COMPACT_BOOK = f"""
select quote_history_line_key(sport, 'book', source, book, event_id, player_key, market, line),
       sport, 'book', source, book, event_id, player_key, market, line, max(game_id), max(player_id), max(stat),
       'american',
       max(price_american) filter (where side = 'over' and rn_open = 1),
       max(price_american) filter (where side = 'under' and rn_open = 1),
       max(price_american) filter (where side = 'over' and rn_close = 1),
       max(price_american) filter (where side = 'under' and rn_close = 1),
       min(fetched_at) filter (where rn_open = 1), max(fetched_at) filter (where rn_close = 1), count(*)
from prune_book {{where}} group by {LINE}"""

_COMPACT_KALSHI = """
select quote_history_line_key(sport, 'kalshi', 'kalshi', 'kalshi', ticker, player_key, series_ticker, line),
       sport, 'kalshi', 'kalshi', 'kalshi', ticker, player_key, series_ticker, line, max(game_id), max(player_id),
       max(stat), 'dollars',
       max(yes_ask) filter (where rn_open = 1), max(no_ask) filter (where rn_open = 1),
       max(yes_ask) filter (where rn_close = 1), max(no_ask) filter (where rn_close = 1),
       min(fetched_at) filter (where rn_open = 1), max(fetched_at) filter (where rn_close = 1), count(*)
from prune_kalshi {where} group by sport, ticker, player_key, series_ticker, line"""

_KEPT_BOOK = "where not unused"
_KEPT_KALSHI = "where ticker in (select ticker from prune_kalshi_keep)"

_UPSERT = f"""
insert into quote_history ({_HISTORY_COLS})
{{select}}
on conflict (line_key) do update set
    open_over  = coalesce(quote_history.open_over, excluded.open_over),
    open_under = coalesce(quote_history.open_under, excluded.open_under),
    opened_at  = least(quote_history.opened_at, excluded.opened_at),
    close_over  = case when excluded.closed_at >= quote_history.closed_at then excluded.close_over
                       else quote_history.close_over end,
    close_under = case when excluded.closed_at >= quote_history.closed_at then excluded.close_under
                       else quote_history.close_under end,
    closed_at  = greatest(quote_history.closed_at, excluded.closed_at),
    rows_compacted = quote_history.rows_compacted + excluded.rows_compacted,
    compacted_at = now()"""

_INDEX_HEAD = re.compile(r"^CREATE (UNIQUE )?INDEX \S+ ON (?:ONLY )?\S+ ", re.I)


def _measure(conn, like: str, select_sql: str, params=None) -> int:
    """Bytes (heap + indexes) the rows of `select_sql` take in a table shaped like `like`. Indexes are
    built after loading, as REINDEX leaves them (the job reindexes quote_history every run)."""
    conn.execute("drop table if exists pg_temp.prune_probe")
    conn.execute(f"create temp table prune_probe (like {like} including defaults)")
    conn.execute(f"insert into prune_probe {select_sql}", params)
    for (indexdef,) in conn.execute(
            "select indexdef from pg_indexes where schemaname = 'public' and tablename = %s", (like,)).fetchall():
        conn.execute(_INDEX_HEAD.sub(lambda m: f"CREATE {m.group(1) or ''}INDEX ON pg_temp.prune_probe ", indexdef))
    conn.execute("analyze prune_probe")
    size = conn.execute("select pg_total_relation_size('pg_temp.prune_probe')").fetchone()[0]
    conn.execute("drop table pg_temp.prune_probe")
    return int(size)


def _picked(table: str, stage: str, cond: str) -> str:
    return f"select q.* from {table} q join {stage} p using (id) where not ({cond})"


def prune(conn, now, measure: bool = False) -> dict:
    """Drop + compact + delete inside the caller's transaction. Returns counts (and bytes if measure)."""
    params = {"sport": config.SPORT, "cutoff": now - timedelta(days=KEEP_DAYS),
              "books": list(config.BOOK_QUOTE_BOOKS), "markets": list(config.PROPLINE_MARKETS),
              "one_strike": list(config.KALSHI_LADDER_ONE_STRIKE)}
    for t in ("prune_book", "prune_kalshi", "prune_kalshi_keep"):
        conn.execute(f"drop table if exists pg_temp.{t}")
    conn.execute(_STAGE_BOOK, params)
    conn.execute(_STAGE_KALSHI, params)
    conn.execute(_KALSHI_KEEP, params)
    out = {"cutoff": params["cutoff"].isoformat()}
    out["games"] = conn.execute("""select count(distinct game_id) from (select game_id from prune_book
                                   union all select game_id from prune_kalshi) t
                                   where game_id is not null""").fetchone()[0]

    if measure:
        picked_book = _picked("book_quotes", "prune_book", _NOT_PICKED_BOOK)
        picked_kalshi = _picked("kalshi_quotes", "prune_kalshi", _NOT_PICKED_KALSHI)
        kept_rows = _measure(conn, "book_quotes", picked_book) + _measure(conn, "kalshi_quotes", picked_kalshi)
        out["bytes_before"] = (
            _measure(conn, "book_quotes", "select q.* from book_quotes q join prune_book p using (id)")
            + _measure(conn, "kalshi_quotes", "select q.* from kalshi_quotes q join prune_kalshi p using (id)")
            + _measure(conn, "kalshi_markets", "select m.* from kalshi_markets m "
                                               "where m.ticker in (select ticker from prune_kalshi)"))
        out["bytes_after_compaction_only"] = (
            _measure(conn, "quote_history", _COMPACT_BOOK.format(where=""))
            + _measure(conn, "quote_history", _COMPACT_KALSHI.format(where="")) + kept_rows)
        out["bytes_after"] = (
            _measure(conn, "quote_history", _COMPACT_BOOK.format(where=_KEPT_BOOK))
            + _measure(conn, "quote_history", _COMPACT_KALSHI.format(where=_KEPT_KALSHI)) + kept_rows)

    # 1. drop rows the model never uses, at any age (ingest already skips them)
    dropped_book = conn.execute(f"delete from book_quotes q where q.sport = %(sport)s and {_UNUSED_BOOK} "
                                f"and {_NOT_PICKED_BOOK}", params).rowcount
    # 3. compact what's settled and kept, then delete every settled row that isn't pick-referenced
    lines_book = conn.execute(_UPSERT.format(select=_COMPACT_BOOK.format(where=_KEPT_BOOK))).rowcount
    lines_kalshi = conn.execute(_UPSERT.format(select=_COMPACT_KALSHI.format(where=_KEPT_KALSHI))).rowcount
    deleted_book = conn.execute(
        f"delete from book_quotes q using prune_book p where q.id = p.id and {_NOT_PICKED_BOOK}").rowcount
    deleted_kalshi = conn.execute(
        f"delete from kalshi_quotes q using prune_kalshi p where q.id = p.id and {_NOT_PICKED_KALSHI}").rowcount
    deleted_markets = conn.execute(
        """delete from kalshi_markets m where m.sport = %s and m.ticker in (select ticker from prune_kalshi)
           and not exists (select 1 from kalshi_quotes q where q.sport = m.sport and q.ticker = m.ticker)""",
        (config.SPORT,)).rowcount
    # 5. old job_runs
    deleted_runs = conn.execute(
        "delete from job_runs where sport = %s and started_at < %s and status <> 'running'",
        (config.SPORT, now - timedelta(days=config.JOB_RUNS_KEEP_DAYS))).rowcount

    settled_unused = conn.execute("select count(*) from prune_book where unused").fetchone()[0]
    tickers = conn.execute("select count(distinct ticker) from prune_kalshi").fetchone()[0]
    out.update(book_quotes={"rows_folded": conn.execute("select count(*) from prune_book").fetchone()[0]
                            - settled_unused, "lines": lines_book, "deleted": deleted_book},
               book_rows_dropped_unused=dropped_book,
               kalshi_quotes={"rows_folded": conn.execute("select count(*) from prune_kalshi").fetchone()[0],
                              "lines": lines_kalshi, "deleted": deleted_kalshi},
               kalshi_strikes_dropped=tickers - lines_kalshi if tickers >= lines_kalshi else 0,
               kalshi_markets_deleted=deleted_markets, job_runs_deleted=deleted_runs)
    return out


def _maintenance(conn, sql: str, done: str) -> str:
    import psycopg

    conn.commit()
    conn.autocommit = True
    try:
        conn.execute("set lock_timeout = '30s'")
        conn.execute(sql)
        return done
    except psycopg.errors.LockNotAvailable:
        return f"{sql.split()[0].lower()} skipped (tables busy)"
    finally:
        conn.execute("reset lock_timeout")
        conn.autocommit = False


def vacuum_quotes(conn) -> str:
    vac = _maintenance(conn, "vacuum (analyze) book_quotes, kalshi_quotes, kalshi_markets, quote_history, job_runs",
                       "vacuumed")
    if vac != "vacuumed":
        vac += "; autovacuum will reclaim the space"
    return f"{vac}; {_maintenance(conn, 'reindex table quote_history', 'quote_history reindexed')}"


def db_mb(conn) -> float:
    return float(conn.execute("select pg_database_size(current_database()) / 1e6").fetchone()[0])


def projection(conn, out: dict, now, size_mb: float) -> dict:
    """End-of-season database size: today's size, minus what this compaction frees, plus every game
    left at the measured per-game cost (quotes after compaction + box scores + small tables), plus
    job_runs growing to its retention limit."""
    sport = config.SPORT
    played = conn.execute(
        "select count(*) from games where sport = %s and status = 'final' and game_date >= %s",
        (sport, season_start(now.astimezone(config.ET).date()))).fetchone()[0]
    left = max(config.SEASON_GAMES - played, 0)
    gl_rows, gl_games = conn.execute(
        "select count(*), count(distinct game_id) from game_logs where sport = %s", (sport,)).fetchone()
    gl_bytes = conn.execute("select pg_total_relation_size('game_logs')").fetchone()[0]
    box_kb = gl_bytes / max(gl_rows, 1) * (gl_rows / max(gl_games, 1)) / 1024
    quotes_kb = out["bytes_after"] / max(out["games"], 1) / 1024
    runs_bytes = conn.execute("select pg_total_relation_size('job_runs')").fetchone()[0]
    runs_total, runs_week = conn.execute(
        "select count(*), count(*) filter (where started_at >= %s) from job_runs where sport = %s",
        (now - timedelta(days=7), sport)).fetchone()
    runs_cap = runs_bytes / max(runs_total, 1) * runs_week / 7 * config.JOB_RUNS_KEEP_DAYS
    runs_growth_mb = max(runs_cap - runs_bytes, 0) / 1e6
    freed_mb = (out["bytes_before"] - out["bytes_after"]) / 1e6
    per_game_kb = quotes_kb + box_kb + OTHER_KB_PER_GAME
    total = size_mb - freed_mb + left * per_game_kb / 1024 + runs_growth_mb
    return {"games_played": played, "games_left": left, "quotes_kb_per_game": round(quotes_kb, 1),
            "box_kb_per_game": round(box_kb, 1), "other_kb_per_game": OTHER_KB_PER_GAME,
            "job_runs_growth_mb": round(runs_growth_mb, 1), "freed_now_mb": round(freed_mb, 1),
            "season_end_mb": round(total, 0), "headroom_mb": round(500 - total, 0)}


def run(ctx: Context) -> JobResult:
    dry = bool(ctx.options.get("dry_run"))
    as_of = ctx.options.get("as_of")
    if as_of and not dry:
        raise ValueError("--as-of is only allowed with --dry-run")
    now = as_of or ctx.now
    if dry:
        with ctx.conn.transaction(force_rollback=True):
            out = prune(ctx.conn, now, measure=True)
        vac = "dry run: rolled back, nothing changed"
    else:
        out = prune(ctx.conn, now)
        vac = vacuum_quotes(ctx.conn)
    size = db_mb(ctx.conn)
    if dry:
        out["projection"] = projection(ctx.conn, out, now, size)
    folded = out["book_quotes"]["rows_folded"] + out["kalshi_quotes"]["rows_folded"]
    deleted = out["book_quotes"]["deleted"] + out["kalshi_quotes"]["deleted"] + out["book_rows_dropped_unused"]
    red = [f"database at {size:.0f} MB of 500 MB (red above {PRUNE_RED_MB})"] if size > PRUNE_RED_MB else []
    warn = [f"database at {size:.0f} MB of 500 MB"] if PRUNE_WARN_MB < size <= PRUNE_RED_MB else []
    return JobResult(rows_in=folded, rows_out=deleted, expected=False, red_reasons=red, warnings=warn,
                     meta={**out, "vacuum": vac, "db_mb": round(size, 1), "dry_run": dry, "keep_days": KEEP_DAYS,
                           "as_of": (as_of or ctx.now).isoformat()})


def render(m: dict, dry: bool) -> str:
    verb = "would" if dry else "did"
    lines = [f"  cutoff {m['cutoff']} (games that started before it are settled), {m['games']} game(s)",
             f"  unused books/markets/alt lines: {verb} delete {m['book_rows_dropped_unused']} book_quotes rows "
             f"(kept: {', '.join(config.BOOK_QUOTE_BOOKS)})"]
    for t in ("book_quotes", "kalshi_quotes"):
        v = m[t]
        lines.append(f"  {t:14} {v['rows_folded']:>7} rows -> {v['lines']:>6} quote_history lines; "
                     f"{verb} delete {v['deleted']} (pick-used rows kept)")
    lines.append(f"  kalshi spread/total ladders: {m['kalshi_strikes_dropped']} off-center strike(s) not kept; "
                 f"kalshi_markets {verb} delete {m['kalshi_markets_deleted']} settled market(s)")
    lines.append(f"  job_runs older than {config.JOB_RUNS_KEEP_DAYS} days: {verb} delete {m['job_runs_deleted']}")
    if "bytes_before" in m:
        g = max(m["games"], 1)
        b, c, a = m["bytes_before"], m["bytes_after_compaction_only"], m["bytes_after"]
        pct = (lambda x: f"{1 - x / b:.0%} smaller") if b else (lambda x: "nothing to compact")
        lines.append(f"  bytes per game: before {b / g / 1024:,.0f} KB; compaction alone {c / g / 1024:,.0f} KB "
                     f"({pct(c)}); with the drops {a / g / 1024:,.0f} KB ({pct(a)}); "
                     f"total {b / 1e6:.2f} MB -> {a / 1e6:.2f} MB")
    lines.append(f"  database {m['db_mb']} MB of 500 MB ({m['vacuum']})")
    p = m.get("projection")
    if p:
        lines.append(f"  season projection: {p['season_end_mb']:.0f} MB at season end, {p['headroom_mb']:.0f} MB under "
                     f"the 500 MB cap ({p['games_played']} games played, {p['games_left']} left x "
                     f"{p['quotes_kb_per_game'] + p['box_kb_per_game'] + p['other_kb_per_game']:.0f} KB "
                     f"[quotes {p['quotes_kb_per_game']:.0f} + box scores {p['box_kb_per_game']:.0f} + other "
                     f"{p['other_kb_per_game']}]; job_runs +{p['job_runs_growth_mb']} MB to its "
                     f"{config.JOB_RUNS_KEEP_DAYS}-day limit; this run frees {p['freed_now_mb']} MB)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="compact inside a rolled-back transaction; show bytes/game")
    ap.add_argument("--as-of", type=parse_ts_arg, help="dry run only: pretend it is this time (ISO, ET if no offset)")
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args(argv)
    if args.as_of and not args.dry_run:
        ap.error("--as-of only works with --dry-run")
    code, outs = run_all([("prune", run)], options={"dry_run": args.dry_run, "as_of": args.as_of},
                         alert=not args.no_alert, title="Prune")
    if outs[0].status != "error":
        print(render(outs[0].meta, args.dry_run))
    return code


def parse_ts_arg(value: str) -> datetime:
    """'2026-11-14T12:00' (ET) or any ISO time with an offset -> aware UTC datetime."""
    dt = datetime.fromisoformat(value)
    return (dt if dt.tzinfo else dt.replace(tzinfo=config.ET)).astimezone(timezone.utc)


if __name__ == "__main__":
    sys.exit(main())
