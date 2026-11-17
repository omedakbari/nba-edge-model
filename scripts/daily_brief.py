"""One small file to attach to chat: data/cards/YYYY-MM-DD_brief.txt (under 8 KB).

Sections:
  1. yesterday's graded team picks  (data/team_picks/results_log.csv, written by log_team_results.py)
  2. bets ledger, last 7 days       (prop_worker.bets report --days 7: overall, platform, placed, calibration)
  3. today's team PICK SUMMARY      (data/team_picks/<date>.json, newest revision, with INJURY_BLIND flags)
  4. today's card                   (data/cards/<date>.txt from `prop_worker.slate --card`)
Read-only apart from writing the brief. A missing section says so instead of failing the brief.

Run: ./venv/bin/python scripts/daily_brief.py [--date 2026-11-17]
"""
import argparse
import csv
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.utils.slate import latest_revision, slate_today

MAX_BYTES = 8000
PICK_TIERS = ('STRONG', 'LEAN')
REPORT_SECTIONS = ('by platform', 'by placed', 'by p bucket')


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _line(v):
    x = _f(v)
    return '' if x is None else f'{x:+g}'


def yesterday_results(day: date, log: Path) -> list[str]:
    y = (day - timedelta(days=1)).isoformat()
    out = [f'== YESTERDAY {y}: graded team picks ==']
    if not log.exists():
        return out + ['  (no results_log.csv yet)']
    rows = {}
    with log.open() as f:
        for r in csv.DictReader(f):
            if r['game_date'] == y:
                rows[r['game_id']] = r                     # last grading of a game wins
    lines, rec = [], {'sp': [0, 0], 'ou': [0, 0]}
    for r in rows.values():
        parts = []
        if r['spread_result'] not in ('NO_PICK', 'NO_LINE', 'PENDING', ''):
            ms = _f(r['market_spread'])
            line = ms if r['spread_pick_side'] == 'home' else (-ms if ms is not None else None)
            parts.append(f"{r['tier']} {r['spread_pick_team']} {_line(line)} {r['spread_result']} (home by {r['actual_margin']})")
            if r['spread_result'] in ('WIN', 'LOSS'):
                rec['sp'][r['spread_result'] == 'LOSS'] += 1
        if r.get('ou_result') not in ('NO_PICK', 'NO_LINE', 'PENDING', '', None):
            parts.append(f"{r['ou_tier']} {r['ou_direction']} {r['over_under']} {r['ou_result']} ({r['actual_total']})")
            if r['ou_result'] in ('WIN', 'LOSS'):
                rec['ou'][r['ou_result'] == 'LOSS'] += 1
        if parts:
            lines.append(f"  {r['away']}@{r['home']:<4} " + ' | '.join(parts))
    if not rows:
        return out + [f'  (no graded games for {y}: the daily run grades yesterday; check log_team_results)']
    out += lines or ['  (no spread/total picks)']
    out.append(f"  record: spread {rec['sp'][0]}-{rec['sp'][1]}, total {rec['ou'][0]}-{rec['ou'][1]} "
               f"({len(rows)} games graded)")
    return out


def bets_summary(run=subprocess.run) -> list[str]:
    out = ['== BETS, last 7 days (research-only) ==']
    p = run([sys.executable, '-m', 'prop_worker.bets', 'report', '--days', '7'], cwd=ROOT / 'worker',
            capture_output=True, text=True)
    if p.returncode:
        return out + [f'  (bets report failed: {(p.stderr or "").strip()[:200]})']
    keep, section = [], None
    for line in p.stdout.splitlines():
        if line.startswith(' by '):
            section = line.strip()
            if section in REPORT_SECTIONS:
                keep.append(line)
        elif line.startswith((' Sleeper cards', ' calibration')):
            section = None
            keep.append(line)
        elif line.startswith('  ') and (section is None or section in REPORT_SECTIONS):
            keep.append(line)                              # column header, ALL, and rows of kept sections
        elif line.startswith('BETS REPORT'):
            keep.append(line)
    return out + keep


def todays_picks(day: date, picks_dir: Path) -> list[str]:
    out = [f'== TODAY {day}: team PICK SUMMARY ==']
    path = latest_revision(picks_dir / f'{day.isoformat()}.json')
    if not path.exists():
        return out + ['  (no team picks file for today)']
    picks = json.loads(path.read_text()).get('picks', [])
    n_pick = n_blind = 0
    for a in picks:
        sp = ''
        if a.get('tier') in PICK_TIERS and a.get('spread_pick_team') and a.get('market_spread') is not None:
            ms = a['market_spread']
            sp = f"{a['tier']} {a['spread_pick_team']} {_line(ms if a['spread_pick_side'] == 'home' else -ms)}"
            n_pick += 1
        ou = ''
        if a.get('ou_tier') in PICK_TIERS and a.get('ou_direction'):
            ou = f"{a['ou_tier']} {a['ou_direction']} {a.get('over_under')}"
            n_pick += 1
        ib = a.get('injury_blind')
        blind = ''
        if ib:
            n_blind += 1
            was = ib.get('before_guard', {})
            downgraded = [k for k, t in (('spread', was.get('tier')), ('total', was.get('ou_tier'))) if t in PICK_TIERS]
            blind = 'INJURY_BLIND ' + '; '.join(
                f"{t}: {', '.join(p['name'] for p in f['players'][:3])} ({f['reason'].split(';')[0]})"
                for t, f in ib['teams'].items())
            if downgraded:
                blind += f" -> {'/'.join(downgraded)} now MONITOR"
        picks_txt = ' | '.join(x for x in (sp, ou) if x) or a.get('tier', 'NONE')
        out.append(f"  {a['away']}@{a['home']:<4} {picks_txt}" + (f"  {blind}" if blind else ''))
    guarded = any('injury_blind' in a for a in picks) or n_blind
    out.append(f'  {n_pick} pick(s), {n_blind} game(s) INJURY_BLIND' +
               ('' if guarded else '  (file has no injury guard data: it predates the guard or no team is blind)'))
    return out


def todays_card(day: date, cards_dir: Path) -> list[str]:
    path = cards_dir / f'{day.isoformat()}.txt'
    if not path.exists():
        return ['== CARD ==', f'  (no card for {day}: run_daily writes it last)']
    return ['== CARD =='] + path.read_text().rstrip('\n').splitlines()


def build(day: date, *, root: Path = ROOT, run=subprocess.run) -> str:
    sections = [yesterday_results(day, root / 'data/team_picks/results_log.csv'), bets_summary(run),
                todays_picks(day, root / 'data/team_picks'), todays_card(day, root / 'data/cards')]
    text = '\n\n'.join('\n'.join(s) for s in sections) + '\n'
    if len(text.encode()) > MAX_BYTES:                     # trim the card body, keep its header and footer
        card = sections[3]
        head, body, foot = card[:5], card[5:-2], card[-2:]
        while body and len(text.encode()) > MAX_BYTES - 60:
            body = body[:-1]
            sections[3] = head + body + [f'  ... card trimmed to fit {MAX_BYTES} bytes; full card in {day}.txt'] + foot
            text = '\n\n'.join('\n'.join(s) for s in sections) + '\n'
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--date', type=date.fromisoformat)
    a = ap.parse_args(argv)
    day = a.date or slate_today()
    text = build(day)
    out = ROOT / 'data/cards' / f'{day.isoformat()}_brief.txt'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(text)
    print(f'Brief ({len(text.encode())} bytes): {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
