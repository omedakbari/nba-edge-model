"""
NBA Edge Model — unified daily CLI.

After shell setup (see below), all workflows reduce to one short command:

    nba daily                         grade yesterday + today's picks + screenshot template
    nba picks                         today's full workflow: refresh + props + team picks
    nba picks --quick                 skip data refresh (reuse today's cached data)
    nba picks --team-only             team spread/ML picks only
    nba picks --props-only            player props only
    nba picks --template              print ChatGPT screenshot template for top props
    nba picks --lines-file [FILE]     load verified Sleeper lines (auto-resolves today's date if no FILE given)
    nba grade                         grade yesterday's picks
    nba grade 2026-04-20              grade a specific date
    nba refresh                       force data refresh only
    nba status                        cumulative performance reports
    nba ratings                       current team power ratings table

Shell setup (one-time — already added to ~/.zshrc):
    nba() {
        source ~/projects/nba-edge-model/venv/bin/activate
        python ~/projects/nba-edge-model/nba.py "$@"
    }
"""

import argparse
import sqlite3
import subprocess
import sys
import textwrap
import warnings
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings('ignore')

from config.settings import DB_PATH, CURRENT_SEASON
from src.utils.season import detect_phase


def refreshed_today(season: str = CURRENT_SEASON) -> bool:
    """Return True if team_stats was already updated today — avoids redundant API calls."""
    today = date.today().isoformat()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT MAX(updated_at) FROM team_stats WHERE season = ?", (season,)
        ).fetchone()
        conn.close()
        return bool(row and row[0] and row[0][:10] >= today)
    except Exception:
        return False


# ── Runner ────────────────────────────────────────────────────────────────────

def run(script_args: list[str], label: str = '') -> int:
    """Run a project script via the current Python interpreter (streaming output)."""
    if label:
        print(f"\n{'─'*64}")
        print(f"  {label}")
        print(f"{'─'*64}")
    cmd = [sys.executable] + [str(a) for a in script_args]
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def _flags(phase: str) -> list[str]:
    return {'playoffs': ['--playoffs'], 'play-in': ['--play-in'], 'regular': []}[phase]


def _label(phase: str) -> str:
    return {'playoffs': 'PLAYOFFS', 'play-in': 'PLAY-IN', 'regular': 'REGULAR SEASON'}[phase]


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_picks(args):
    season = CURRENT_SEASON
    phase  = detect_phase(season)
    flags  = _flags(phase)
    today  = date.today().isoformat()

    # --debug-pp / --debug-sleeper: dump raw line data for given players and exit.
    # Skips all other workflow steps — purely diagnostic.
    if getattr(args, 'debug_pp', None):
        run(['scripts/generate_todays_picks.py', '--debug-pp', args.debug_pp],
            'PP LINE DEBUG')
        return

    if getattr(args, 'debug_sleeper', None):
        extra = flags  # carries --playoffs / --play-in when phase is set
        run(['scripts/generate_todays_picks.py', '--debug-sleeper', args.debug_sleeper] + extra,
            'SLEEPER LINE DEBUG')
        return

    # --lines / --lines-file / --template forwarded to generate_todays_picks.py
    # --lines-file with no path auto-resolves to today's default sleeper lines file
    extra_lines      = ['--lines',      args.lines]      if getattr(args, 'lines',       None)  else []
    lines_file       = getattr(args, 'lines_file', None)
    if lines_file == '':  # flag present but no path given
        lines_file = f"data/sleeper_lines/{today}.txt"
    extra_lines_file = ['--lines-file', lines_file]      if lines_file                          else []
    extra_template   = ['--template']                    if getattr(args, 'template',    False) else []

    print(f"\n{'='*64}")
    print(f"  NBA PICKS  |  {today}  |  {season}  |  {_label(phase)}")
    print(f"  Phase auto-detected from recent game_logs")
    print(f"{'='*64}")

    # Step 1 — Refresh data
    if args.quick:
        print(f"\n  --quick: skipping data refresh")
    elif refreshed_today(season):
        print(f"\n  Data already refreshed today — skipping (run `nba refresh` to force)")
    else:
        refresh_flags = ['--playoffs'] if phase in ('playoffs', 'play-in') else []
        run(
            ['scripts/update_data.py'] + refresh_flags,
            'STEP 1 — Refreshing data'
        )

    # Step 2 — Player props
    if not args.team_only:
        run(
            ['scripts/generate_todays_picks.py'] + flags
            + extra_lines + extra_lines_file + extra_template
            + ['--season', season, '--save'],
            'STEP 2 — Player props picks'
        )

    # Step 3 — Team spread / ML picks
    if not args.props_only:
        run(
            ['scripts/generate_todays_team_picks.py'] + flags + ['--season', season, '--save'],
            'STEP 3 — Team spread / ML picks'
        )


