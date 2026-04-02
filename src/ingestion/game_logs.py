"""
Player Game Log Ingestion Pipeline.

Pulls game logs from NBA.com via nba_api and stores them in SQLite.
Supports both full backfill and incremental daily updates.

Usage:
    # Full backfill (run once to seed your database)
    python -m src.ingestion.game_logs --backfill

    # Update current season only
    python -m src.ingestion.game_logs --update

    # Specific season
    python -m src.ingestion.game_logs --season 2024-25
"""

import time
import argparse
import sqlite3
from datetime import datetime

import pandas as pd
from nba_api.stats.endpoints import playergamelog, commonallplayers
from nba_api.stats.static import players as static_players
from tqdm import tqdm

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import (
    NBA_API_DELAY,
    BACKFILL_SEASONS,
    CURRENT_SEASON,
)
from src.utils.db import get_connection, init_db


# ─────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────

def get_active_players(season: str) -> pd.DataFrame:
    """
    Get all players who were active in a given season.
    Uses the CommonAllPlayers endpoint which gives us every player
    who appeared in at least one game.
    """
    print(f"  Fetching player list for {season}...")
    time.sleep(NBA_API_DELAY)

    result = commonallplayers.CommonAllPlayers(
        is_only_current_season=0,
        league_id="00",
        season=season,
    )
    df = result.get_data_frames()[0]

    # Filter to players who were active during this season.
    # FROM_YEAR/TO_YEAR store the start-year of first/last season (e.g. "2024" for 2024-25).
    # We keep anyone whose active range spans the target season start year.
    season_start_year = int(season.split("-")[0])
    df = df[
        (df["FROM_YEAR"].astype(int) <= season_start_year) &
        (df["TO_YEAR"].astype(int) >= season_start_year)
    ]

    return df[["PERSON_ID", "DISPLAY_FIRST_LAST"]].rename(
        columns={"PERSON_ID": "player_id", "DISPLAY_FIRST_LAST": "full_name"}
    )


def parse_matchup(matchup: str) -> tuple[str, str, bool]:
    """
    Parse NBA matchup string into (team, opponent, is_home).

    Examples:
        'LAL vs. DEN' → ('LAL', 'DEN', True)   # home game
        'LAL @ DEN'   → ('LAL', 'DEN', False)   # away game
    """
    if " vs. " in matchup:
        parts = matchup.split(" vs. ")
        return parts[0].strip(), parts[1].strip(), True
    elif " @ " in matchup:
        parts = matchup.split(" @ ")
        return parts[0].strip(), parts[1].strip(), False
    else:
        raise ValueError(f"Cannot parse matchup: {matchup}")


def parse_minutes(min_str) -> float:
    """
    Convert minutes string to decimal.
    NBA API returns minutes as 'MM:SS' or as integer.
    """
    if pd.isna(min_str) or min_str is None:
        return 0.0
    if isinstance(min_str, (int, float)):
        return float(min_str)
    if ":" in str(min_str):
        parts = str(min_str).split(":")
        return int(parts[0]) + int(parts[1]) / 60
    return float(min_str)


