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
    return card(day) or diag


def card(day):
    """Final step: the research-only shortlist from the worker's PropLine/Sleeper/Kalshi data (Supabase)."""
    out = ROOT/'data/cards'/f'{day}.txt'
    print(f'\n── card (research-only) ──', flush=True)
    rc = subprocess.run([sys.executable, '-m', 'prop_worker.slate', '--card', '--date', day, '--out', str(out)],
                        cwd=ROOT/'worker').returncode
    print(f'Card: {out}' if rc == 0 else f'Card failed (exit {rc}); nothing written', flush=True)
    return rc


if __name__=='__main__':raise SystemExit(main())
