"""Read-only original-time freshness replay. Writes diagnostics, never picks."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.data.shadow_freshness import compare


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--capture-minutes',type=float,default=5);parser.add_argument('--future-tolerance-seconds',type=float,default=60)
    args=parser.parse_args();s=json.loads(args.snapshot.read_text())
    r=compare(s['rows'],s['scoreboard'],s['http_evidence'],s['captured_at'],capture_minutes=args.capture_minutes,
        future_tolerance_seconds=args.future_tolerance_seconds,provider_inventory=s.get('provider_inventory'))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:json.dump(r,stream,indent=2)
    print(json.dumps({k:v for k,v in r.items() if k not in ('future_anomalies','by_book_stat')},indent=2))


if __name__=='__main__':main()
