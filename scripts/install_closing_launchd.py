"""Install/update a user LaunchAgent for prospective closing captures, every minute."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
ROOT=Path(__file__).resolve().parents[1]
LABEL='com.omedakbari.nba-closing'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activate',action='store_true')
    args=parser.parse_args()
    logs=ROOT/'logs'; logs.mkdir(exist_ok=True)
    plist=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'; plist.parent.mkdir(parents=True,exist_ok=True)
    payload=dict(Label=LABEL,ProgramArguments=[str(ROOT/'venv/bin/python'),str(ROOT/'scripts/capture_closing_odds.py'),'--window-minutes','10'],
        WorkingDirectory=str(ROOT),StartInterval=60,RunAtLoad=True,
        StandardOutPath=str(logs/'closing_capture.log'),StandardErrorPath=str(logs/'closing_capture.error.log'))
    with plist.open('wb') as stream: plistlib.dump(payload,stream)
    print(plist)
    if args.activate:
        domain=f'gui/{os.getuid()}'
        subprocess.run(['launchctl','bootout',f'{domain}/{LABEL}'],capture_output=True)
        return subprocess.run(['launchctl','bootstrap',domain,str(plist)]).returncode
    return 0


if __name__=='__main__': raise SystemExit(main())
