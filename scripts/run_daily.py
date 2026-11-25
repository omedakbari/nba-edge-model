"""Normal daily entrypoint: archive live evidence before grading or refreshing.

Transport failures are explicit failures, never an empty-slate success. This
runner never trains models or promotes research artifacts.

--dry-run: no ledger or Supabase writes, today's card / brief / picks files are not overwritten,
grading is computed but not written, the data refresh is skipped. The card goes to data/runs/dry-<stamp>/.
Append-only archives (live / quote snapshots, fair-board and stack revisions) are still written.

Exit codes: 0 = every step OK; 3 = every step ran but at least one WARNED (got input, produced nothing;
or a worker job is red); anything else = a step failed. Each step prints a status line
"  OK|WARN|FAILED (exit n) — <label>[ :: reasons]" (src/utils/step_status.py) that the dashboard parses.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.data.live_evidence import collect
from src.utils.slate import slate_today
from src.utils.step_status import WARN_ENV, WARN_EXIT, read_warnings, status_line


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
        epilog='Archives live evidence, runs `nba.py daily`, market diagnostics, the card, and the brief. '
               'Use nba.py subcommands for anything else.')
    ap.add_argument('--dry-run', action='store_true',
                    help='no ledger/Supabase writes; do not overwrite today\'s card, brief or picks files')
    args = ap.parse_args(argv or [])
    day=slate_today().isoformat()
    if args.dry_run:
        print('DRY RUN: no ledger/Supabase writes; today\'s card, brief and picks files are left untouched.',
              flush=True)
    report,path=collect(day,ROOT/'data/live_snapshots')
    print(f'Live evidence: {path}',flush=True)
    if not report['live_api_ready']:
        for key,value in report['responses'].items():
            if not value['ok']:
                print(f"{key}: {value.get('error_kind')} — {value.get('error')}",flush=True)
        print('Daily stopped before database/grade changes: live transport unavailable. '
              'Model remains diagnostic-only; approved picks 0, maximum entry cost $0.',flush=True)
        return 2
    fd, warn_path = tempfile.mkstemp(prefix='nba_run_', suffix='.jsonl')
    os.close(fd)
    os.environ[WARN_ENV] = warn_path            # every child step appends its warnings here
    if args.dry_run:
        os.environ['NBA_DRY_RUN'] = '1'
    try:
        result = subprocess.run([sys.executable,'-u',str(ROOT/'nba.py'),'daily'],cwd=ROOT).returncode
        if result:
            return result
        if args.dry_run:
            print('\nDry run: market diagnostics report skipped', flush=True)
            diag = 0
        else:
            diag = subprocess.run([sys.executable, str(ROOT/'scripts/report_market_diagnostics.py')],cwd=ROOT).returncode
        rc = card(day, warn_path, dry_run=args.dry_run) or diag
        if args.dry_run:
            print('\nDry run: brief skipped (it would overwrite today\'s brief)', flush=True)
        else:
            brief(day)
        health_warnings = worker_health()
        return finish(rc, read_warnings(warn_path) + health_warnings)
    finally:
        os.environ.pop(WARN_ENV, None)
        os.environ.pop('NBA_DRY_RUN', None)
        try:
            os.unlink(warn_path)
        except OSError:
            pass


def finish(rc, warnings):
    """Final status line; exit WARN_EXIT when everything ran but something warned."""
    if rc:
        print(f'\nRUN STATUS: FAILED (exit {rc})', flush=True)
        return rc
    if warnings:
        print(f'\nRUN STATUS: WARN — {len(warnings)} warning(s):', flush=True)
        for w in warnings:
            print(f"  - {w.get('step') or 'step'}: {w['reason']}", flush=True)
        return WARN_EXIT
    print('\nRUN STATUS: OK', flush=True)
    return 0


def worker_health():
    """Cloud worker jobs (Supabase job_runs, read-only). Red jobs become run warnings."""
    from src.data.worker_health import job_status, status_lines
    print('\n── worker health ──', flush=True)
    health = job_status()
    for line in status_lines(health):
        print(line, flush=True)
    if not health['ok']:
        return [{'step': 'Worker health', 'reason': f"Supabase job_runs unreadable: {health['error']}"}]
    return [{'step': 'Worker health', 'reason': f"{j['job']} RED: " + '; '.join(j['reasons'] or [j['status']])}
            for j in health['jobs'] if j['level'] == 'red']


def brief(day):
    """Last: the one small file to attach to chat (yesterday's results, bets, today's picks, card)."""
    out = ROOT/'data/cards'/f'{day}_brief.txt'
    rc = subprocess.run([sys.executable, str(ROOT/'scripts/daily_brief.py'), '--date', day], cwd=ROOT,
                        stdout=subprocess.DEVNULL).returncode
    print(f'\nBrief (attach this file to chat): {out}' if rc == 0 else f'\nBrief failed (exit {rc})', flush=True)
    print(status_line('Brief', rc), flush=True)
    return rc


def card(day, warn_path=None, dry_run=False):
    """Final step: the research-only shortlist from the worker's PropLine/Sleeper/Kalshi data (Supabase).
    Every printed leg is logged in the bets ledger (source=card, placed=false) and graded nightly.
    dry_run: no --log (no ledger write) and the card goes to data/runs/dry-<stamp>/, not data/cards/."""
    if dry_run:
        from datetime import datetime, timezone
        out = ROOT/'data/runs'/f"dry-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"/f'{day}.txt'
        log = []
    else:
        out = ROOT/'data/cards'/f'{day}.txt'
        log = ['--log']
    before = len(read_warnings(warn_path))
    print(f'\n── card (research-only) ──', flush=True)
    rc = subprocess.run([sys.executable, '-m', 'prop_worker.slate', '--card', '--date', day, '--out', str(out)] + log,
                        cwd=ROOT/'worker').returncode
    print(f'Card: {out}' if rc == 0 else f'Card failed (exit {rc}); nothing written', flush=True)
    print(status_line('Prop card', rc, read_warnings(warn_path)[before:]), flush=True)
    return rc


if __name__=='__main__':raise SystemExit(main(sys.argv[1:]))
