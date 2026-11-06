"""
ESPN sportsbook player-prop lines (free core API, no key).

Shared by scripts/backfill_espn_props.py (historical, opening lines) and the
daily picks (live, current pregame lines via fetch_book_props()).

Each ESPN item is one SIDE of one prop. Through 2024-25 items carry an
explicit over/under label; from 2025-26 the label is gone and pairs arrive
over-then-under (verified against outcomes: as-labeled Brier 0.2476 vs 0.2578
flipped). Only `open` values are guaranteed pregame for past games —
`current` can be a frozen in-game price.
"""

import re
import time
import math
import json
from datetime import datetime, timezone
from pathlib import Path
from src.utils.slate import slate_today, revision_path
from concurrent.futures import ThreadPoolExecutor
from src.data.quote_diagnostics import timestamp_evidence, quote_coverage, print_quote_coverage

import requests
from src.data.errors import SourceError

CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
ODDS_URL = CORE + "/events/{eid}/competitions/{eid}/odds"
PROPS_URL = CORE + "/events/{eid}/competitions/{eid}/odds/{pid}/propBets?limit=1000"
TIMEOUT = 30
WORKERS = 2          # ESPN starts returning 403 after sustained bursts at 6 workers




class Blocked(Exception):
    """ESPN refused or kept failing — do NOT mark the event done."""


class PropRow(tuple):
    """Preserve the historical 11-column DB interface plus source-side evidence."""
    def __new__(cls, values, *, side_verified=False, raw=None):
        row=super().__new__(cls,values)
        row.side_verified=side_verified
        row.raw=raw
        return row

# ESPN market name → our stat_key
MARKETS = {
    'Total Points': 'pts',
    'Total Rebounds': 'reb',
    'Total Assists': 'ast',
    'Total 3-Point Field Goals': 'fg3m',
    'Total Points, Rebounds, and Assists': 'pra',
    'Total Assists and Rebounds': 'reb_ast',
    'Total Points and Rebounds': 'pts_reb',
    'Total Points and Assists': 'pts_ast',
}

def _get(session, url, retries=4):
    """JSON on 200, None on 404 (genuinely absent), raises Blocked otherwise."""
    for attempt in range(retries):
        try:
            requested = datetime.now(timezone.utc).isoformat()
            r = session.get(url, timeout=TIMEOUT)
            if isinstance(getattr(session, 'quote_http_evidence', None), list):
                import hashlib
                session.quote_http_evidence.append(dict(source_url=url, requested_at=requested,
                    captured_at=datetime.now(timezone.utc).isoformat(), http_status=r.status_code,
                    http_date=r.headers.get('Date'), age=r.headers.get('Age'),
                    last_modified=r.headers.get('Last-Modified'),
                    content_sha256=hashlib.sha256(r.content).hexdigest()))
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except (requests.RequestException, ValueError):
            pass
        time.sleep(5 * 2 ** attempt)
    raise Blocked(url)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _side(node):
    if not node:
        return None, None, None
    labels=[s for s in ('over','under') if s in node]
    side=labels[0] if len(labels)==1 else None
    price = (node.get(side) or {}).get('decimal') if side else None
    line = (node.get('target') or {}).get('value')
    return side, line, price


def fetch_event(session, eid: str, game_date: str) -> list[tuple] | None:
    """Prop rows for one event, or None if ESPN blocked/failed (retry next run)."""
    try:
        return _fetch_event(session, eid, game_date)
    except Blocked:
        return None


def _fetch_event(session, eid: str, game_date: str) -> list[tuple]:
    odds = _get(session, ODDS_URL.format(eid=eid))
    rows = []
    for item in (odds or {}).get('items', []):
        prov = item.get('provider') or {}
        skip_live = 'live' in (prov.get('name') or '').lower()
        inventory = dict(event_id=eid, provider=prov, odds_url=ODDS_URL.format(eid=eid),
                         skipped_live_provider=skip_live)
        if isinstance(getattr(session, 'quote_provider_evidence', None), list):
            session.quote_provider_evidence.append(inventory)
        if skip_live:
            continue
        props = _get(session, PROPS_URL.format(eid=eid, pid=prov.get('id')))
        inventory['returned_prop_items'] = len((props or {}).get('items', []))
        seen: dict = {}   # 2025-26+ items omit the over/under label; pairs arrive over-then-under
        for p in (props or {}).get('items', []):
            stat = MARKETS.get((p.get('type') or {}).get('name'))
            ath = (p.get('athlete') or {}).get('$ref')
            if not stat or not ath:
                continue
            m = re.search(r'/athletes/(\d+)', ath)
            if not m:
                continue
            side_o, line_o, price_o = _side(p.get('open'))
            side_c, line_c, price_c = _side(p.get('current'))
            explicit=bool(side_c and (side_o is None or side_o==side_c))
            if side_o and side_c and side_o!=side_c:
                continue
            side = side_o or side_c
            if not side:
                odds = p.get('odds') or {}
                dec, tot = odds.get('decimal') or {}, odds.get('total') or {}
                line_o, line_c = _num(tot.get('open')), _num(tot.get('value'))
                price_o, price_c = _num(dec.get('open')), _num(dec.get('value'))
                k = (m.group(1), stat, line_o)
                seen[k] = seen.get(k, 0) + 1
                if seen[k] > 2:
                    continue
                side = 'over' if seen[k] == 1 else 'under'
                if line_o is None:
                    continue
            rows.append(PropRow((eid, game_date, prov.get('name'), m.group(1), stat, side,
                         line_o, price_o, line_c, price_c, p.get('lastUpdated')),
                         side_verified=explicit,raw=p))
    return rows



SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"


def no_vig_consensus(rows: list[tuple], use_current: bool, *, captured_at=None, rejected=None) -> dict:
    """
    rows: output of fetch_event. Returns
      {(espn_athlete_id, stat): {'line', 'p_over', 'n_books'}}
    using each book's paired over/under at the same line (no-vig), then the
    modal line across books and the mean no-vig P(over) at that line.
    """
    pairs: dict = {}
    verified={}
    for row in rows:
        (_eid, _d, prov, aid, stat, side, line_o, price_o, line_c, price_c, _u)=row
        if use_current and not _u:
            if rejected is not None:
                rejected.append(dict(event_id=_eid, athlete_id=aid, stat=stat, book=prov,
                                     side=side, updated_at=_u, reason='missing/invalid provider timestamp',
                                     timestamp_evidence=timestamp_evidence(_u, captured_at)))
            continue
        if use_current and captured_at is not None:
            from src.betting.quote_quality import timestamp_issue
            issue = timestamp_issue(_u, captured_at)
            if issue:
                if rejected is not None:
                    rejected.append(dict(event_id=_eid, athlete_id=aid, stat=stat, book=prov,
                                         side=side, updated_at=_u, reason=issue,
                                         timestamp_evidence=timestamp_evidence(_u, captured_at)))
                continue
        # Never silently pair a current side with the other side's opening quote.
        line, price = (line_c, price_c) if use_current else (line_o, price_o)
        line, price = _num(line), _num(price)
        if (not prov or not aid or line is None or price is None or not math.isfinite(line)
                or not math.isfinite(price) or price <= 1 or side not in ('over', 'under')):
            continue
        pairs.setdefault((_eid, aid, stat, prov, line, _u), {}).setdefault(side, []).append(price)
        verified[(_eid,aid,stat,prov,line,_u,side)]=bool(getattr(row,'side_verified',False))
    by_prop: dict = {}
    for (eid, aid, stat, prov, line, updated), sides in pairs.items():
        if 'over' not in sides or 'under' not in sides:
            continue
        if any(len(v) != 1 for v in sides.values()):
            continue  # duplicate quotes are ambiguous, never last-row-wins
        sides = {k: v[0] for k, v in sides.items()}
        io, iu = 1 / sides['over'], 1 / sides['under']
        if not 0 < io + iu - 1 < 0.15:
            continue
        by_prop.setdefault((aid, stat), []).append((line, io / (io + iu), prov,
                                                    sides['over'], sides['under'], eid, updated,
                            all(verified.get((eid,aid,stat,prov,line,updated,s),False) for s in ('over','under'))))
    out = {}
    for key, lst in by_prop.items():
        if len({x[5] for x in lst}) != 1:
            continue
        lines = [x[0] for x in lst]
        modal = max(sorted(set(lines)), key=lines.count)
        at = [x for x in lst if x[0] == modal]
        if len({x[2] for x in at}) != len(at):
            continue  # multiple versions of one book cannot count as consensus
        out[key] = {'line': modal, 'p_over': sum(x[1] for x in at) / len(at), 'n_books': len(at),
                    'best_over': max(x[3] for x in at), 'best_under': max(x[4] for x in at),
                    'best_over_book': max(at, key=lambda x: x[3])[2],
                    'best_under_book': max(at, key=lambda x: x[4])[2],
                    'event_id': at[0][5], 'espn_athlete_id': key[0],
                    'price_kind': 'current' if use_current else 'opening',
                    'anchor_kind': 'multi-book' if len(at) > 1 else 'single-book',
                    'side_mapping_verified': all(x[7] for x in at),
                    'paired_quotes': [dict(book=x[2], line=x[0], over=x[3], under=x[4],
                                           updated_at=x[6], event_id=x[5]) for x in at]}
    return out


