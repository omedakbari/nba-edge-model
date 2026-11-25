"""Worker (cloud ingest) health, read-only from Supabase job_runs. Never writes.

job_status() -> {"ok": bool, "error": str | None, "jobs": [ {job, status, started_at, age_h, rows_in,
rows_out, reasons: [str], level: green|yellow|red}, ... ]}

level: the job's own status (green/red; 'warn'/'yellow' -> yellow) with red_reasons / warnings attached
verbatim, so e.g. validate's "AJ Green (MIL): minutes ESPN None vs PropLine 0.15" names the player.
A job whose newest run is older than STALE_H[job] is yellow ("no run for N h").
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOKBACK_DAYS = 3
# Expected cadence: market/injury jobs every ~15 min on game days, nightly jobs once a day.
STALE_H = {'injuries': 2, 'kalshi': 2, 'propline': 2, 'schedule': 6, 'rosters': 30, 'boxscores': 30,
           'validate': 30, 'grade': 30, 'bets': 30, 'prune': 30}
LEVEL = {'green': 'green', 'ok': 'green', 'red': 'red', 'failed': 'red', 'error': 'red',
         'yellow': 'yellow', 'warn': 'yellow', 'warning': 'yellow'}


def _reasons(red_reasons, warnings, error) -> list[str]:
    out = []
    for v in (red_reasons, warnings):
        if isinstance(v, (list, tuple)):
            out += [str(x) for x in v if x]
        elif v:
            out.append(str(v))
    if error:
        out.append(str(error))
    return out


def summarize(rows, now: datetime | None = None) -> list[dict]:
    """rows: (job, status, started_at, rows_in, rows_out, red_reasons, warnings, error), newest per job."""
    now = now or datetime.now(timezone.utc)
    jobs = []
    for job, status, started, n_in, n_out, red, warns, err in rows:
        age_h = (now - started).total_seconds() / 3600 if started else None
        level = LEVEL.get((status or '').lower(), 'yellow')
        reasons = _reasons(red, warns, err)
        if level == 'green' and reasons:
            level = 'yellow'                                   # green run that still logged warnings
        if age_h is not None and age_h > STALE_H.get(job, 30) and level == 'green':
            level = 'yellow'
            reasons.append(f'no run for {age_h:.0f} h')
        jobs.append({'job': job, 'status': status, 'started_at': started.isoformat() if started else None,
                     'age_h': round(age_h, 1) if age_h is not None else None, 'rows_in': n_in,
                     'rows_out': n_out, 'reasons': reasons, 'level': level})
    return sorted(jobs, key=lambda j: ({'red': 0, 'yellow': 1, 'green': 2}[j['level']], j['job']))


def job_status(conn=None, now: datetime | None = None) -> dict:
    """Newest job_runs row per job in the last LOOKBACK_DAYS. Read-only; never raises."""
    try:
        if conn is None:
            sys.path.insert(0, str(ROOT / 'worker'))
            from prop_worker.db import connect
            with connect() as c:
                return job_status(c, now)
        conn.read_only = True
        rows = conn.execute(
            """select distinct on (job) job, status, started_at, rows_in, rows_out, red_reasons, warnings, error
               from job_runs where sport = 'nba' and started_at > now() - make_interval(days => %s)
               order by job, started_at desc""", (LOOKBACK_DAYS,)).fetchall()
        return {'ok': True, 'error': None, 'jobs': summarize(rows, now)}
    except Exception as exc:  # noqa: BLE001 — health must degrade to "unknown", never crash the page
        msg = f'{type(exc).__name__}'
        try:
            sys.path.insert(0, str(ROOT / 'worker'))
            from prop_worker.masking import redact
            msg = redact(f'{type(exc).__name__}: {exc}')
        except Exception:  # noqa: BLE001
            pass
        return {'ok': False, 'error': msg, 'jobs': []}


def status_lines(health: dict) -> list[str]:
    """Run-log text: one line per non-green job, plus a one-line all-clear."""
    if not health['ok']:
        return [f"Worker jobs: UNKNOWN (Supabase unreadable: {health['error']})"]
    bad = [j for j in health['jobs'] if j['level'] != 'green']
    lines = [f"Worker jobs: {len(health['jobs']) - len(bad)}/{len(health['jobs'])} green"]
    for j in bad:
        lines.append(f"  {j['level'].upper()} {j['job']} ({j['started_at']}): " + ('; '.join(j['reasons']) or j['status']))
    return lines
