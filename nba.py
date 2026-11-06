"""
NBA Edge Model — unified daily CLI.

After shell setup (see below), all workflows reduce to one short command:

    nba daily                         grade yesterday + market-price props + team picks
    nba picks                         today's full workflow: refresh + props + team picks
    nba picks --quick                 skip data refresh (reuse today's cached data)
    nba picks --team-only             team spread/ML picks only
    nba picks --props-only            player props only
    nba picks --pickem --template     optional app-line screenshot workflow
    nba picks --lines-file [FILE]     load verified Sleeper lines (auto-resolves today's date if no FILE given)
    nba picks --playoffs              force playoffs mode (auto-detected, but can override)
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
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings('ignore')

from src.utils.slate import slate_today
from config.settings import DB_PATH, CURRENT_SEASON, PROCESSED_DIR
from src.utils.season import detect_phase


def refreshed_today(season: str = CURRENT_SEASON) -> bool:
    """Skip refresh only when today's team stats also have rebuilt features."""
    today = slate_today().isoformat()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT MAX(updated_at) FROM team_stats WHERE season = ?", (season,)
        ).fetchone()
        conn.close()
        if not (row and row[0]):
            return False
        from src.utils.slate import PACIFIC
        updated = datetime.fromisoformat(row[0].replace('Z', '+00:00'))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=PACIFIC)  # legacy team_stats used localtime
        if updated.astimezone(PACIFIC).date().isoformat() != today:
            return False
        features = PROCESSED_DIR / 'features_appearance_v2.parquet'
        # A failed feature build leaves today's team_stats beside yesterday's
        # parquet. That partial refresh must be retried rather than skipped.
        return features.exists() and features.stat().st_mtime >= updated.timestamp()
    except Exception:
        return False


STEP_RESULTS = []


# ── Runner ────────────────────────────────────────────────────────────────────

def run(script_args: list[str], label: str = '') -> int:
    """Run a project script via the current Python interpreter (streaming output)."""
    if label:
        print(f"\n{'─'*64}")
        print(f"  {label}")
        print(f"{'─'*64}")
    cmd = [sys.executable] + [str(a) for a in script_args]
    try:
        rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
    except OSError as exc:
        STEP_RESULTS.append((label or str(script_args[0]), 1))
        print(f'  Could not start step: {exc}')
        raise SystemExit(1) from exc
    STEP_RESULTS.append((label or str(script_args[0]), rc))
    if rc:
        raise SystemExit(rc)
    return rc


def _flags(phase: str) -> list[str]:
    from src.utils.season import phase_flags
    return phase_flags(phase)


def _label(phase: str) -> str:
    from src.utils.season import phase_label
    return phase_label(phase)


def _print_refresh_failed() -> None:
    print(f"\n  {'='*62}")
    print(f"  ❌  DATA REFRESH FAILED — picks generation aborted.")
    print(f"      Game logs could not be updated; picks would be based on")
    print(f"      stale data. Fix the issue and retry:")
    print(f"        nba refresh       # retry data refresh")
    print(f"        nba picks --quick # generate picks from cached data (with risk)")
    print(f"  {'='*62}\n")


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_picks(args):
    season = CURRENT_SEASON
    phase  = detect_phase(season)
    # Explicit --playoffs / --play-in overrides auto-detection
    if getattr(args, 'playoffs', False):
        phase = 'playoffs'
    elif getattr(args, 'play_in', False):
        phase = 'play-in'
    flags  = _flags(phase)
    today  = slate_today().isoformat()

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
        rc = run(
            ['scripts/update_data.py'] + refresh_flags,
            'STEP 1 — Refreshing data'
        )
        if rc != 0:
            _print_refresh_failed()
            return rc

    # Explicit app lines/template opt into the legacy pick'em card workflow.
    pickem = (getattr(args, 'pickem', False) or bool(extra_lines) or
              bool(extra_lines_file) or bool(extra_template))
    # Step 2 — Player props at actual sportsbook prices by default.
    if not args.team_only:
        if pickem:
            run(['scripts/generate_todays_picks.py'] + flags
                + extra_lines + extra_lines_file + extra_template
                + ['--season', season, '--save'], 'STEP 2 — Verified pick\'em cards')
        else:
            run(['scripts/find_bets.py'] + flags + ['--season', season, '--save'],
                'STEP 2 — Player props at market prices')

    # Step 3 — Team spread / ML picks
    if not args.props_only:
        run(
            ['scripts/generate_todays_team_picks.py'] + flags + ['--season', season, '--save'],
            'STEP 3 — Team spread / ML picks'
        )

    if not pickem:
        run(['scripts/build_prop_stacks.py'], 'STEP 4 — Player + game prop stack screen')


