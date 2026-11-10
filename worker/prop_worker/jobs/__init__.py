"""Ingest jobs. Each module exposes  run(ctx) -> JobResult  and a  main()  CLI entrypoint.

Order matters when run together (prop_worker.ingest): rosters refresh teams, schedule creates the
games every other job attaches to.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date


def single_job_cli(name: str, fn, description: str, date_option: bool = False, argv=None) -> int:
    """Shared CLI for `python -m prop_worker.jobs.<name>`."""
    from ..runner import run_all

    ap = argparse.ArgumentParser(description=description)
    if date_option:
        ap.add_argument("--date", type=date.fromisoformat, help="ET date (YYYY-MM-DD) instead of the default window")
    ap.add_argument("--no-alert", action="store_true", help="don't send an ntfy push on red/error")
    args = ap.parse_args(argv)
    options = {"date": getattr(args, "date", None)}
    code, _ = run_all([(name, fn)], options=options, alert=not args.no_alert)
    return code


def main_for(module_name: str, name: str, fn, doc: str, date_option: bool = False):
    if module_name == "__main__":
        sys.exit(single_job_cli(name, fn, doc.splitlines()[0], date_option))
