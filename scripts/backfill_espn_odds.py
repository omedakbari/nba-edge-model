"""
Backfill historical NBA game odds (spread / total / moneyline, open + close)
from ESPN's free public APIs into the `historical_odds` table.

Why: every backtest so far measured the models against synthetic lines
(L10 averages for props, own-predicted spread for teams). Profitability can
only be judged against real market lines, and ESPN's core odds API serves
them for every game back to 2019-20.

Sources (no API key):
  scoreboard  site.api.espn.com/.../nba/scoreboard?dates=YYYYMMDD   → event ids
  odds        sports.core.api.espn.com/.../events/{id}/competitions/{id}/odds

One row per (espn_event_id, provider). Spread/total fields are the provider's
latest (≈ closing for finished games); open_/close_ columns are filled when
ESPN exposes them (mostly 2023-24 onward).

Usage:
    python scripts/backfill_espn_odds.py                      # all BACKFILL_SEASONS
    python scripts/backfill_espn_odds.py --seasons 2024-25
    python scripts/backfill_espn_odds.py --start 2025-10-01 --end 2026-06-30

Resumable: dates already present in historical_odds are skipped.
"""

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH, BACKFILL_SEASONS
from src.data.espn_odds import _normalize

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ODDS_URL = ("https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/"
            "events/{eid}/competitions/{eid}/odds")
