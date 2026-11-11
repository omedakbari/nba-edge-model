"""grade: nightly grading of picks — placeholder until Phase 5.

Phase 5 fills this in: settle picks against game_logs, calibration buckets, Brier score, and CLV
against the closing price the prune job keeps. Until then it records a green, not-expected run so
the 6am workflow and job_runs already have their shape.
"""

from __future__ import annotations

from ..runner import Context, JobResult


def run(ctx: Context) -> JobResult:
    open_picks = ctx.conn.execute("select count(*) from picks where status in ('pending', 'approved')").fetchone()[0]
    return JobResult(0, 0, expected=False, meta={"placeholder": "Phase 5 grades picks here", "open_picks": open_picks})
