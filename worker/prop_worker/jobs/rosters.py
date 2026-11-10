"""rosters: ESPN team rosters -> player_xref + players.team_abbr (once per slate day).

Run:  python -m prop_worker.jobs.rosters [--force] [--no-alert]

Keeps current teams fresh (trades), which the Kalshi and injury-PDF name matches depend on, and
links new ESPN athletes. Runs at most once per slate day unless --force; other runs are skipped
(recorded as green with expected = false).
"""

from __future__ import annotations

import argparse
import sys

from .. import config
from ..runner import Context, JobResult, run_all
from ..xref import link


def ran_today(ctx: Context) -> bool:
    return ctx.conn.execute(
        """select exists (select 1 from job_runs where sport = %s and job = 'rosters' and status = 'green'
                  and expected and (meta->>'slate') = %s)""", (config.SPORT, ctx.slate.isoformat())).fetchone()[0]


def run(ctx: Context) -> JobResult:
    if not ctx.options.get("force") and ran_today(ctx):
        return JobResult(0, 0, expected=False, meta={"skipped": "already ran this slate day"})
    out = link(ctx.conn, session=ctx.session, skip={"kalshi", "propline"}, now=ctx.now)
    st = out["stats"].get("espn", {"known": 0, "linked": 0, "unmatched": 0})
    warnings = [f"{st['unmatched']} ESPN roster player(s) unmatched (see xref report)"] if st["unmatched"] else []
    return JobResult(rows_in=out["fetched"].get("espn", 0), rows_out=st["known"] + st["linked"],
                     expected=True, units=None, warnings=warnings, meta={"espn": st})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args(argv)
    code, _ = run_all([("rosters", run)], options={"force": args.force}, alert=not args.no_alert)
    return code


if __name__ == "__main__":
    sys.exit(main())
