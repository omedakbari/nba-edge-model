"""ET time windows for GitHub's UTC-only, DST-unaware cron.

The workflows fire on a superset of UTC times that covers both EST and EDT; the first thing each
run does is check the real ET clock and exit quietly (code 0, no job_runs rows) when outside.
"""

from __future__ import annotations

from datetime import datetime, time

from . import config


def parse_window(spec: str) -> tuple[time, time]:
    """'09:00-01:00' -> (09:00, 01:00). The end may be past midnight."""
    start, end = spec.split("-")
    return time.fromisoformat(start), time.fromisoformat(end)


def in_window(now: datetime, spec: str) -> bool:
    start, end = parse_window(spec)
    t = now.astimezone(config.ET).time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end          # wraps midnight: 09:00-01:00


def at_hour(now: datetime, hour: int) -> bool:
    return now.astimezone(config.ET).hour == hour
