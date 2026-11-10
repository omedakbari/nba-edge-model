"""Pipeline status: latest run per job, green/red by slate day, and database size vs the free-tier cap.

Run:  python -m prop_worker.status [--days 7]

The Phase 2 gate is "three game days in a row with every job green": the per-day table shows, for
each slate day, how many runs each job had and how many were red or errored.
"""

from __future__ import annotations

import argparse
import sys

from . import config
from .db import connect

CAP_MB = 500


def latest_runs(conn):
    return conn.execute(
        """select distinct on (job) job, started_at, status, duration_s, rows_out, red_reasons, error
           from job_runs where sport = %s order by job, started_at desc""", (config.SPORT,)).fetchall()


def by_day(conn, days: int):
    return conn.execute(
        """select (meta->>'slate')::date as slate, job, count(*) as runs,
                  count(*) filter (where status in ('red', 'error')) as bad,
                  bool_or(is_game_day) as game_day
           from job_runs
           where sport = %s and meta ? 'slate' and (meta->>'slate')::date >= current_date - %s
           group by 1, 2 order by 1 desc, 2""", (config.SPORT, days)).fetchall()


def db_size_mb(conn) -> float:
    return conn.execute("select pg_database_size(current_database())").fetchone()[0] / 1e6


def table_sizes(conn):
    return conn.execute(
        """select relname, pg_total_relation_size(relid) / 1e6 from pg_stat_user_tables
           order by 2 desc limit 8""").fetchall()


def render(latest, days, size_mb, tables) -> str:
    out = ["Latest run per job:"]
    for job, at, status, dur, rows, reasons, error in latest:
        why = "; ".join(reasons or []) or (error or "")
        out.append(f"  {job:10} {at.astimezone(config.ET):%m-%d %H:%M} ET  {status.upper():5} "
                   f"{float(dur or 0):5.1f}s  rows {rows if rows is not None else '-':>6}  {why}")
    out.append("")
    out.append("By slate day (runs / red+error):")
    slates: dict = {}
    for slate, job, runs, bad, game_day in days:
        slates.setdefault(slate, {"jobs": {}, "game_day": False})
        slates[slate]["jobs"][job] = (runs, bad)
        slates[slate]["game_day"] |= bool(game_day)
    for slate, d in slates.items():
        all_green = all(bad == 0 for _, bad in d["jobs"].values())
        cells = "  ".join(f"{j} {r}/{b}" for j, (r, b) in sorted(d["jobs"].items()))
        verdict = "ALL GREEN" if all_green else "HAS RED"
        out.append(f"  {slate}  {'game day' if d['game_day'] else 'off day '}  {verdict:9}  {cells}")
    out.append("")
    out.append(f"Database: {size_mb:.0f} MB of {CAP_MB} MB ({size_mb / CAP_MB:.0%})")
    out += [f"  {name:16} {mb:7.1f} MB" for name, mb in tables]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    with connect() as conn:
        print(render(latest_runs(conn), by_day(conn, args.days), db_size_mb(conn), table_sizes(conn)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
