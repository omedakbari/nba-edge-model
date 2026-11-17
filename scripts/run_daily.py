"""Normal daily entrypoint: archive live evidence before grading or refreshing.

Transport failures are explicit failures, never an empty-slate success. This
runner never trains models or promotes research artifacts.
"""
import json
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.data.live_evidence import collect
from src.utils.slate import slate_today


def main():
    day=slate_today().isoformat()
    report,path=collect(day,ROOT/'data/live_snapshots')
    print(f'Live evidence: {path}',flush=True)
    if not report['live_api_ready']:
        for key,value in report['responses'].items():
            if not value['ok']:
                print(f"{key}: {value.get('error_kind')} — {value.get('error')}",flush=True)
        print('Daily stopped before database/grade changes: live transport unavailable. '
              'Model remains diagnostic-only; approved picks 0, maximum entry cost $0.',flush=True)
        return 2
    result = subprocess.run([sys.executable,'-u',str(ROOT/'nba.py'),'daily'],cwd=ROOT).returncode
    if result:
        return result
    diag = subprocess.run([sys.executable, str(ROOT/'scripts/report_market_diagnostics.py')],cwd=ROOT).returncode
    rc = card(day) or diag
    brief(day)
    return rc


def brief(day):
    """Last: the one small file to attach to chat (yesterday's results, bets, today's picks, card)."""
    out = ROOT/'data/cards'/f'{day}_brief.txt'
    rc = subprocess.run([sys.executable, str(ROOT/'scripts/daily_brief.py'), '--date', day], cwd=ROOT,
                        stdout=subprocess.DEVNULL).returncode
    print(f'\nBrief (attach this file to chat): {out}' if rc == 0 else f'\nBrief failed (exit {rc})', flush=True)
    return rc


def card(day):
    """Final step: the research-only shortlist from the worker's PropLine/Sleeper/Kalshi data (Supabase).
    Every printed leg is logged in the bets ledger (source=card, placed=false) and graded nightly."""
    out = ROOT/'data/cards'/f'{day}.txt'
    print(f'\n── card (research-only) ──', flush=True)
    rc = subprocess.run([sys.executable, '-m', 'prop_worker.slate', '--card', '--date', day, '--out', str(out), '--log'],
                        cwd=ROOT/'worker').returncode
    print(f'Card: {out}' if rc == 0 else f'Card failed (exit {rc}); nothing written', flush=True)
    return rc


if __name__=='__main__':raise SystemExit(main())
