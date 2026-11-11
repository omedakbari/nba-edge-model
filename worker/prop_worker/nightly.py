"""Nightly (6am ET): finish last night's box scores, validate them, prune quotes, grade (placeholder).

Run:  python -m prop_worker.nightly [--jobs schedule,boxscores,validate,prune,grade]
                                     [--only-at-et-hour 6] [--no-alert]

schedule + boxscores first so games that ended after the last 1am ingest run are loaded before the
10-player validation (against PropLine) looks at them. Exit 1 if any job is red or errored.
--only-at-et-hour: .github/workflows/nightly.yml fires at 10:00 and 11:00 UTC (6am ET in EDT and EST);
the run that isn't 6am ET exits 0 without doing anything.
"""

from __future__ import annotations

import argparse
import sys

from .jobs import boxscores, grade, prune, schedule, validate
from .runner import run_all
from .schedule_window import at_hour
from .util import utcnow

JOBS = {"schedule": schedule.run, "boxscores": boxscores.run, "validate": validate.run,
        "prune": prune.run, "grade": grade.run}


def main(argv=None, now=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", default=",".join(JOBS))
    ap.add_argument("--only-at-et-hour", type=int)
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
    code, _ = run_all([(n, JOBS[n]) for n in names], alert=not args.no_alert, now=now, title="Nightly")
    return code


if __name__ == "__main__":
    sys.exit(main())