def cmd_grade(args):
    grade_date = args.date or (date.today() - timedelta(days=1)).isoformat()
    print(f"\n{'='*64}")
    print(f"  GRADING  |  {grade_date}")
    print(f"{'='*64}")
    run(['scripts/log_pick_results.py', '--date', grade_date], 'Player props results')
    run(['scripts/log_team_results.py',  '--date', grade_date], 'Team pick results')


def cmd_daily(_args):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today     = date.today().isoformat()
    season    = CURRENT_SEASON
    phase     = detect_phase(season)
    flags     = _flags(phase)

    print(f"\n{'='*64}")
    print(f"  DAILY  |  grade {yesterday}  →  picks {today}  |  {_label(phase)}")
    print(f"{'='*64}")

    run(['scripts/log_pick_results.py', '--date', yesterday], 'Props — yesterday')
    run(['scripts/log_team_results.py',  '--date', yesterday], 'Team picks — yesterday')

    if refreshed_today(season):
        print(f"\n  Data already refreshed today — skipping")
    else:
        refresh_flags = ['--playoffs'] if phase in ('playoffs', 'play-in') else []
        run(['scripts/update_data.py'] + refresh_flags, 'Refreshing data')

    run(
        ['scripts/generate_todays_picks.py'] + flags + ['--template', '--season', season, '--save'],
        'Player props — scouting board + template'
    )
    run(
        ['scripts/generate_todays_team_picks.py'] + flags + ['--season', season, '--save'],
        'Team spread / ML picks'
    )


def cmd_refresh(_args):
    season = CURRENT_SEASON
    phase  = detect_phase(season)
    flags  = ['--playoffs'] if phase in ('playoffs', 'play-in') else []
    print(f"\n  Phase detected: {_label(phase)}")
    run(['scripts/update_data.py'] + flags, 'DATA REFRESH')


def cmd_status(_args):
    print(f"\n{'='*64}")
    print(f"  STATUS  |  {date.today().isoformat()}")
    print(f"{'='*64}")
    run(['scripts/log_team_results.py', '--report'], 'Team picks — cumulative report')
    run(['scripts/log_pick_results.py', '--report'],  'Props — calibration report')


