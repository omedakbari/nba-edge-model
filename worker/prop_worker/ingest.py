"""Run every ingest job in order, each with its own job_runs row; one ntfy push if any is red.

Run:  python -m prop_worker.ingest [--jobs rosters,schedule,boxscores,injuries,kalshi,propline]
                                    [--no-alert]

Order: rosters (current teams) -> schedule (games) -> boxscores -> injuries -> kalshi -> propline.
A failing job doesn't stop the ones after it. Exit code 1 if any job was red or errored.
Scheduled by .github/workflows/ingest.yml every 15 min, 9am-1am ET.
"""

from __future__ import annotations

import argparse
import sys

from .jobs import boxscores, injuries, kalshi, propline, rosters, schedule
from .runner import run_all

JOBS = {"rosters": rosters.run, "schedule": schedule.run, "boxscores": boxscores.run,
        "injuries": injuries.run, "kalshi": kalshi.run, "propline": propline.run}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", default=",".join(JOBS), help="comma list, run in the order given")
    ap.add_argument("--no-alert", action="store_true", help="don't send an ntfy push on red/error")
    args = ap.parse_args(argv)
    names = [j.strip() for j in args.jobs.split(",") if j.strip()]
    unknown = [j for j in names if j not in JOBS]
    if unknown:
        ap.error(f"unknown job(s) {unknown}; choose from {list(JOBS)}")
    code, _ = run_all([(n, JOBS[n]) for n in names], alert=not args.no_alert)
    return code


if __name__ == "__main__":
    sys.exit(main())
