"""Run every ingest job in order, each with its own job_runs row; one ntfy push if any is red.

Run:  python -m prop_worker.ingest [--jobs rosters,schedule,boxscores,injuries,kalshi,propline]
                                    [--window 09:00-01:00] [--min-gap-minutes 10] [--no-alert]

Order: rosters (current teams) -> schedule (games) -> boxscores -> injuries -> kalshi -> propline.
A failing job doesn't stop the ones after it. Exit code 1 if any job was red or errored.
--window: do nothing (exit 0) outside this ET window. .github/workflows/ingest.yml fires every 15 min
on UTC hours that cover 9am-1am ET in both EST and EDT, and passes --window 09:00-01:00.
--min-gap-minutes: do nothing (exit 0) if an ingest job already ran (green or red) that recently,
from anywhere. Two triggers fire every slot (GitHub's own cron, which can be late or skip, and the
external cron calling workflow_dispatch, see docs/SCHEDULING.md); this keeps a slot from running twice
and spending PropLine quota twice. An errored run doesn't count, so a retry goes ahead.
"""

from __future__ import annotations

import argparse
import sys

from datetime import timedelta

from . import config
from .db import connect
from .jobs import boxscores, injuries, kalshi, propline, rosters, schedule
from .runner import run_all
from .schedule_window import in_window
from .util import utcnow

JOBS = {"rosters": rosters.run, "schedule": schedule.run, "boxscores": boxscores.run,
        "injuries": injuries.run, "kalshi": kalshi.run, "propline": propline.run}


def main(argv=None, now=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", default=",".join(JOBS), help="comma list, run in the order given")
    ap.add_argument("--window", help="ET window like 09:00-01:00; outside it, exit 0 without running")
    ap.add_argument("--min-gap-minutes", type=float,
                    help="exit 0 without running if an ingest job ran (green/red) this recently")
    ap.add_argument("--no-alert", action="store_true", help="don't send an ntfy push on red/error")
    args = ap.parse_args(argv)
    names = [j.strip() for j in args.jobs.split(",") if j.strip()]
    unknown = [j for j in names if j not in JOBS]
    if unknown:
        ap.error(f"unknown job(s) {unknown}; choose from {list(JOBS)}")
    now = now or utcnow()
    if args.window and not in_window(now, args.window):
        print(f"outside the {args.window} ET window; nothing to do")
        return 0
    if args.min_gap_minutes:
        with connect() as conn:
            last = last_ingest_run(conn, now, args.min_gap_minutes)
        if last is not None:
            print(f"an ingest job already ran at {last.astimezone(config.ET):%H:%M} ET "
                  f"(< {args.min_gap_minutes:g} min ago); nothing to do")
            return 0
    code, _ = run_all([(n, JOBS[n]) for n in names], alert=not args.no_alert, now=now)
    return code


def last_ingest_run(conn, now, minutes: float):
    """Start time of the latest non-error ingest job run within `minutes` of now, else None."""
    row = conn.execute(
        """select max(started_at) from job_runs
           where sport = %s and job = any(%s) and status <> 'error' and started_at > %s and started_at <= %s""",
        (config.SPORT, list(JOBS), now - timedelta(minutes=minutes), now + timedelta(minutes=1))).fetchone()
    return row[0] if row else None


if __name__ == "__main__":
    sys.exit(main())