def cmd_ratings(_args):
    season = CURRENT_SEASON
    phase  = detect_phase(season)
    today  = date.today().isoformat()

    conn = sqlite3.connect(str(DB_PATH))
    from src.models.team_ratings import TeamRatingEngine
    engine = TeamRatingEngine(
        conn, season=season, through_date=today,
        include_playoffs=(phase == 'playoffs'),
    )
    df = engine.all_ratings()
    conn.close()

    print(f"\n{'='*66}")
    print(f"  TEAM RATINGS  |  {season}  |  through {today}  |  {_label(phase)}")
    print(f"{'='*66}")
    print(f"  {'Team':<6}  {'Rating':>7}  {'Season NET':>10}  {'L10 Form':>9}  {'Games':>6}")
    print(f"  {'─'*52}")
    for _, row in df.iterrows():
        s = f"{row['season_pg']:+.1f}" if row['season_pg'] is not None else "   N/A"
        f = f"{row['form_l10']:+.1f}"   if row['form_l10'] is not None else "   N/A"
        print(f"  {row['team']:<6}  {row['rating']:+7.1f}  {s:>10}  {f:>9}  {int(row['games']):>6}")
    print(f"{'='*66}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog='nba',
        description='NBA Edge Model — daily picks CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              nba picks                               full workflow: refresh data + props + team picks
              nba picks --quick                       skip data refresh (faster, use cached data)
              nba picks --team-only                   team spread/ML picks only
              nba picks --props-only                  player props only
              nba picks --props-only --quick          scouting board + screenshot checklist
              nba picks --props-only --quick --template
                                                      also print the ChatGPT fill-in template
              nba picks --props-only --quick --lines-file data/sleeper_lines/2026-04-29.txt
                                                      load verified lines from a file
              nba grade                               grade yesterday's picks
              nba grade 2026-04-20                    grade a specific date
              nba refresh                             force data refresh
              nba status                              cumulative performance reports
              nba ratings                             current team power ratings

            Daily workflow:
              1. nba daily                                  grade yesterday + scouting board + template
              2. Send screenshots to Claude → get lines file saved to data/sleeper_lines/YYYY-MM-DD.txt
              3. nba picks --props-only --quick --lines-file  build verified card (auto-resolves today's file)
        """),
    )

    subs = parser.add_subparsers(dest='command', metavar='command')

    p = subs.add_parser('picks', help="Today's full picks workflow")
    p.add_argument('--quick',      action='store_true',
                   help='Skip data refresh (use today\'s cached data)')
    p.add_argument('--team-only',  action='store_true', dest='team_only',
                   help='Team spread/ML picks only (skip player props)')
    p.add_argument('--props-only', action='store_true', dest='props_only',
                   help='Player props only (skip team picks)')
    p.add_argument('--debug-pp',      metavar='NAMES', dest='debug_pp',
                   help='Dump raw PP projections for comma-separated player name substrings '
                        '(e.g. "julius randle,shai gilgeous") and exit')
    p.add_argument('--debug-sleeper', metavar='NAMES', dest='debug_sleeper',
                   help='Dump raw Sleeper projection records for comma-separated player names '
                        '(e.g. "jalen green,ausar thompson,ayo dosunmu") and exit')
    p.add_argument('--lines', metavar='OVERRIDES', dest='lines',
                   help='Manual Sleeper Pick\'em lines: "Jalen Green PTS 19.5, Ausar Thompson PRA 19.5". '
                        'Merges with (wins over) proxy lines. Stats: PTS REB AST PRA REB+AST FG3M')
    p.add_argument('--lines-file', metavar='FILE', dest='lines_file', nargs='?', const='',
                   help='Text file of verified Sleeper lines, one per line: "Jalen Green PTS 19.5". '
                        'Omit FILE to auto-load today\'s data/sleeper_lines/YYYY-MM-DD.txt. '
                        'Blank lines and #-comments are ignored. --lines overrides duplicates.')
    p.add_argument('--template', action='store_true',
                   help='Print fill-in-the-blank prop template to paste into ChatGPT with screenshots')

    p = subs.add_parser('grade', help='Grade picks (default: yesterday)')
    p.add_argument('date', nargs='?', default=None, metavar='YYYY-MM-DD',
                   help='Date to grade (default: yesterday)')

    subs.add_parser('daily',   help='Grade yesterday + today\'s picks + screenshot template (one command)')
    subs.add_parser('refresh', help='Force data refresh from NBA/ESPN APIs')
    subs.add_parser('status',  help='Cumulative performance reports for picks + props')
    subs.add_parser('ratings', help='Current team power ratings table')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    {
        'picks':   cmd_picks,
        'grade':   cmd_grade,
        'daily':   cmd_daily,
        'refresh': cmd_refresh,
        'status':  cmd_status,
        'ratings': cmd_ratings,
    }[args.command](args)


if __name__ == '__main__':
    main()