def fetch_player_game_log(player_id: int, season: str, season_type: str = "Regular Season") -> pd.DataFrame:
    """
    Pull one player's game log for a season from NBA.com.

    Returns a cleaned DataFrame ready for database insertion,
    or an empty DataFrame if the API returns no data.
    """
    time.sleep(NBA_API_DELAY)  # Rate limit — respect the API

    try:
        response = playergamelog.PlayerGameLog(
            player_id=player_id,
            season=season,
            season_type_all_star=season_type,
        )
        df = response.get_data_frames()[0]
    except Exception as e:
        # Some players have IDs but no game logs (e.g., two-way guys who never played)
        print(f"    ⚠ Error fetching player {player_id}: {e}")
        return pd.DataFrame()

    if df.empty:
        return df

    # Parse and clean the data
    records = []
    for _, row in df.iterrows():
        team_abbr, opp_abbr, is_home = parse_matchup(row["MATCHUP"])
        records.append({
            "player_id": player_id,
            "game_id": row["Game_ID"],
            "game_date": datetime.strptime(row["GAME_DATE"], "%b %d, %Y").strftime("%Y-%m-%d"),
            "season": season,
            "season_type": season_type,
            "matchup": row["MATCHUP"],
            "is_home": int(is_home),
            "team_abbr": team_abbr,
            "opp_abbr": opp_abbr,
            "wl": row.get("WL"),
            "min": parse_minutes(row.get("MIN")),
            "pts": int(row.get("PTS", 0)),
            "reb": int(row.get("REB", 0)),
            "ast": int(row.get("AST", 0)),
            "stl": int(row.get("STL", 0)),
            "blk": int(row.get("BLK", 0)),
            "tov": int(row.get("TOV", 0)),
            "pf": int(row.get("PF", 0)),
            "fgm": int(row.get("FGM", 0)),
            "fga": int(row.get("FGA", 0)),
            "fg3m": int(row.get("FG3M", 0)),
            "fg3a": int(row.get("FG3A", 0)),
            "ftm": int(row.get("FTM", 0)),
            "fta": int(row.get("FTA", 0)),
            "oreb": int(row.get("OREB", 0)),
            "dreb": int(row.get("DREB", 0)),
            "plus_minus": float(row.get("PLUS_MINUS", 0)),
        })

    return pd.DataFrame(records)


# ─────────────────────────────────────────────
# Core Pipeline Functions
# ─────────────────────────────────────────────

