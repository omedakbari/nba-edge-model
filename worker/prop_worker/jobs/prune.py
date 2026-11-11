"""prune: compact settled quote history so the database stays under the 500 MB free-tier cap.

Run:  python -m prop_worker.jobs.prune [--dry-run [--as-of 2026-11-14T12:00]] [--no-alert]

Rule (approved 2026-11-11):
  * Full intraday history is kept until a game started more than KEEP_DAYS (2) days ago.
  * After that each quote line is folded into ONE quote_history row (opening and closing price for
    both sides; see migrations/005_quote_history.sql) and its rows are deleted from book_quotes /
    kalshi_quotes, EXCEPT rows a pick references (picks.book_quote_id / kalshi_quote_id), which
    stay as full rows. Settled Kalshi market metadata (kalshi_markets) is deleted once none of its
    quotes remain (ticker, series, line, player and game live on in quote_history).
  * opening = first stored price per side; closing = last price stored at or before tip-off.
    Quotes not attached to a game use their own last-seen time as "tip-off".
  * Then VACUUM (30 s lock timeout) so freed space is reused.

--dry-run does everything inside a transaction that is rolled back, and reports measured bytes per
game before and after (heap + indexes, measured on temporary copies with the same indexes).
--as-of (dry run only) pretends it is that time, to preview games that haven't settled yet.
Red if the database passes 400 MB of 500; a warning past 300.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from .. import config
from ..runner import Context, JobResult, run_all

KEEP_DAYS = 2
PRUNE_WARN_MB = 300
PRUNE_RED_MB = 400

LINE = "sport, source, book, event_id, player_key, market, line"

_STAGE_BOOK = f"""
create temp table prune_book on commit drop as
with a as (
    select q.id, q.sport, q.source, q.book, q.event_id, q.player_key, q.market, q.line, q.side,
           q.fetched_at, q.price_american, q.game_id, q.player_id, q.stat,
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
    select q.id, q.sport, q.ticker, q.fetched_at, q.yes_ask, q.no_ask, m.series_ticker, m.stat,
           coalesce(m.line, m.floor_strike, 0) as line, coalesce(m.kalshi_fragment, '') as player_key,
           m.player_id, m.game_id,
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
from prune_book group by {LINE}"""

_COMPACT_KALSHI = """
select quote_history_line_key(sport, 'kalshi', 'kalshi', 'kalshi', ticker, player_key, series_ticker, line),
       sport, 'kalshi', 'kalshi', 'kalshi', ticker, player_key, series_ticker, line, max(game_id), max(player_id),
       max(stat), 'dollars',
       max(yes_ask) filter (where rn_open = 1), max(no_ask) filter (where rn_open = 1),
       max(yes_ask) filter (where rn_close = 1), max(no_ask) filter (where rn_close = 1),
       min(fetched_at) filter (where rn_open = 1), max(fetched_at) filter (where rn_close = 1), count(*)
from prune_kalshi group by sport, ticker, player_key, series_ticker, line"""

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


def _measure(conn, like: str, select_sql: str, params=None) -> int:
    """Bytes (heap + indexes) the rows of `select_sql` take in a table shaped like `like`."""
    conn.execute("drop table if exists pg_temp.prune_probe")
    conn.execute(f"create temp table prune_probe (like {like} including indexes including defaults)")
    conn.execute(f"insert into prune_probe {select_sql}", params)
    conn.execute("analyze prune_probe")
    size = conn.execute("select pg_total_relation_size('pg_temp.prune_probe')").fetchone()[0]
    conn.execute("drop table pg_temp.prune_probe")
    return int(size)


def prune(conn, now, measure: bool = False) -> dict:
    """Compact + delete inside the caller's transaction. Returns counts (and bytes if measure)."""
    params = {"sport": config.SPORT, "cutoff": now - timedelta(days=KEEP_DAYS)}
    conn.execute("drop table if exists pg_temp.prune_book")
    conn.execute("drop table if exists pg_temp.prune_kalshi")
    conn.execute(_STAGE_BOOK, params)
    conn.execute(_STAGE_KALSHI, params)
    out = {"cutoff": params["cutoff"].isoformat()}
    games = conn.execute("""select count(distinct game_id) from (select game_id from prune_book
                            union all select game_id from prune_kalshi) t where game_id is not null""").fetchone()[0]
    out["games"] = games

    if measure:
        before = (_measure(conn, "book_quotes", "select q.* from book_quotes q join prune_book p using (id)")
                  + _measure(conn, "kalshi_quotes", "select q.* from kalshi_quotes q join prune_kalshi p using (id)")
                  + _measure(conn, "kalshi_markets", """select m.* from kalshi_markets m
                                                        where m.ticker in (select ticker from prune_kalshi)"""))
        after = (_measure(conn, "quote_history", _COMPACT_BOOK) + _measure(conn, "quote_history", _COMPACT_KALSHI)
                 + _measure(conn, "book_quotes", """select q.* from book_quotes q join prune_book p using (id)
                                                   where exists (select 1 from picks k where k.book_quote_id = q.id)""")
                 + _measure(conn, "kalshi_quotes", """select q.* from kalshi_quotes q join prune_kalshi p using (id)
                                                     where exists (select 1 from picks k where k.kalshi_quote_id = q.id)"""))
        out["bytes_before"], out["bytes_after"] = before, after

    lines_book = conn.execute(_UPSERT.format(select=_COMPACT_BOOK)).rowcount
    lines_kalshi = conn.execute(_UPSERT.format(select=_COMPACT_KALSHI)).rowcount
    deleted_book = conn.execute(
        """delete from book_quotes q using prune_book p where q.id = p.id
           and not exists (select 1 from picks k where k.book_quote_id = q.id)""").rowcount
    deleted_kalshi = conn.execute(
        """delete from kalshi_quotes q using prune_kalshi p where q.id = p.id
           and not exists (select 1 from picks k where k.kalshi_quote_id = q.id)""").rowcount
    deleted_markets = conn.execute(
        """delete from kalshi_markets m where m.sport = %s and m.ticker in (select ticker from prune_kalshi)
           and not exists (select 1 from kalshi_quotes q where q.sport = m.sport and q.ticker = m.ticker)""",
        (config.SPORT,)).rowcount
    out.update(book_quotes={"rows_folded": conn.execute("select count(*) from prune_book").fetchone()[0],
                            "lines": lines_book, "deleted": deleted_book},
               kalshi_quotes={"rows_folded": conn.execute("select count(*) from prune_kalshi").fetchone()[0],
                              "lines": lines_kalshi, "deleted": deleted_kalshi},
               kalshi_markets_deleted=deleted_markets)
    return out


def vacuum_quotes(conn) -> str:
    import psycopg

    conn.commit()
    conn.autocommit = True
    try:
        conn.execute("set lock_timeout = '30s'")
        conn.execute("vacuum (analyze) book_quotes, kalshi_quotes, kalshi_markets, quote_history")
        return "vacuumed"
    except psycopg.errors.LockNotAvailable:
        return "vacuum skipped (tables busy); autovacuum will reclaim the space"
    finally:
        conn.execute("reset lock_timeout")
        conn.autocommit = False


def db_mb(conn) -> float:
    return float(conn.execute("select pg_database_size(current_database()) / 1e6").fetchone()[0])


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
    folded = out["book_quotes"]["rows_folded"] + out["kalshi_quotes"]["rows_folded"]
    deleted = out["book_quotes"]["deleted"] + out["kalshi_quotes"]["deleted"]
    red = [f"database at {size:.0f} MB of 500 MB (red above {PRUNE_RED_MB})"] if size > PRUNE_RED_MB else []
    warn = [f"database at {size:.0f} MB of 500 MB"] if PRUNE_WARN_MB < size <= PRUNE_RED_MB else []
    return JobResult(rows_in=folded, rows_out=deleted, expected=False, red_reasons=red, warnings=warn,
                     meta={**out, "vacuum": vac, "db_mb": round(size, 1), "dry_run": dry, "keep_days": KEEP_DAYS,
                           "as_of": (as_of or ctx.now).isoformat()})


def render(m: dict, dry: bool) -> str:
    verb = "would" if dry else "did"
    lines = [f"  cutoff {m['cutoff']} (games that started before it are settled), {m['games']} game(s)"]
    for t in ("book_quotes", "kalshi_quotes"):
        v = m[t]
        lines.append(f"  {t:14} {v['rows_folded']:>7} rows -> {v['lines']:>6} quote_history lines; "
                     f"{verb} delete {v['deleted']} (pick-used rows kept)")
    lines.append(f"  kalshi_markets {verb} delete {m['kalshi_markets_deleted']} settled market(s)")
    if "bytes_before" in m:
        g = max(m["games"], 1)
        b, a = m["bytes_before"], m["bytes_after"]
        saved = f"{1 - a / b:.0%} smaller" if b else "nothing to compact"
        lines.append(f"  bytes per game: before {b / g / 1024:,.0f} KB, after {a / g / 1024:,.0f} KB ({saved}); "
                     f"total {b / 1e6:.2f} MB -> {a / 1e6:.2f} MB")
    lines.append(f"  database {m['db_mb']} MB of 500 MB ({m['vacuum']})")
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
