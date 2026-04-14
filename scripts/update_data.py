"""
Daily data refresh — run this each morning before generating picks.

Updates:
  1. game_logs  — pulls latest player box scores from NBA API
  2. team_stats — refreshes season NET_RTG, pace, ORTG, DRTG from NBA API

Usage:
    source venv/bin/activate
    python scripts/update_data.py                    # regular season update
    python scripts/update_data.py --playoffs         # also pull playoff game logs
    python scripts/update_data.py --season 2025-26   # specific season
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def run(cmd: list[str], label: str) -> bool:
    print(f"\n  [{label}] running: {' '.join(cmd)}")
    t = time.time()
    result = subprocess.run(cmd, capture_output=False, text=True)
    elapsed = time.time() - t
    ok = result.returncode == 0
    status = "done" if ok else "FAILED"
    print(f"  [{label}] {status} in {elapsed:.1f}s")
    return ok


def main():
    parser = argparse.ArgumentParser(description="Daily NBA data refresh")
    parser.add_argument('--season', default=None,
                        help='Season to update (default: current from config)')
    parser.add_argument('--playoffs', action='store_true',
                        help='Also pull playoff game logs')
    parser.add_argument('--props-only', action='store_true',
                        help='Skip team_stats update (faster, props-only workflow)')
    args = parser.parse_args()

    py = sys.executable
    print(f"\n{'='*60}")
    print(f"  NBA DAILY DATA REFRESH")
    print(f"{'='*60}")

    success = True

    # 1. Game logs — regular season
    cmd = [py, str(ROOT / 'src' / 'ingestion' / 'game_logs.py'), '--update']
    if args.season:
        cmd += ['--season', args.season]
    ok = run(cmd, 'game_logs regular season')
    success = success and ok

    # 2. Game logs — playoffs (if requested or after April 18 in a typical season)
    if args.playoffs:
        from config.settings import CURRENT_SEASON
        season = args.season or CURRENT_SEASON
        # There's no --playoffs flag in game_logs.py; it uses --season + season_type
        # We call it with a direct python command that targets playoffs
        cmd2 = [py, '-c',
            f"import sys; sys.path.insert(0,'{ROOT}'); "
            f"from src.ingestion.game_logs import ingest_season; "
            f"ingest_season('{season}', 'Playoffs'); "
            f"ingest_season('{season}', 'PlayIn')"
        ]
        ok2 = run(cmd2, 'game_logs playoffs + play-in')
        success = success and ok2

    # 3. Team stats
    if not args.props_only:
        cmd3 = [py, str(ROOT / 'src' / 'ingestion' / 'team_stats.py'), '--update']
        if args.season:
            cmd3 += ['--season', args.season]
        ok3 = run(cmd3, 'team_stats')
        success = success and ok3

    print(f"\n{'='*60}")
    if success:
        print(f"  All updates complete. Ready to generate picks.")
    else:
        print(f"  ⚠️  One or more updates failed. Check errors above.")
    print(f"{'='*60}\n")

    print(f"  Next steps:")
    print(f"    python scripts/generate_todays_picks.py --playoffs --save")
    print(f"    python scripts/generate_todays_team_picks.py --playoffs --save")
    print()


if __name__ == '__main__':
    main()
