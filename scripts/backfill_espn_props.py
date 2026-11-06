"""
Backfill historical sportsbook PLAYER PROP lines (over/under + prices) from
ESPN's free core API into `historical_props`.

Why: the props model has only ever been validated against synthetic L10-average
lines. This pulls real book lines (DraftKings, MGM, Caesars, ESPN BET, ...) for
every game so the model — and pick'em lines — can be measured against the market.

Source (no API key):
  .../events/{eid}/competitions/{eid}/odds                    → providers per game
  .../events/{eid}/competitions/{eid}/odds/{pid}/propBets     → player props
  .../seasons/{yr}/athletes/{aid}                              → athlete name

Each ESPN item is one SIDE of one prop. We store the `open` line/price, which is
guaranteed pregame — `current` values are often frozen in-game prices (e.g. a
-3000 line captured after tip-off) and must not be used for backtesting.

Usage:
    python scripts/backfill_espn_props.py                       # 2023-24 → 2025-26
    python scripts/backfill_espn_props.py --start 2026-01-01 --end 2026-01-31

Resumable: events already present in historical_props are skipped.
Requires historical_odds (scripts/backfill_espn_odds.py) for the event list.
"""

import argparse
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH
from src.data.espn_props import (CORE, ODDS_URL, PROPS_URL, MARKETS, WORKERS,
                                 Blocked, _get, fetch_event)

SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_props (
    espn_event_id   TEXT NOT NULL,
    game_date       TEXT NOT NULL,
    provider        TEXT NOT NULL,
    espn_athlete_id TEXT NOT NULL,
    stat_key        TEXT NOT NULL,
    side            TEXT NOT NULL,         -- 'over' | 'under'
    line_open       REAL,
    price_open      REAL,                  -- decimal odds
    line_current    REAL,                  -- may be in-game for past games; do not backtest on it
    price_current   REAL,
    last_updated    TEXT,
    PRIMARY KEY (espn_event_id, provider, espn_athlete_id, stat_key, side)
);
CREATE INDEX IF NOT EXISTS idx_hist_props_date ON historical_props(game_date);
CREATE TABLE IF NOT EXISTS espn_athletes (
    espn_athlete_id TEXT PRIMARY KEY,
    full_name       TEXT
);
CREATE TABLE IF NOT EXISTS historical_props_done (
    espn_event_id TEXT PRIMARY KEY
);
"""


def resolve_athletes(conn, session):
    todo = [r[0] for r in conn.execute(
        "SELECT DISTINCT espn_athlete_id FROM historical_props "
        "WHERE espn_athlete_id NOT IN (SELECT espn_athlete_id FROM espn_athletes)")]
    print(f"  resolving {len(todo)} athlete names...")

    def one(aid):
        try:
            j = _get(session, f"{CORE}/athletes/{aid}")
        except Blocked:
            return aid, None
        return aid, (j or {}).get('fullName')
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        res = [r for r in pool.map(one, todo) if r[1]]
    conn.executemany("INSERT OR REPLACE INTO espn_athletes VALUES (?, ?)", res)
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2023-10-01')
    ap.add_argument('--end', default='2026-06-30')
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    events = conn.execute(
        "SELECT DISTINCT espn_event_id, game_date FROM historical_odds "
        "WHERE game_date BETWEEN ? AND ? "
        "AND espn_event_id NOT IN (SELECT espn_event_id FROM historical_props_done) "
        "ORDER BY game_date", (args.start, args.end)).fetchall()
    print(f"  {len(events)} events to backfill")

    session = requests.Session()
    sql = "INSERT OR REPLACE INTO historical_props VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    n, failed, t0 = 0, 0, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, (rows, (eid, _)) in enumerate(
                zip(pool.map(lambda e: fetch_event(session, *e), events), events), 1):
            if rows is None:
                failed += 1
                if failed >= 20 and failed == i:
                    print("  ESPN is blocking every request — stopping; rerun later to resume.")
                    break
                continue
            conn.executemany(sql, rows)
            conn.execute("INSERT OR IGNORE INTO historical_props_done VALUES (?)", (eid,))
            conn.commit()
            n += len(rows)
            if i % 100 == 0 or i == len(events):
                print(f"  {i}/{len(events)} events  |  {n:,} prop sides  |  {failed} blocked  |  {time.time() - t0:.0f}s",
                      flush=True)
    resolve_athletes(conn, session)
    conn.close()


if __name__ == '__main__':
    main()
