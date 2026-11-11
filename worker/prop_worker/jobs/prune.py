"""prune: thin settled quote history so the database stays under the 500 MB free-tier cap.

Run:  python -m prop_worker.jobs.prune [--dry-run] [--no-alert]

Rule (approved 2026-11-11):
  * Full intraday history is kept until a game started more than PRUNE_KEEP_DAYS (2) days ago.
  * After that, per quote line, only these rows survive:
      opening   the first stored price
      closing   the last price stored at or before tip-off (start_time)
      pick      any row a pick references (picks.book_quote_id / picks.kalshi_quote_id)
A "quote line" is book_quotes (source, book, event, player, market, side, line) or one Kalshi ticker.
Quotes not attached to a game use their own last-seen time instead of tip-off.

Then VACUUM (30 s lock timeout) so freed space is reused. Red if the database passes
PRUNE_RED_MB (400 MB of 500); a warning past PRUNE_WARN_MB (300).
"""

from __future__ import annotations

import argparse
import sys

from .. import config
from ..runner import Context, JobResult, run_all

KEEP_DAYS = 2
PRUNE_WARN_MB = 300
PRUNE_RED_MB = 400

BOOK_KEY = "q.sport, q.source, q.book, q.event_id, q.player_key, q.market, q.side, q.line"

_BOOK = f"""
with a as (
    select q.id, q.fetched_at, {BOOK_KEY},
           coalesce(g.start_time, max(q.last_seen_at) over (partition by {BOOK_KEY})) as tip
    from book_quotes q left join games g on g.game_id = q.game_id
    where q.sport = %(sport)s
), b as (
    select id, tip,
           row_number() over (partition by sport, source, book, event_id, player_key, market, side, line
                              order by fetched_at, id) as rn_open,
           row_number() over (partition by sport, source, book, event_id, player_key, market, side, line
                              order by (fetched_at <= tip) desc, fetched_at desc, id desc) as rn_close
    from a q
)
{{action}} b
where q.id = b.id and b.tip < %(cutoff)s and b.rn_open > 1 and b.rn_close > 1
  and not exists (select 1 from picks p where p.book_quote_id = q.id)"""

_KALSHI = """
with a as (
    select q.id, q.ticker, q.fetched_at,
           coalesce(g.start_time, max(q.last_seen_at) over (partition by q.sport, q.ticker)) as tip, q.sport
    from kalshi_quotes q
    join kalshi_markets m on m.sport = q.sport and m.ticker = q.ticker
    left join games g on g.game_id = m.game_id
    where q.sport = %(sport)s
), b as (
    select id, tip,
           row_number() over (partition by sport, ticker order by fetched_at, id) as rn_open,
           row_number() over (partition by sport, ticker order by (fetched_at <= tip) desc, fetched_at desc,
                              id desc) as rn_close
    from a
)
{action} b
where q.id = b.id and b.tip < %(cutoff)s and b.rn_open > 1 and b.rn_close > 1
  and not exists (select 1 from picks p where p.kalshi_quote_id = q.id)"""


def _stmt(template: str, table: str, dry_run: bool) -> str:
    action = f"select count(*) from {table} q,"
    if not dry_run:
        action = f"delete from {table} q using"
    return template.format(action=action)


def prune(conn, now, dry_run: bool = False) -> dict:
    params = {"sport": config.SPORT, "cutoff": now - _days(KEEP_DAYS)}
    out = {}
    for table, template in (("book_quotes", _BOOK), ("kalshi_quotes", _KALSHI)):
        total = conn.execute(f"select count(*) from {table} where sport = %s", (config.SPORT,)).fetchone()[0]
        cur = conn.execute(_stmt(template, table, dry_run), params)
        removed = cur.fetchone()[0] if dry_run else cur.rowcount
        out[table] = {"rows_before": total, "removed": removed}
    return out


def _days(n):
    from datetime import timedelta
    return timedelta(days=n)


def vacuum_quotes(conn) -> str:
    import psycopg

    conn.commit()
    conn.autocommit = True
    try:
        conn.execute("set lock_timeout = '30s'")
        conn.execute("vacuum (analyze) book_quotes, kalshi_quotes")
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
    out = prune(ctx.conn, ctx.now, dry_run=dry)
    vac = "dry run: nothing deleted" if dry else vacuum_quotes(ctx.conn)
    size = db_mb(ctx.conn)
    removed = sum(v["removed"] for v in out.values())
    rows = sum(v["rows_before"] for v in out.values())
    red = [f"database at {size:.0f} MB of 500 MB (red above {PRUNE_RED_MB})"] if size > PRUNE_RED_MB else []
    warn = [f"database at {size:.0f} MB of 500 MB"] if PRUNE_WARN_MB < size <= PRUNE_RED_MB else []
    return JobResult(rows_in=rows, rows_out=removed, expected=False, red_reasons=red, warnings=warn,
                     meta={**out, "vacuum": vac, "db_mb": round(size, 1), "dry_run": dry,
                           "keep_days": KEEP_DAYS})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="count what would be removed, delete nothing")
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args(argv)
    code, outs = run_all([("prune", run)], options={"dry_run": args.dry_run}, alert=not args.no_alert)
    m = outs[0].meta or {}
    for t in ("book_quotes", "kalshi_quotes"):
        if t in m:
            verb = "would remove" if args.dry_run else "removed"
            print(f"  {t:14} {m[t]['rows_before']:>8} rows, {verb} {m[t]['removed']}")
    if "db_mb" in m:
        print(f"  database {m['db_mb']} MB of 500 MB ({m['vacuum']})")
    return code


if __name__ == "__main__":
    sys.exit(main())
