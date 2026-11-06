"""
Team Stats Ingestion — pulls regular-season DRTG/ORTG/pace from nba_api.

Populates the team_stats table so the playoff backtest can classify opponents
as top/mid/bot defensive tier (currently all 'mid' due to empty table).

Data source: LeagueDashTeamStats Advanced endpoint (per-game).
Fetches: DEF_RATING, OFF_RATING, NET_RATING, PACE, DREB_PCT, AST_PCT

Seasons fetched: 2021-22 through 2025-26 (matches playoff backtest window).
2025-26 not yet in playoff backtest but included for daily-picks future use.

Usage:
    # Backfill all seasons (run once)
    python -m src.ingestion.team_stats --backfill

    # Update current season only
    python -m src.ingestion.team_stats --update

    # Specific season
    python -m src.ingestion.team_stats --season 2024-25

    # Dry-run: print rows, don't write
    python -m src.ingestion.team_stats --backfill --dry-run
"""

import argparse
import sqlite3
import time
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from nba_api.stats.endpoints import leaguedashteamstats
from nba_api.stats.static import teams as static_teams

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.data.errors import SourceError
from config.settings import DB_PATH, NBA_API_DELAY, BACKFILL_SEASONS, CURRENT_SEASON


# Seasons that could have playoff opponents we need to classify.
# We only fetch regular-season stats (used to rank opponent DRTG at playoff time).
TARGET_SEASONS = ['2021-22', '2022-23', '2023-24', '2024-25', '2025-26', '2026-27']


def _team_abbr_map() -> dict[int, str]:
    """Build team_id → abbreviation map from nba_api static data."""
    return {t['id']: t['abbreviation'] for t in static_teams.get_teams()}


def fetch_team_stats(season: str) -> pd.DataFrame:
    """
    Fetch regular-season advanced team stats for a given season.

    Returns DataFrame with columns:
        team_abbr, season, pace, off_rtg, def_rtg, net_rtg, reb_rate, ast_rate
    """
    time.sleep(NBA_API_DELAY)
    raw = leaguedashteamstats.LeagueDashTeamStats(
        season=season,
        season_type_all_star='Regular Season',
        measure_type_detailed_defense='Advanced',
        per_mode_detailed='PerGame',
    ).get_data_frames()[0]

    if raw.empty:
        raise SourceError(f"Empty team stats for {season}", source="NBA team stats", kind="empty")

    abbr_map = _team_abbr_map()
    raw['team_abbr'] = raw['TEAM_ID'].map(abbr_map)
    missing = raw['team_abbr'].isna().sum()
    if missing:
        print(f"    WARNING: {missing} rows have no abbreviation mapping — skipping")
        raw = raw.dropna(subset=['team_abbr'])

    out = pd.DataFrame({
        'team_abbr': raw['team_abbr'],
        'season':    season,
        'pace':      raw['PACE'].round(3),
        'off_rtg':   raw['OFF_RATING'].round(3),
        'def_rtg':   raw['DEF_RATING'].round(3),
        'net_rtg':   raw['NET_RATING'].round(3),
        'reb_rate':  raw['DREB_PCT'].round(4),
        'ast_rate':  raw['AST_PCT'].round(4),
    })
    validate_team_stats(out, season)
    return out


def validate_team_stats(df, season):
    """Require all active NBA franchises (30 today), even on early-season dates.

    Before all franchises have played, a full-league aggregate is explicitly
    incomplete; this collector never silently accepts a partial league.
    """
    expected = set(_team_abbr_map().values())
    required = {"team_abbr", "season", "pace", "off_rtg", "def_rtg", "net_rtg", "reb_rate", "ast_rate"}
    if df.empty or not required.issubset(df.columns):
        raise SourceError("Empty or missing team-stat columns", source="NBA team stats", kind="incomplete")
    actual = set(df.team_abbr)
    if actual != expected or len(df) != len(expected) or not df.season.eq(season).all():
        raise SourceError(f"Incomplete {season}: expected {len(expected)} teams, got {len(df)}; missing {sorted(expected-actual)}", source="NBA team stats", kind="incomplete")
    numeric = df[list(required - {"team_abbr", "season"})].to_numpy(dtype=float)
    if not np.isfinite(numeric).all() or not df[["pace", "off_rtg", "def_rtg"]].gt(0).all().all():
        raise SourceError("Invalid numeric team stats", source="NBA team stats", kind="incomplete")


