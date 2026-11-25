"""Catch-up grading (AUDIT_2026-11-24.md item 6; DECISIONS.md D17).

The graders used to grade only "yesterday", so a day the daily run was skipped (11-21..11-23) left the day
before it (11-20) ungraded forever. Now each grader, with --catch-up, grades every date that has a picks
file, is before today, comes AFTER the last successful grade (or --since), and isn't graded yet.
Re-running is a no-op: graded dates are skipped and the writers dedupe (pick_results upsert,
results_log.csv by game_id). A date whose games aren't final stays ungraded and is retried next run.

Lag: game days (dates with box scores in data/nba_edge.db) after the last successful grade, up to
yesterday. More than MAX_LAG_GAME_DAYS -> WARN naming each day and why (no picks file = daily not run).
Holes before the last success can't be graded (no picks were saved) and don't count against the lag.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'data'
MAX_LAG_GAME_DAYS = 1
DATE_FILE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.json$')


def pick_file_dates(folder: Path) -> list[str]:
    """Dates that have a base picks file (revisions live beside it as <date>.revision-*.json)."""
    if not folder.exists():
        return []
    return sorted(m.group(1) for f in folder.iterdir() if (m := DATE_FILE.match(f.name)))


def team_graded_dates(results_log: Path = DATA / 'team_picks' / 'results_log.csv') -> set[str]:
    if not results_log.exists():
        return set()
    return set(pd.read_csv(results_log, dtype=str)['game_date'].dropna())


def market_graded_dates(db: Path = DATA / 'nba_edge.db', folder: Path = DATA / 'market_picks') -> set[str]:
    """pick_results rows, plus dates whose newest picks file holds 0 picks (nothing to grade = done)."""
    done = set()
    if db.exists():
        with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as c:
            try:
                done |= {r[0] for r in c.execute('select distinct pick_date from pick_results')}
            except sqlite3.OperationalError:
                pass
    from src.utils.slate import latest_revision
    for d in pick_file_dates(folder):
        try:
            if not json.loads(latest_revision(folder / f'{d}.json').read_text()).get('picks'):
                done.add(d)
        except (OSError, ValueError):
            pass
    return done


def to_grade(files: list[str], graded: set[str], today: str, since: str | None = None) -> list[str]:
    """Ungraded picks-file dates before today and after the last success (or on/after --since)."""
    last = max((d for d in graded if d < today), default=None)
    floor = since if since else last
    return [d for d in files if d < today and d not in graded and (floor is None or
                                                                    (d >= floor if since else d > floor))]


def game_days(db: Path = DATA / 'nba_edge.db', after: str | None = None, until: str | None = None) -> list[str]:
    if not db.exists():
        return []
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as c:
        return [r[0] for r in c.execute(
            'select distinct game_date from game_logs where game_date > ? and game_date <= ? order by 1',
            (after or '0000', until or '9999'))]


def lag_warning(kind: str, files: list[str], graded: set[str], today: str, db: Path = DATA / 'nba_edge.db'
                ) -> str | None:
    """None, or 'team grading lags 3 game day(s): 2026-11-21 (no picks file: daily not run), ...'."""
    last = max((d for d in graded if d < today), default=None)
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    days = game_days(db, last, yesterday)
    if len(days) <= MAX_LAG_GAME_DAYS:
        return None
    why = [f"{d} ({'not graded yet' if d in files else 'no picks file: daily not run'})" for d in days]
    return f"{kind} grading lags {len(days)} game day(s) since {last or 'never'}: " + ', '.join(why)