def fetch_book_props(target_date: str, workers: int = 2) -> dict:
    """
    Live sportsbook consensus for every player prop on target_date (YYYY-MM-DD).
    Returns {(full_name, stat_key): {'line', 'p_over', 'n_books'}}.
    Transport failures raise; a successful fetch can have no usable quotes.
    """
    session = requests.Session()
    session.quote_http_evidence = []
    session.quote_provider_evidence = []
    try:
        sb = _get(session, SCOREBOARD + f"?dates={target_date.replace('-', '')}")
    except Blocked as exc:
        raise RuntimeError('Sportsbook scoreboard fetch failed') from exc
    if sb is None:
        raise RuntimeError('Sportsbook scoreboard unavailable')
    from datetime import date
    live_date = target_date >= slate_today().isoformat()
    # The model predicts full-game pregame props. In-game prices must never
    # become its market anchor; historical backfills still use opening prices.
    from src.data.live_evidence import pregame_event
    events = [ev['id'] for ev in (sb or {}).get('events', [])
              if not live_date or
              pregame_event(ev)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda e: fetch_event(session, e, target_date), events))
    failed_events = [eid for eid, result in zip(events, results) if result is None]
    if failed_events:
        raise RuntimeError('Sportsbook prop transport failed for events: ' + ', '.join(failed_events))
    rows = [r for res in results if res for r in res]
    # For past dates `current` is often a frozen in-game price — use opening lines.
    captured = datetime.now(timezone.utc).isoformat()
    rejected = []
    cons = no_vig_consensus(rows, use_current=live_date, captured_at=captured, rejected=rejected)
    coverage = quote_coverage(rows, rejected, cons, timestamp_check_applied=live_date)
    from src.data.shadow_freshness import compare
    from config.settings import SHADOW_CAPTURE_MINUTES, SHADOW_FUTURE_TOLERANCE_SECONDS
    shadow = compare(rows, sb, session.quote_http_evidence, captured, capture_minutes=SHADOW_CAPTURE_MINUTES,
                     future_tolerance_seconds=SHADOW_FUTURE_TOLERANCE_SECONDS,
                     provider_inventory=session.quote_provider_evidence) if live_date else None
    if shadow:
        print(f"  Shadow freshness only: current={shadow['current_paired_props']} vs shadow={shadow['shadow_paired_props']} paired props; future anomalies={shadow['future_anomaly_sides']} sides. Production gate unchanged.")
        for group in shadow['by_book_stat']:
            print(f"    {group['book']} {group['stat']}: current={group['current_eligible_sides']} / shadow={group['shadow_eligible_sides']} sides; future anomalies={group['future_anomaly_sides']}")
    print_quote_coverage(coverage)
    # Keep raw quotes for later as-of investigations. This is an append-only
    # snapshot, not a historical claim about when a quote first became available.
    from config.settings import PROJECT_ROOT
    directory = PROJECT_ROOT / 'data/quote_snapshots'
    if rows or session.quote_provider_evidence:
        directory.mkdir(parents=True, exist_ok=True)
        revision_path(directory / f'{target_date}.json').write_text(json.dumps(
            dict(captured_at=captured, scoreboard=sb, rows=rows, rejected_quotes=rejected,
                 http_evidence=session.quote_http_evidence,
                 provider_inventory=session.quote_provider_evidence, quote_coverage=coverage, shadow_freshness=shadow,
                 row_evidence=[dict(side_verified=getattr(r,'side_verified',False),raw=getattr(r,'raw',None))
                               for r in rows]), indent=2))
    if rejected:
        from collections import Counter
        counts = Counter(r['reason'] for r in rejected)
        print('  Excluded quote sides: ' + ', '.join(f'{k}={v}' for k,v in sorted(counts.items())))
    event_map = {ev['id']: ev for ev in (sb or {}).get('events', [])}
    from src.data.espn_odds import _normalize
    for v in cons.values():
        event = event_map.get(v['event_id'], {})
        competitors = (event.get('competitions') or [{}])[0].get('competitors', [])
        v.update(captured_at=captured, start_time=event.get('date'),
                 status='scheduled' if event.get('status', {}).get('type', {}).get('state') == 'pre' else 'unknown',
                 teams=[_normalize(c.get('team', {}).get('abbreviation', '')) for c in competitors],
                 # ESPN's unlabeled pair ordering is a historical heuristic,
                 # not authoritative settlement-side evidence.
                 side_mapping_verified=v.get('side_mapping_verified',False))

    for value in cons.values():
        for pair in value['paired_quotes']:
            provider = next((r['provider'] for r in session.quote_provider_evidence
                             if r['event_id'] == pair['event_id'] and r['provider'].get('name') == pair['book']), {})
            url = PROPS_URL.format(eid=pair['event_id'], pid=provider.get('id'))
            evidence = next((r for r in reversed(session.quote_http_evidence) if r['source_url'] == url), {})
            pair.update(source_url=url, http_date=evidence.get('http_date'),
                        http_age=evidence.get('age'), http_captured_at=evidence.get('captured_at'))

    names: dict = {}
    for aid in {aid for aid, _ in cons}:
        try:
            j = _get(session, f"{CORE}/athletes/{aid}")
            names[aid] = (j or {}).get('fullName')
            if not names[aid]:
                raise SourceError('Missing fullName', source=f'ESPN athlete {aid}', kind='schema')
        except Blocked as exc:
            raise SourceError(str(exc), source=f'ESPN athlete {aid}') from exc
    return {(names[aid], stat): v for (aid, stat), v in cons.items() if names.get(aid)}
