"""
Database connection and initialization helpers.

Usage:
    from src.utils.db import get_connection, init_db

    # Initialize database (run once, or on first use)
    init_db()

    # Use a connection
    with get_connection() as conn:
        df = pd.read_sql("SELECT * FROM game_logs LIMIT 10", conn)
"""

import sqlite3
from pathlib import Path

# Import config — handle both direct run and module import
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import DB_PATH, SCHEMA_PATH


def get_connection() -> sqlite3.Connection:
    """
    Get a SQLite connection with useful defaults.

    Returns a connection with:
    - WAL mode (allows concurrent reads while writing)
    - Foreign keys enforced
    - Row factory for dict-like access
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """
    Initialize the database by running the schema SQL file.
    Safe to call multiple times — all tables use CREATE IF NOT EXISTS.
    """
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found: {SCHEMA_PATH}")

    with get_connection() as conn:
        schema_sql = SCHEMA_PATH.read_text()
        conn.executescript(schema_sql)
        print(f"✅ Database initialized at {DB_PATH}")


def table_row_count(table_name: str) -> int:
    """Quick check: how many rows in a table?"""
    with get_connection() as conn:
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table_name}")
        return cursor.fetchone()[0]


def get_ingestion_status(season: str) -> dict:
    """
    Check what we've already pulled for a given season.
    Returns dict of {player_id: last_game_date}.
    """
    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT player_id, last_game_date
            FROM ingestion_log
            WHERE season = ?
            """,
            (season,),
        )
        return {row[0]: row[1] for row in cursor.fetchall()}


if __name__ == "__main__":
    # Quick test
    init_db()
    print(f"Game logs: {table_row_count('game_logs')} rows")
    print(f"Players: {table_row_count('players')} rows")
