"""Collect same-book observations inside the pre-tipoff window; never backfill."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config.settings import DB_PATH
from src.data.closing import observations, in_window
from src.data.espn_odds import fetch_odds
from src.data.espn_props import fetch_book_props
from src.data.odds_store import persist_odds_snapshot
from src.utils.slate import slate_today, utc_now, revision_path


def saved(directory, day):
    path = ROOT/'data'/directory/f'{day}.json'
    return json.loads(path.read_text()).get('picks', []) if path.exists() else []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', default=slate_today().isoformat())
    parser.add_argument('--window-minutes', type=float, default=10)
    args = parser.parse_args()
    if args.date != slate_today().isoformat():
        parser.error('Closing capture is prospective only; date must be current Pacific slate')
    if not 0 < args.window_minutes <= 60: parser.error('window-minutes must be >0 and <=60')
    report = dict(date=args.date, requested_at=utc_now(), window_minutes=args.window_minutes,
        observations=[], excluded=[], ok=False, approved=False, maximum_entry_cost=0)
    rc = 0
    try:
        games = fetch_odds(args.date); now = datetime.now(timezone.utc)
        teams = saved('team_picks', args.date); players = saved('market_picks', args.date)
        eligible_ids = {g['game_id'] for g in games if in_window(g, now, args.window_minutes)}
        with sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True) as conn:
            pairs = set(conn.execute("SELECT game_id,odds_provider FROM game_odds WHERE market_state='pick_time' AND game_date=?", (args.date,)))
        teams = [p for p in teams if (p['game_id'], p.get('odds_provider')) in pairs]
        props = fetch_book_props(args.date) if any((p.get('quote') or {}).get('event_id') in eligible_ids for p in players) else {}
        now = datetime.now(timezone.utc)
        rows, excluded = observations(games, props, teams, players, now, args.window_minutes)
        accepted = {r['event_id'] for r in rows if r['kind']=='team'}
        if accepted:
            with sqlite3.connect(str(DB_PATH)) as conn:
                persist_odds_snapshot(conn, [g for g in games if g['game_id'] in accepted], 'closing')
        report.update(ok=True, captured_at=utc_now(), observations=rows, excluded=excluded,
            in_window_events=len(eligible_ids), raw_games=games,
            limitations=['HTTP freshness verifies feed observation, not bookmaker last-change semantics'])
    except Exception as exc:
        report.update(error=f'{type(exc).__name__}: {exc}', captured_at=utc_now()); rc=1
    directory = ROOT/'data/closing_snapshots'/args.date; directory.mkdir(parents=True, exist_ok=True)
    path = revision_path(directory/(report['requested_at'].replace(':','')+'.json'))
    path.write_text(json.dumps(report, indent=2))
    print(f"Closing capture {'OK' if report['ok'] else 'FAILED'}: {len(report['observations'])} observations; {path}")
    if rc: print(report['error'])
    return rc


if __name__ == '__main__': raise SystemExit(main())
