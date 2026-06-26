"""
Single source of truth for season phase detection.

Usage:
    from src.utils.season import get_season_context, detect_phase

    ctx = get_season_context()
    # ctx = {'season_id': '2025-26', 'phase': 'playoffs', 'today': '2026-05-09'}

    phase = detect_phase()
    # phase = 'playoffs' | 'play-in' | 'regular' | 'offseason'
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import DB_PATH, CURRENT_SEASON


def detect_phase(season: str = CURRENT_SEASON, lookback_days: int = 14) -> str:
    """
    Detect current season phase by inspecting recent game_logs.

    Priority: Playoffs > PlayIn > Regular Season > offseason

    Returns one of: 'playoffs', 'play-in', 'regular', 'offseason'
    Falls back to 'regular' if DB is unreachable.
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            """
            SELECT season_type, COUNT(*) AS n
            FROM game_logs
            WHERE season = ? AND game_date >= ?
            GROUP BY season_type
            """,
            (season, cutoff),
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"  [season] DB unreachable, defaulting to 'regular': {e}", file=sys.stderr)
        return "regular"

    types = {r[0]: r[1] for r in rows}
    if types.get("Playoffs", 0) > 0:
        return "playoffs"
    if types.get("PlayIn", 0) > 0:
        return "play-in"
    if types.get("Regular Season", 0) > 0:
        return "regular"
    return "offseason"


def get_season_context(season: str = CURRENT_SEASON) -> dict:
    """
    Return a dict with season metadata for use in any script.

    Keys:
        season_id  — e.g. '2025-26'
        phase      — 'playoffs' | 'play-in' | 'regular' | 'offseason'
        today      — ISO date string

    Extend here (not in scattered conditionals) when new fields are needed.
    """
    return {
        "season_id": season,
        "phase": detect_phase(season),
        "today": date.today().isoformat(),
    }


def phase_flags(phase: str) -> list:
    """Translate phase string to CLI flag list for generate_todays_picks.py."""
    return {
        "playoffs": ["--playoffs"],
        "play-in": ["--play-in"],
        "regular": [],
        "offseason": [],
    }.get(phase, [])


def phase_label(phase: str) -> str:
    return {
        "playoffs": "PLAYOFFS",
        "play-in": "PLAY-IN",
        "regular": "REGULAR SEASON",
        "offseason": "OFFSEASON",
    }.get(phase, phase.upper())


# ── Playoff round helpers ──────────────────────────────────────────────────────

ROUND_LABELS = {
    1: "First Round",
    2: "Conference Semifinals",
    3: "Conference Finals",
    4: "Finals",
}


def get_playoff_round(game_id: str) -> int:
    """
    Extract playoff round digit from an NBA game_id string.

    NBA playoff game_id format (10 chars): e.g. '0042500101'
      game_id[7] = round (1=First Round, 2=CSF, 3=CF, 4=Finals)
      game_id[9] = game number within the series

    Returns 0 if the format is unexpected or game_id is too short.
    """
    try:
        r = int(game_id[7])
        return r if 1 <= r <= 4 else 0
    except (IndexError, ValueError):
        return 0


def get_series_game_number(game_id: str) -> int:
    """Extract game number within the series from a playoff game_id (position 9)."""
    try:
        return int(game_id[9])
    except (IndexError, ValueError):
        return 0