def upsert_team_stats(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """
    Insert or replace rows into team_stats.
    Returns number of rows written.
    """
    if df.empty:
        return 0
    rows = df.to_dict('records')
    conn.executemany("""
        INSERT INTO team_stats (team_abbr, season, pace, off_rtg, def_rtg, net_rtg, reb_rate, ast_rate, updated_at)
        VALUES (:team_abbr, :season, :pace, :off_rtg, :def_rtg, :net_rtg, :reb_rate, :ast_rate, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(team_abbr, season) DO UPDATE SET
            pace      = excluded.pace,
            off_rtg   = excluded.off_rtg,
            def_rtg   = excluded.def_rtg,
            net_rtg   = excluded.net_rtg,
            reb_rate  = excluded.reb_rate,
            ast_rate  = excluded.ast_rate,
            updated_at = excluded.updated_at
    """, rows)
    conn.commit()
    return len(rows)


def run(seasons: list[str], dry_run: bool = False) -> None:
    conn = sqlite3.connect(str(DB_PATH)) if not dry_run else None

    total_written = 0
    try:
        for season in seasons:
            print(f"  Fetching {season}...", end='', flush=True)
            try:
                df = fetch_team_stats(season)
                validate_team_stats(df, season)
            except Exception as exc:
                raise SourceError(str(exc), source=f'NBA team stats {season}', kind='incomplete_or_failed') from exc
            if dry_run:
                print(f" {len(df)} rows (dry-run, not written)")
            else:
                total_written += upsert_team_stats(conn, df)
                print(f" {len(df)} rows")
    finally:
        if conn:
            conn.close()
    print(f"\n  Total rows written: {total_written}")


def verify(seasons: list[str]) -> None:
    """Print a quick sanity check: top-5 and bottom-5 DRTG teams per season."""
    conn = sqlite3.connect(str(DB_PATH))
    for season in seasons:
        df = pd.read_sql(
            "SELECT team_abbr, def_rtg, off_rtg, net_rtg, pace FROM team_stats WHERE season=? ORDER BY def_rtg",
            conn, params=(season,)
        )
        if df.empty:
            print(f"  {season}: no data")
            continue
        print(f"\n  {season} — {len(df)} teams  DRTG range [{df.def_rtg.min():.1f}–{df.def_rtg.max():.1f}]")
        print(f"  Best defense (low DRTG):")
        print(df.head(5).to_string(index=False, justify='left'))
        print(f"  Worst defense (high DRTG):")
        print(df.tail(5).to_string(index=False, justify='left'))
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Ingest regular-season team DRTG/ORTG/pace into team_stats table")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--backfill', action='store_true', help="Fetch all TARGET_SEASONS")
    group.add_argument('--update',   action='store_true', help="Fetch current season only")
    group.add_argument('--season',   type=str,            help="Fetch a specific season e.g. 2024-25")
    parser.add_argument('--dry-run', action='store_true', help="Print rows, don't write to DB")
    parser.add_argument('--verify',  action='store_true', help="After writing, print top/bottom DRTG teams")
    args = parser.parse_args()

    if args.backfill:
        seasons = TARGET_SEASONS
    elif args.update:
        seasons = [CURRENT_SEASON]
    else:
        seasons = [args.season]

    print(f"\n{'='*60}")
    print(f"  Team Stats Ingestion — {', '.join(seasons)}")
    print(f"  DB: {DB_PATH}")
    print(f"  Dry-run: {args.dry_run}")
    print(f"{'='*60}\n")

    run(seasons, dry_run=args.dry_run)

    if args.verify and not args.dry_run:
        print(f"\n{'='*60}")
        print(f"  VERIFICATION")
        print(f"{'='*60}")
        verify(seasons)

    print(f"\n{'='*60}\n")


if __name__ == '__main__':
    main()