TIMEOUT = 30
WORKERS = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS historical_odds (
    espn_event_id  TEXT NOT NULL,
    game_date      TEXT NOT NULL,          -- local (US) game date, matches game_logs.game_date
    home_abbr      TEXT NOT NULL,
    away_abbr      TEXT NOT NULL,
    home_score     INTEGER,
    away_score     INTEGER,
    season_type    INTEGER,                -- ESPN: 1=pre, 2=regular, 3=post, 5=play-in
    provider       TEXT NOT NULL,
    provider_priority INTEGER,
    spread_home    REAL,                   -- home perspective, neg = home favored
    total          REAL,
    home_ml        INTEGER,
    away_ml        INTEGER,
    open_spread_home REAL,
    close_spread_home REAL,
    open_total     REAL,
    close_total    REAL,
    open_home_ml   INTEGER,
    close_home_ml  INTEGER,
    open_away_ml   INTEGER,
    close_away_ml  INTEGER,
    details        TEXT,
    raw_json       TEXT,
    fetched_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (espn_event_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_hist_odds_date ON historical_odds(game_date);
"""


def _get(session: requests.Session, url: str, params=None, retries: int = 3):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def _num(x):
    if x is None:
        return None
    try:
        return float(str(x).replace('+', '').replace('PK', '0').replace('EVEN', '100'))
    except ValueError:
        return None


def _american(node: dict | None, key: str):
    """Pull node[key].american (e.g. '+6.5', '-120') as a float."""
    if not node or key not in node:
        return None
    v = node[key]
    if isinstance(v, dict):
        return _num(v.get('american') or v.get('alternateDisplayValue'))
    return _num(v)


def _total_from(node: dict | None):
    """open/close total lives under node['total'] or node['overUnder'] depending on era."""
    if not node:
        return None
    for k in ('total', 'overUnder'):
        if k in node:
            v = node[k]
            if isinstance(v, dict):
                a = v.get('american') or v.get('alternateDisplayValue') or ''
                return _num(str(a).lstrip('ou'))
            return _num(v)
    return None


def parse_item(item: dict) -> dict:
    home, away = item.get('homeTeamOdds', {}) or {}, item.get('awayTeamOdds', {}) or {}
    row = {
        'provider': (item.get('provider') or {}).get('name', 'unknown'),
        'provider_priority': (item.get('provider') or {}).get('priority'),
        'spread_home': _num(item.get('spread')),
        'total': _num(item.get('overUnder')),
        'home_ml': _num(home.get('moneyLine')),
        'away_ml': _num(away.get('moneyLine')),
        'details': item.get('details'),
        'open_spread_home': _american(home.get('open'), 'pointSpread'),
        'close_spread_home': _american(home.get('close'), 'pointSpread'),
        'open_home_ml': _american(home.get('open'), 'moneyLine'),
        'close_home_ml': _american(home.get('close'), 'moneyLine'),
        'open_away_ml': _american(away.get('open'), 'moneyLine'),
        'close_away_ml': _american(away.get('close'), 'moneyLine'),
        'open_total': _total_from(item.get('open')),
        'close_total': _total_from(item.get('close')),
    }
    return row


def fetch_date(session: requests.Session, ymd: str) -> list[dict]:
    sb = _get(session, SCOREBOARD, params={'dates': ymd.replace('-', '')})
    if not sb:
        return []
    out = []
    for ev in sb.get('events', []):
        comp = (ev.get('competitions') or [{}])[0]
        teams = {c.get('homeAway'): c for c in comp.get('competitors', [])}
        if 'home' not in teams or 'away' not in teams:
            continue
        base = {
            'espn_event_id': ev['id'],
            'game_date': ymd,
            'home_abbr': _normalize(teams['home']['team']['abbreviation']),
            'away_abbr': _normalize(teams['away']['team']['abbreviation']),
            'home_score': int(teams['home'].get('score') or 0) or None,
            'away_score': int(teams['away'].get('score') or 0) or None,
            'season_type': (ev.get('season') or {}).get('type'),
        }
        odds = _get(session, ODDS_URL.format(eid=ev['id']))
        for item in (odds or {}).get('items', []):
            # in-game lines (e.g. 'ESPN Bet - Live Odds') are not pregame markets
            if 'live' in ((item.get('provider') or {}).get('name') or '').lower():
                continue
            row = {**base, **parse_item(item), 'raw_json': json.dumps(item)}
            out.append(row)
    return out


def dates_to_fetch(conn: sqlite3.Connection, args) -> list[str]:
    if args.start and args.end:
        q = "SELECT DISTINCT game_date FROM game_logs WHERE game_date BETWEEN ? AND ? ORDER BY 1"
        dates = [r[0] for r in conn.execute(q, (args.start, args.end))]
    else:
        seasons = args.seasons or BACKFILL_SEASONS
        q = f"SELECT DISTINCT game_date FROM game_logs WHERE season IN ({','.join('?' * len(seasons))}) ORDER BY 1"
        dates = [r[0] for r in conn.execute(q, seasons)]
    done = {r[0] for r in conn.execute("SELECT DISTINCT game_date FROM historical_odds")}
    return [d for d in dates if d not in done]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seasons', nargs='*')
    ap.add_argument('--start')
    ap.add_argument('--end')
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    dates = dates_to_fetch(conn, args)
    print(f"  {len(dates)} game dates to backfill")

    cols = ['espn_event_id', 'game_date', 'home_abbr', 'away_abbr', 'home_score', 'away_score',
            'season_type', 'provider', 'provider_priority', 'spread_home', 'total', 'home_ml',
            'away_ml', 'open_spread_home', 'close_spread_home', 'open_total', 'close_total',
            'open_home_ml', 'close_home_ml', 'open_away_ml', 'close_away_ml', 'details', 'raw_json']
    sql = (f"INSERT OR REPLACE INTO historical_odds ({','.join(cols)}) "
           f"VALUES ({','.join('?' * len(cols))})")

    session = requests.Session()
    n_rows, t0 = 0, time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for i, rows in enumerate(pool.map(lambda d: fetch_date(session, d), dates), 1):
            if rows:
                conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])
                conn.commit()
                n_rows += len(rows)
            if i % 25 == 0 or i == len(dates):
                print(f"  {i}/{len(dates)} dates  |  {n_rows} odds rows  |  {time.time() - t0:.0f}s",
                      flush=True)
    conn.close()


if __name__ == '__main__':
    main()