def ingest_season(season: str, season_type: str = "Regular Season") -> dict:
    """
    Pull all player game logs for a season and insert into the database.

    This is the main workhorse function. It:
    1. Gets the list of active players for the season
    2. Checks what we've already ingested (incremental)
    3. Pulls game logs for each player
    4. Inserts into SQLite with upsert logic
    5. Logs progress to the ingestion_log table

    Returns a summary dict with counts.
    """
    conn = get_connection()
    summary = {"inserted": 0, "skipped": 0, "errors": 0, "players_processed": 0}

    # Log this pipeline run
    conn.execute(
        "INSERT INTO pipeline_runs (run_type, season) VALUES (?, ?)",
        ("backfill" if season != CURRENT_SEASON else "daily_update", season),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    # Get player list
    active = get_active_players(season)
    print(f"\n{'='*60}")
    print(f"  Season: {season} ({season_type})")
    print(f"  Players found: {len(active)}")
    print(f"{'='*60}\n")

    # Save players to players table
    for _, player in active.iterrows():
        conn.execute(
            """
            INSERT INTO players (player_id, full_name, is_active)
            VALUES (?, ?, 1)
            ON CONFLICT(player_id) DO UPDATE SET
                full_name = excluded.full_name,
                updated_at = datetime('now')
            """,
            (int(player["player_id"]), player["full_name"]),
        )
    conn.commit()

    # Check what we've already ingested
    existing = {}
    cursor = conn.execute(
        "SELECT player_id, games_pulled FROM ingestion_log WHERE season = ? AND season_type = ?",
        (season, season_type),
    )
    for row in cursor.fetchall():
        existing[row[0]] = row[1]

    # Process each player
    for _, player in tqdm(active.iterrows(), total=len(active), desc=f"  {season}"):
        pid = int(player["player_id"])

        # Skip if we already have this player's data
        # (For daily updates, you'd check last_game_date instead)
        if pid in existing and existing[pid] > 0:
            summary["skipped"] += 1
            continue

        # Fetch game log
        df = fetch_player_game_log(pid, season, season_type)

        if df.empty:
            summary["skipped"] += 1
            continue

        # Insert into database
        try:
            for _, game in df.iterrows():
                conn.execute(
                    """
                    INSERT INTO game_logs (
                        player_id, game_id, game_date, season, season_type,
                        matchup, is_home, team_abbr, opp_abbr, wl,
                        min, pts, reb, ast, stl, blk, tov, pf,
                        fgm, fga, fg3m, fg3a, ftm, fta,
                        oreb, dreb, plus_minus
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?
                    )
                    ON CONFLICT(player_id, game_id) DO NOTHING
                    """,
                    (
                        game["player_id"], game["game_id"], game["game_date"],
                        game["season"], game["season_type"],
                        game["matchup"], game["is_home"], game["team_abbr"],
                        game["opp_abbr"], game["wl"],
                        game["min"], game["pts"], game["reb"], game["ast"],
                        game["stl"], game["blk"], game["tov"], game["pf"],
                        game["fgm"], game["fga"], game["fg3m"], game["fg3a"],
                        game["ftm"], game["fta"],
                        game["oreb"], game["dreb"], game["plus_minus"],
                    ),
                )

            # Log ingestion
            last_date = df["game_date"].max()
            conn.execute(
                """
                INSERT INTO ingestion_log (player_id, season, season_type, games_pulled, last_game_date)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(player_id, season, season_type) DO UPDATE SET
                    games_pulled = excluded.games_pulled,
                    last_game_date = excluded.last_game_date,
                    pulled_at = datetime('now')
                """,
                (pid, season, season_type, len(df), last_date),
            )
            conn.commit()

            summary["inserted"] += len(df)
            summary["players_processed"] += 1

        except sqlite3.Error as e:
            print(f"    ❌ DB error for player {pid}: {e}")
            summary["errors"] += 1
            conn.rollback()

    # Finalize pipeline run log
    conn.execute(
        """
        UPDATE pipeline_runs
        SET finished_at = datetime('now'),
            rows_inserted = ?,
            rows_skipped = ?,
            errors = ?,
            status = 'completed'
        WHERE id = ?
        """,
        (summary["inserted"], summary["skipped"], summary["errors"], run_id),
    )
    conn.commit()
    conn.close()

    return summary


def backfill_all_seasons():
    """
    Backfill all configured seasons. Run this once to seed the database.
    Takes a while (expect 30-60+ min per season due to rate limiting).
    """
    print("\n" + "=" * 60)
    print("  NBA EDGE MODEL — FULL BACKFILL")
    print("=" * 60)

    init_db()
    total = {"inserted": 0, "skipped": 0, "errors": 0}

    for season in BACKFILL_SEASONS:
        result = ingest_season(season)
        for key in total:
            total[key] += result.get(key, 0)

        print(f"\n  {season} complete: {result['inserted']} rows inserted, "
              f"{result['skipped']} skipped, {result['errors']} errors")

    print("\n" + "=" * 60)
    print(f"  BACKFILL COMPLETE")
    print(f"  Total rows inserted: {total['inserted']}")
    print(f"  Total skipped: {total['skipped']}")
    print(f"  Total errors: {total['errors']}")
    print("=" * 60 + "\n")


def update_current_season():
    """
    Quick update for the current season only.
    Use this for daily updates.
    """
    print("\n  Updating current season...")
    init_db()
    result = ingest_season(CURRENT_SEASON)
    print(f"\n  Update complete: {result['inserted']} new rows, "
          f"{result['errors']} errors")


# ─────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NBA Game Log Ingestion Pipeline")
    parser.add_argument("--backfill", action="store_true", help="Backfill all configured seasons")
    parser.add_argument("--update", action="store_true", help="Update current season only")
    parser.add_argument("--season", type=str, help="Ingest a specific season (e.g., 2024-25)")

    args = parser.parse_args()

    if args.backfill:
        backfill_all_seasons()
    elif args.update:
        update_current_season()
    elif args.season:
        init_db()
        result = ingest_season(args.season)
        print(f"\n  Done: {result['inserted']} rows, {result['errors']} errors")
    else:
        print("Usage:")
        print("  python -m src.ingestion.game_logs --backfill    # Seed all seasons")
        print("  python -m src.ingestion.game_logs --update      # Update current season")
        print("  python -m src.ingestion.game_logs --season 2024-25  # Specific season")
