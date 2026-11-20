"""Nightly (6am ET): finish last night's box scores, validate them, prune quotes, grade picks and bets.

Run:  python -m prop_worker.nightly [--jobs schedule,boxscores,validate,prune,grade,bets]
                                     [--from-et-hour 6] [--no-alert]

schedule + boxscores first so games that ended after the last 1am ingest run are loaded before the
10-player validation (against PropLine) looks at them. Exit 1 if any job is red or errored.
--from-et-hour 6: .github/workflows/nightly.yml fires at 10:00 and 11:00 UTC (6am ET in EDT and EST), but
GitHub starts scheduled runs hours late (Nov 2026: 1-3pm ET, every day). So the first run at or after
6am ET does the work and any later run that day exits 0. (--only-at-et-hour, the old exact-hour gate,
skipped every late run and left grading/validation/bets undone from 11-13 to 11-20.)
"""

from __future__ import annotations

import argparse
import sys

from .jobs import bets_grade, boxscores, grade, prune, schedule, validate
from .runner import run_all
from .schedule_window import at_hour
from . import config
from .db import connect
from .util import utcnow

JOBS = {"schedule": schedule.run, "boxscores": boxscores.run, "validate": validate.run,
        "prune": prune.run, "grade": grade.run, "bets": bets_grade.run}
NIGHTLY_ONLY = ("validate", "prune", "grade", "bets")       # schedule/boxscores also run every ingest


def ran_since(since) -> bool:
    """True if a nightly-only job has a job_runs row started at or after `since`."""
    with connect() as conn:
        return conn.execute("select 1 from job_runs where job = any(%s) and started_at >= %s limit 1",
                            (list(NIGHTLY_ONLY), since)).fetchone() is not None


def main(argv=None, now=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", default=",".join(JOBS))
    ap.add_argument("--only-at-et-hour", type=int)
    ap.add_argument("--from-et-hour", type=int)
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args(argv)
    names = [j.strip() for j in args.jobs.split(",") if j.strip()]
    unknown = [j for j in names if j not in JOBS]
    if unknown:
        ap.error(f"unknown job(s) {unknown}; choose from {list(JOBS)}")
    now = now or utcnow()
    if args.only_at_et_hour is not None and not at_hour(now, args.only_at_et_hour):
        print(f"not {args.only_at_et_hour}:00 ET; nothing to do")
        return 0
    if args.from_et_hour is not None:
        et = now.astimezone(config.ET)
        start = et.replace(hour=args.from_et_hour, minute=0, second=0, microsecond=0)
        if et < start:
            print(f"before {args.from_et_hour}:00 ET; nothing to do")
            return 0
        if ran_since(start):
            print(f"nightly already ran since {start:%Y-%m-%d %H:%M} ET; nothing to do")
            return 0
    code, _ = run_all([(n, JOBS[n]) for n in names], alert=not args.no_alert, now=now, title="Nightly")
    return code


if __name__ == "__main__":
    sys.exit(main())