def cmd_grade(args):
    grade_date = args.date or (slate_today() - timedelta(days=1)).isoformat()
    print(f"\n{'='*64}")
    print(f"  GRADING  |  {grade_date}")
    print(f"{'='*64}")
    props_cmd = ['scripts/log_pick_results.py', '--date', grade_date]
    if not getattr(args, 'pickem', False):
        props_cmd.append('--market')
    if args.overwrite:
        props_cmd.append('--overwrite')
    run(props_cmd, 'Player props results')
    run(['scripts/log_team_results.py', '--date', grade_date], 'Team pick results')


def cmd_daily(_args):
    yesterday = (slate_today() - timedelta(days=1)).isoformat()
    today     = slate_today().isoformat()
    season    = CURRENT_SEASON
    phase     = detect_phase(season)
    flags     = _flags(phase)

    print(f"\n{'='*64}")
    print(f"  DAILY  |  grade {yesterday}  →  picks {today}  |  {_label(phase)}")
    print(f"{'='*64}")

    run(['scripts/log_pick_results.py', '--date', yesterday, '--market'], 'Market props — yesterday')
    run(['scripts/log_team_results.py',  '--date', yesterday], 'Team picks — yesterday')

    if refreshed_today(season):
        print(f"\n  Data already refreshed today — skipping")
    else:
        refresh_flags = ['--playoffs'] if phase in ('playoffs', 'play-in') else []
        rc = run(['scripts/update_data.py'] + refresh_flags, 'Refreshing data')
        if rc != 0:
            _print_refresh_failed()
            return rc

    run(
        ['scripts/find_bets.py'] + flags + ['--season', season, '--save'],
        'Player props — market prices and EV'
    )
    run(
        ['scripts/generate_todays_team_picks.py'] + flags + ['--season', season, '--save'],
        'Team spread / ML picks'
    )

    run(['scripts/build_prop_stacks.py'], 'Player + game prop stack screen')


def cmd_bets(args):
    """+EV finder: fair probability per prop, priced at Kalshi / sportsbook / anything you type."""
    cmd = ['scripts/find_bets.py', '--bankroll', str(args.bankroll), '--min-ev', str(args.min_ev)]
    if not args.price and not args.entry:
        cmd.append('--save')
    if args.reuse:
        cmd.append('--reuse-board')
    for s in args.price or []:
        cmd += ['--price', s]
    for s in args.entry or []:
        cmd += ['--entry', s]
    if args.payout:
        cmd += ['--payout', str(args.payout)]
    run(cmd, '+EV BET FINDER')


def cmd_refresh(_args):
    season = CURRENT_SEASON
    phase  = detect_phase(season)
    flags  = ['--playoffs'] if phase in ('playoffs', 'play-in') else []
    print(f"\n  Phase detected: {_label(phase)}")
    run(['scripts/update_data.py'] + flags, 'DATA REFRESH')


def cmd_status(_args):
    print(f"\n{'='*64}")
    print(f"  STATUS  |  {slate_today().isoformat()}")
    print(f"{'='*64}")
    run(['scripts/log_team_results.py', '--report'], 'Team picks — cumulative report')
    run(['scripts/log_pick_results.py', '--report'],  'Props — calibration report')


