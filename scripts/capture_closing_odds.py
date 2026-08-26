"""
Capture Closing Odds.

Thin script — fetches today's (or a given date's) market lines and persists
them to game_odds with market_state='closing'. Meant to run close to tip-off,
separate from the morning picks run (which snapshots market_state='pick_time'
via generate_todays_team_picks.py).

Having both snapshots for a game is what makes CLV (closing-line value) —
how the line moved between when we picked and when it closed — computable in
scripts/log_team_results.py. A game with no closing snapshot yet just grades
without CLV; nothing else depends on this having run.

Usage:
    # Today
    python scripts/capture_closing_odds.py

    # Specific date
    python scripts/capture_closing_odds.py --date 2026-04-20
"""

import argparse
import sqlite3
import sys
import warnings
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

from config.settings import DB_PATH
from src.data.espn_odds import fetch_odds
from src.data.odds_store import persist_odds_snapshot


def main():
    parser = argparse.ArgumentParser(description="Capture closing market lines for CLV tracking")
    parser.add_argument('--date', default=date.today().isoformat(),
                        help='Target date YYYY-MM-DD (default: today)')
    args = parser.parse_args()

    print(f"  Fetching closing lines for {args.date}...")
    games = fetch_odds(args.date)
    if not games:
        print(f"  ⚠️  No games or odds found for {args.date}.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    n = persist_odds_snapshot(conn, games, market_state='closing')
    conn.close()

    print(f"  Snapshotted {n} game line(s) to game_odds (market_state=closing)")


if __name__ == '__main__':
    main()
