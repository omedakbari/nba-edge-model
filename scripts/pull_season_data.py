#!/usr/bin/env python3
"""
Pull season data into the database.

Usage:
    # Full backfill (all seasons — run once, takes a few hours)
    python scripts/pull_season_data.py --backfill

    # Single season (faster, good for testing)
    python scripts/pull_season_data.py --season 2024-25

    # Quick test (just one player, verifies pipeline works)
    python scripts/pull_season_data.py --test
"""

import sys
import time
import argparse
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import CURRENT_SEASON
from src.utils.db import init_db, get_connection, table_row_count
from src.ingestion.game_logs import ingest_season, backfill_all_seasons

# Playoff seasons to pull for historical elevation analysis.
# Add new seasons here each year after playoffs conclude.
PLAYOFF_SEASONS = ["2024-25", "2023-24", "2022-23", "2021-22"]

# Play-In seasons available in nba_api (tournament started in 2020-21).
PLAYIN_SEASONS = ["2025-26", "2024-25", "2023-24", "2022-23", "2021-22", "2020-21"]


def pull_playoff_data(seasons: list = None) -> None:
    """
    Pull Playoffs game logs for historical adjustment calculations.

    This is separate from the regular daily workflow. Run once before
    the current playoffs begin, or whenever you want updated history.

    After running, generate_todays_picks.py --playoffs will automatically
    use this data to apply per-player elevation ratios.
    """
    seasons = seasons or PLAYOFF_SEASONS
    print("\n" + "=" * 60)
    print("  NBA EDGE MODEL — PLAYOFF DATA PULL")
    print("=" * 60)
    print(f"  Pulling Playoffs data for: {', '.join(seasons)}")
    print(f"  This takes ~10–30 min due to API rate limits.\n")
    init_db()
    total = {"inserted": 0, "skipped": 0, "errors": 0}
    for season in seasons:
        result = ingest_season(season, season_type="Playoffs", force_update=False)
        for key in total:
            total[key] += result.get(key, 0)
        print(f"  {season} Playoffs: {result['inserted']} rows, {result['skipped']} skipped, {result['errors']} errors")
    print("\n" + "=" * 60)
    print(f"  PLAYOFF PULL COMPLETE")
    print(f"  Total rows: {total['inserted']} inserted, {total['errors']} errors")
    print(f"\n  Next: run picks with playoffs mode:")
    print(f"  python scripts/generate_todays_picks.py --playoffs --save")
    print("=" * 60 + "\n")


def run_test():
    """
    Quick pipeline test: pull one player's current season data.
    Use this to verify everything works before running the full backfill.
    """
    from nba_api.stats.static import players as static_players
    from src.ingestion.game_logs import fetch_player_game_log

    print("\n🧪 Running pipeline test...")
    init_db()

    # Find a known active player (Nikola Jokic)
    all_players = static_players.get_players()
    jokic = [p for p in all_players if p["full_name"] == "Nikola Jokic"][0]
    print(f"  Test player: {jokic['full_name']} (ID: {jokic['id']})")

    # Fetch his game log
    df = fetch_player_game_log(jokic["id"], CURRENT_SEASON)

    if df.empty:
        print("  ❌ No data returned. Check your internet connection and try again.")
        return

    print(f"  ✅ Got {len(df)} games")
    print(f"  Latest game: {df['game_date'].iloc[0]}")
    print(f"  Season averages:")
    print(f"     PTS: {df['pts'].mean():.1f}")
    print(f"     REB: {df['reb'].mean():.1f}")
    print(f"     AST: {df['ast'].mean():.1f}")

    # Insert into database
    conn = get_connection()
    conn.execute(
        "INSERT INTO players (player_id, full_name) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (jokic["id"], jokic["full_name"]),
    )
    for _, game in df.iterrows():
        conn.execute(
            """
            INSERT INTO game_logs (
                player_id, game_id, game_date, season, season_type,
                matchup, is_home, team_abbr, opp_abbr, wl,
                min, pts, reb, ast, stl, blk, tov, pf,
                fgm, fga, fg3m, fg3a, ftm, fta,
                oreb, dreb, plus_minus
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    conn.commit()
    conn.close()

    print(f"\n  ✅ Test complete! {table_row_count('game_logs')} rows in game_logs table.")
    print(f"  Pipeline is working. You're ready for the full backfill.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NBA Data Pull Script")
    parser.add_argument("--backfill", action="store_true", help="Full backfill of all seasons")
    parser.add_argument("--season", type=str, help="Pull a specific season")
    parser.add_argument("--test", action="store_true", help="Quick test with one player")
    parser.add_argument("--playoffs", action="store_true",
                        help="Pull Playoffs game logs for historical adjustment data")
    parser.add_argument("--play-in", action="store_true",
                        help="Pull Play-In game logs (2020-21 through 2024-25, ~5-10 min)")
    parser.add_argument("--update", action="store_true", help="Update current regular season")

    args = parser.parse_args()

    if args.test:
        run_test()
    elif args.backfill:
        backfill_all_seasons()
    elif args.playoffs:
        pull_playoff_data()
    elif args.play_in:
        print("\n" + "=" * 60)
        print("  NBA EDGE MODEL — PLAY-IN DATA PULL")
        print("=" * 60)
        print(f"  Pulling PlayIn data for: {', '.join(PLAYIN_SEASONS)}")
        print(f"  This takes ~5–10 min due to API rate limits.\n")
        init_db()
        total = {"inserted": 0, "skipped": 0, "errors": 0}
        for season in PLAYIN_SEASONS:
            result = ingest_season(season, season_type="PlayIn", force_update=False)
            for key in total:
                total[key] += result.get(key, 0)
            print(f"  {season} Play-In: {result['inserted']} rows, {result['skipped']} skipped, {result['errors']} errors")
        print("\n" + "=" * 60)
        print(f"  PLAY-IN PULL COMPLETE")
        print(f"  Total rows: {total['inserted']} inserted, {total['errors']} errors")
        print(f"\n  Next: rebuild features and run picks with play-in mode:")
        print(f"  python -m src.features.pipeline")
        print(f"  python scripts/generate_todays_picks.py --play-in --save")
        print("=" * 60 + "\n")
    elif args.update:
        init_db()
        result = ingest_season(CURRENT_SEASON, force_update=True)
        print(f"\n  Update complete: {result['inserted']} new rows, {result['errors']} errors")
    elif args.season:
        init_db()
        ingest_season(args.season)
    else:
        parser.print_help()
