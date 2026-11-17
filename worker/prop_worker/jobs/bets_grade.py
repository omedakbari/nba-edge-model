"""bets: nightly grading of the bet ledger from game_logs (rules in prop_worker/bets.py).

Run:  python -m prop_worker.jobs.bets_grade [--no-alert]

Grades every ungraded bet up to yesterday's slate (6am ET: ctx.slate is today), so a missed night is
caught up the next one. Never expected to load rows (no bets is a normal night), so it can't go red
on volume; it errors only if the grading itself fails.
"""

from __future__ import annotations

from datetime import timedelta

from ..bets import grade_pending
from ..runner import Context, JobResult
from . import main_for


def run(ctx: Context) -> JobResult:
    counts = grade_pending(ctx.conn, ctx.slate - timedelta(days=1), now=ctx.now)
    graded = sum(v for k, v in counts.items() if k != "pending")
    return JobResult(rows_in=graded + counts.get("pending", 0), rows_out=graded, expected=False, units=None,
                     meta={"through": (ctx.slate - timedelta(days=1)).isoformat(), **counts})


main_for(__name__, "bets", run, __doc__)
