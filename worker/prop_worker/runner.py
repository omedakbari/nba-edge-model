"""Run ingest jobs with job_runs bookkeeping, red-run rules and ntfy alerts.

Every job is a function  run(ctx) -> JobResult.  The runner:
  1. inserts a job_runs row (status 'running', committed first so a crash leaves a trace),
  2. runs the job in its own transaction (rolled back on any exception),
  3. judges the run:
       error  the job raised
       red    any job-specific red reason, or, on a game day when the job was expected to load rows:
                - it loaded 0 rows
                - it finished in under RED_MIN_DURATION_S (2 s)
                - its rows per game/event fell under half its trailing average
                  (only runs with the same `baseline` count: a job sets one when what it collects
                  changes, e.g. propline's book list, so older runs don't make the new level look low)
       green  otherwise
  4. updates the job_runs row, and after all jobs sends one ntfy push listing every red/error run,
  5. exits non-zero if any run was red or error.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from . import config
from .db import connect
from .http import make_session
from .masking import redact
from .util import slate_date, utcnow


@dataclass
class JobResult:
    rows_in: int
    rows_out: int                         # rows accepted into the DB (new + confirmed-unchanged)
    expected: bool = True                 # was this run supposed to load rows?
    units: int | None = None              # games/events covered -> volume = rows_out / units
    red_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    baseline: str = ""                    # what was collected; trailing average only compares equal ones


@dataclass
class RunOutcome:
    job: str
    status: str
    duration_s: float
    rows_in: int | None
    rows_out: int | None
    is_game_day: bool | None
    expected: bool | None
    volume: float | None
    trailing_avg: float | None
    red_reasons: list[str]
    warnings: list[str]
    error: str | None = None
    meta: dict = field(default_factory=dict)


class Context:
    """What a job gets: a connection, an HTTP session, a fixed 'now', and cached reference data."""

    def __init__(self, conn, session=None, now: datetime | None = None, options: dict | None = None):
        self.conn = conn
        self.session = session or make_session()
        self.now = now or utcnow()
        self.now_et = self.now.astimezone(config.ET)
        self.slate = slate_date(self.now)
        self.options = options or {}
        self._teams = None

    @property
    def teams(self) -> dict:
        if self._teams is None:
            rows = self.conn.execute(
                """select abbr, name, espn_abbr, espn_team_id, propline_team_id from teams
                   where sport = %s""", (config.SPORT,)).fetchall()
            self._teams = {
                "abbrs": {r[0] for r in rows},
                "by_espn_abbr": {r[2]: r[0] for r in rows},
                "by_espn_id": {r[3]: r[0] for r in rows},
                "by_name": {_squash(r[1]): r[0] for r in rows},
                "by_propline_id": {r[4]: r[0] for r in rows},
            }
        return self._teams

    def games_on(self, d: date) -> int:
        return self.conn.execute(
            """select count(*) from games where sport = %s and game_date = %s
               and status not in ('postponed', 'canceled')""", (config.SPORT, d)).fetchone()[0]


def _squash(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


# ── judging a run ────────────────────────────────────────────────────────────

def trailing_average(conn, job: str, now: datetime, baseline: str = "") -> tuple[float | None, int, float | None]:
    """Average rows-per-game of recent green game-day runs at about the same ET time of day, with the
    same baseline (runs stored before baselines existed count as baseline '').

    Same time of day (±1 h) because volume legitimately grows through the day (teams file injury
    reports, books post props); a 9 am run judged against evening runs would always look low.
    """
    hour = now.astimezone(config.ET).hour
    row = conn.execute(
        """select avg(volume), count(*), percentile_cont(0.5) within group (order by duration_s) from (
               select volume, duration_s from job_runs
               where sport = %s and job = %s and status = 'green' and is_game_day and expected
                 and volume is not null and coalesce(meta->>'baseline', '') = %s
                 and least(abs(extract(hour from started_at at time zone 'America/New_York') - %s),
                           24 - abs(extract(hour from started_at at time zone 'America/New_York') - %s)) <= 1
               order by started_at desc limit %s) t""",
        (config.SPORT, job, baseline, hour, hour, config.TRAILING_WINDOW)).fetchone()
    avg, n, median_s = row
    return (float(avg) if avg is not None else None), n, (float(median_s) if median_s is not None else None)


def too_fast(duration_s: float, median_s: float | None, n: int) -> bool:
    if duration_s >= config.RED_MIN_DURATION_S:
        return False
    if config.RED_DURATION_MODE == "absolute":
        return True
    return median_s is not None and n >= config.TRAILING_MIN_RUNS and duration_s < config.RED_FAST_FRACTION * median_s


def judge(result: JobResult, duration_s: float, is_game_day: bool, trailing: float | None,
          trailing_n: int, median_s: float | None = None) -> tuple[list[str], float | None]:
    reasons = list(result.red_reasons)
    volume = (result.rows_out / result.units) if result.units else None
    if is_game_day and result.expected:
        if result.rows_out == 0:
            reasons.append("loaded 0 rows on a game day")
        if too_fast(duration_s, median_s, trailing_n):
            usual = f", usual {median_s:.2f}s" if median_s is not None else ""
            reasons.append(f"finished in {duration_s:.2f}s (< {config.RED_MIN_DURATION_S:g}s{usual}) on a game day")
        if (volume is not None and trailing is not None and trailing_n >= config.TRAILING_MIN_RUNS
                and volume < config.RED_TRAILING_FRACTION * trailing):
            reasons.append(f"loaded {volume:.1f} rows per game vs trailing average {trailing:.1f} "
                           f"(< {config.RED_TRAILING_FRACTION:.0%})")
    return reasons, volume


# ── running ──────────────────────────────────────────────────────────────────

def run_job(ctx: Context, name: str, fn: Callable[[Context], JobResult],
            clock: Callable[[], float] = time.monotonic) -> RunOutcome:
    conn = ctx.conn
    run_id = conn.execute(
        """insert into job_runs (sport, job, status, git_sha, runner, meta)
           values (%s, %s, 'running', %s, %s, %s::jsonb) returning id""",
        (config.SPORT, name, os.getenv("GITHUB_SHA"),
         "github-actions" if config.in_ci() else "local",
         json.dumps({"as_of": ctx.now.isoformat(), "slate": ctx.slate.isoformat()}))).fetchone()[0]
    conn.commit()

    t0 = clock()
    result, error = None, None
    try:
        result = fn(ctx)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — any failure becomes an 'error' run, never a crash
        conn.rollback()
        error = redact(f"{type(exc).__name__}: {exc}")          # never store/print a secret
        tb = redact(traceback.format_exc(limit=6))
    duration = clock() - t0

    is_game_day = ctx.games_on(ctx.slate) > 0
    if error:
        outcome = RunOutcome(name, "error", duration, None, None, is_game_day, None, None, None,
                             [error], [], error, {"traceback": tb[-4000:]})
    else:
        trailing, n, median_s = trailing_average(conn, name, ctx.now, result.baseline)
        if result.baseline:
            result.meta = {**result.meta, "baseline": result.baseline}
        reasons, volume = judge(result, duration, is_game_day, trailing, n, median_s)
        reasons = [redact(r) for r in reasons]
        result.warnings = [redact(w) for w in result.warnings]
        outcome = RunOutcome(name, "red" if reasons else "green", duration, result.rows_in, result.rows_out,
                             is_game_day, result.expected, volume, trailing, reasons, result.warnings,
                             None, result.meta)

    conn.execute(
        """update job_runs set finished_at = now(), duration_s = %s, status = %s, is_game_day = %s,
                  rows_in = %s, rows_out = %s, expected = %s, volume = %s, trailing_avg = %s,
                  red_reasons = %s, warnings = %s, error = %s, meta = coalesce(meta, '{}') || %s::jsonb
           where id = %s""",
        (round(duration, 3), outcome.status, outcome.is_game_day, outcome.rows_in, outcome.rows_out,
         outcome.expected, outcome.volume, outcome.trailing_avg, outcome.red_reasons or None,
         outcome.warnings or None, outcome.error, json.dumps(outcome.meta, default=str), run_id))
    conn.commit()
    return outcome


def render_summary(outcomes: list[RunOutcome], when: datetime, title: str = "Ingest") -> str:
    lines = [f"## {title} {when.astimezone(config.ET):%Y-%m-%d %H:%M} ET", "",
             "| job | status | s | rows in | rows out | per game | trailing | game day | notes |",
             "|---|---|---|---|---|---|---|---|---|"]
    for o in outcomes:
        notes = "; ".join(o.red_reasons + [f"warn: {w}" for w in o.warnings]).replace("|", "/")
        lines.append(f"| {o.job} | {o.status.upper()} | {o.duration_s:.1f} | {_n(o.rows_in)} | {_n(o.rows_out)} | "
                     f"{_f(o.volume)} | {_f(o.trailing_avg)} | {'yes' if o.is_game_day else 'no'} | {notes} |")
    return redact("\n".join(lines))


def _n(v):
    return "-" if v is None else str(v)


def _f(v):
    return "-" if v is None else f"{v:.1f}"


def alert_text(outcomes: list[RunOutcome]) -> tuple[str, str] | None:
    bad = [o for o in outcomes if o.status in ("red", "error")]
    if not bad:
        return None
    title = f"NBA worker: {len(bad)} job(s) {'/'.join(sorted({o.status.upper() for o in bad}))}"
    body = [f"{o.job} {o.status.upper()}: {'; '.join(o.red_reasons)}" for o in bad]
    run_url = _actions_run_url()
    if run_url:
        body.append(run_url)
    return title, redact("\n".join(body))


def _actions_run_url() -> str | None:
    server, repo, run = (os.getenv(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


def send_ntfy(title: str, body: str, session=None, topic: str | None = None) -> str:
    topic = config.NTFY_TOPIC if topic is None else topic
    if not topic:
        return "ntfy skipped: NTFY_TOPIC not set"
    session = session or make_session()
    try:
        r = session.post(f"{config.NTFY_SERVER}/{topic}", data=body.encode("utf-8"),
                         headers={"Title": title, "Priority": "high", "Tags": "rotating_light"})
        r.raise_for_status()
        return "ntfy sent"
    except Exception as exc:  # noqa: BLE001 — a failed alert must not hide the red run itself
        return redact(f"ntfy FAILED: {type(exc).__name__}: {exc}")   # the topic is a secret too


def run_all(jobs: list[tuple[str, Callable]], conn=None, session=None, now=None, options=None,
            alert: bool = True, notifier=send_ntfy, clock=time.monotonic,
            title: str = "Ingest") -> tuple[int, list[RunOutcome]]:
    own = conn is None
    conn = conn or connect()
    try:
        ctx = Context(conn, session=session, now=now, options=options)
        outcomes = [run_job(ctx, name, fn, clock=clock) for name, fn in jobs]
    finally:
        if own:
            conn.close()
    summary = render_summary(outcomes, now or utcnow(), title)
    print(summary)
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(summary + "\n\n")
    msg = alert_text(outcomes)
    if msg and alert:
        print(notifier(*msg))
    return (1 if msg else 0), outcomes