def cmd_ratings(_args):
    season = CURRENT_SEASON
    phase  = detect_phase(season)
    today  = slate_today().isoformat()

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
              nba picks --props-only --quick          scan player props at market prices
              nba picks --pickem --template           optional verified pick'em card workflow
              nba grade                               grade yesterday's picks
              nba grade --date 2026-04-20             grade a specific date
              nba refresh                             force data refresh
              nba status                              cumulative performance reports
              nba ratings                             current team power ratings
              nba bets                                +EV bets at Kalshi + sportsbook prices
              nba bets --reuse --price "Jalen Brunson PTS over 26.5 x1.85"
                                                      price-check any app (-115, 1.87, x1.85, 52c)
              nba bets --reuse --entry "A PTS over 20.5 x1.8; B REB under 8.5 x1.9"
                                                      price a whole pick'em entry

            Daily workflow:
              1. nba daily                              refresh data + market props + team picks
              2. nba bets --reuse --price "Name PTS over 24.5 -110"
                                                        compare a price from any app
              3. nba grade                              grade saved market candidates after games
        """),
    )

    subs = parser.add_subparsers(dest='command', metavar='command')

    p = subs.add_parser('picks', help="Today's full picks workflow")
    p.add_argument('--quick',      action='store_true',
                   help='Skip data refresh (use today\'s cached data)')
    p.add_argument('--pickem', action='store_true',
                   help='Opt into the app-line/card workflow (default: sportsbook prices)')
    p.add_argument('--team-only', '--teams-only', action='store_true', dest='team_only',
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
    p.add_argument('--playoffs', action='store_true',
                   help='Force playoffs mode (overrides auto-detection)')
    p.add_argument('--play-in', action='store_true', dest='play_in',
                   help='Force Play-In mode (overrides auto-detection)')

    p = subs.add_parser('grade', help='Grade picks (default: yesterday)')
    p.add_argument('--date', default=None, metavar='YYYY-MM-DD',
                   help='Date to grade (default: yesterday)')
    p.add_argument('--overwrite', action='store_true',
                   help='Re-grade picks even if already graded')
    p.add_argument('--pickem', action='store_true', help='Grade optional pick\'em cards instead of market props')

    p = subs.add_parser('bets', help='+EV bets at real prices (Kalshi, sportsbooks, any app)')
    p.add_argument('--bankroll', type=float, default=100.0)
    p.add_argument('--min-ev', type=float, default=0.04, dest='min_ev')
    p.add_argument('--reuse', action='store_true', help="reuse today's saved fair board (fast)")
    p.add_argument('--price', action='append', help='"Name STAT over|under LINE PRICE"')
    p.add_argument('--entry', action='append', help='"leg; leg" with per-leg prices, or use --payout')
    p.add_argument('--payout', type=float, help='total entry multiplier')

    subs.add_parser('daily',   help='Grade yesterday + today\'s market props and team picks')
    subs.add_parser('refresh', help='Force data refresh from NBA/ESPN APIs')
    subs.add_parser('status',  help='Cumulative performance reports for picks + props')
    subs.add_parser('ratings', help='Current team power ratings table')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    STEP_RESULTS.clear()
    try:
        result = {
        'picks':   cmd_picks,
        'grade':   cmd_grade,
        'daily':   cmd_daily,
        'refresh': cmd_refresh,
        'status':  cmd_status,
        'ratings': cmd_ratings,
        'bets':    cmd_bets,
        }[args.command](args)
        return int(result or 0)
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(f'  Command failed: {type(exc).__name__}: {exc}')
        return 1
    finally:
        print('\n  STEP STATUS SUMMARY')
        for label, code in STEP_RESULTS:
            print(f"  {'OK' if code == 0 else 'FAILED'} (exit {code}) — {label}")
        if any(code for _, code in STEP_RESULTS):
            print('  Workflow incomplete; remaining steps were not run.')


if __name__ == '__main__':
    raise SystemExit(main())
