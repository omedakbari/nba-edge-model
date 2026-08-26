"""
Odds Store — persists market-line snapshots fetched via src/data/espn_odds.py.

Historically, generate_todays_team_picks.py fetched live odds to build picks
and then discarded them — no market line was ever kept, so nothing could be
backtested against real closing lines (see scripts/backtest_team_model.py's
docstring). This module is the persistence boundary: fetch_odds() stays a
pure fetcher, this is where a fetch becomes a durable row in game_odds.

Usage:
    from src.data.espn_odds import fetch_odds
    from src.data.odds_store import persist_odds_snapshot

    games = fetch_odds(target_date)
    persist_odds_snapshot(conn, games, market_state='pick_time')
"""

import sqlite3
from datetime import datetime, timezone


def persist_odds_snapshot(
    conn: sqlite3.Connection,
    games: list[dict],
    market_state: str,
) -> int:
    """
    Insert one game_odds row per game dict (fetch_odds()'s return shape).

    Args:
        conn: open sqlite3 Connection
        games: list of game dicts as returned by src.data.espn_odds.fetch_odds()
        market_state: 'pick_time' | 'closing'

    Returns:
        Number of rows inserted.
    """
    if not games:
        return 0

    captured_at = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            g['game_id'],
            g['game_date'],
            g['home'],
            g['away'],
            captured_at,
            market_state,
            g.get('spread'),
            g.get('over_under'),
            g.get('home_ml'),
            g.get('away_ml'),
            g.get('odds_provider'),
        )
        for g in games
    ]

    conn.executemany(
        """
        INSERT INTO game_odds
            (game_id, game_date, home_abbr, away_abbr, captured_at,
             market_state, spread_home, over_under, home_ml, away_ml, odds_provider)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def latest_snapshot(conn: sqlite3.Connection, game_id: str, market_state: str) -> dict | None:
    """
    Return the most recent game_odds row for (game_id, market_state), or None
    if no snapshot of that state has been captured for this game yet.
    """
    row = conn.execute(
        """
        SELECT game_id, game_date, home_abbr, away_abbr, captured_at,
               spread_home, over_under, home_ml, away_ml, odds_provider
        FROM game_odds
        WHERE game_id = ? AND market_state = ?
        ORDER BY captured_at DESC
        LIMIT 1
        """,
        (game_id, market_state),
    ).fetchone()
    if row is None:
        return None
    cols = ['game_id', 'game_date', 'home_abbr', 'away_abbr', 'captured_at',
            'spread_home', 'over_under', 'home_ml', 'away_ml', 'odds_provider']
    return dict(zip(cols, row))
