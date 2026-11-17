"""Injury guard for team picks: a guard, not a model. The ratings don't see who is out.

Only RECENT absences count: Out/Doubtful players who played within RECENT_DAYS (14) of the slate.
Anyone out longer is already reflected in the team's recent results, so the ratings have priced it.
A team is INJURY_BLIND if any of these holds for its recent absences:
  * top 2   one of them is in the team's top 2 in points per game (pool: everyone who played for the
            team in the last 14 days; each player's ppg over his last 15 played games)
  * share   together they score >= 20% of the team's points per game (team: last 15 final games)
  * minutes together they play >= 60 minutes per game (each player's last 15 played games)
Sources:
  * worker (Supabase): injuries = the official NBA report for the date (players on its newest run),
    else ESPN for players not on it; minutes, points and scores from the worker's game_logs / games.
  * fallback when the worker can't be read or has nothing for the date: the ESPN injuries snapshot
    the daily run saves (data/injury_snapshots/<date>.json, newest revision), with the local
    nba_edge.db game_logs (players matched by name).
A blind game's spread and total STRONG/LEAN become MONITOR (no pick); the flag is saved with the
pick JSON under 'injury_blind', with the tiers the game had before the guard.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo('America/New_York')
OUT_STATUSES = {'out', 'doubtful'}
WINDOW = 15
RECENT_DAYS = 14
TOP_N = 2
SHARE_MIN = 0.20
MINUTES_MIN = 60.0
PICK_TIERS = ('STRONG', 'LEAN')


# ── rule ────────────────────────────────────────────────────────────────────

def _d(v) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def recent(absent: list[dict], target: date) -> list[dict]:
    """Out/Doubtful players who played within RECENT_DAYS before the slate."""
    since = target - timedelta(days=RECENT_DAYS)
    return [a for a in absent if (a.get('status') or '').lower() in OUT_STATUSES
            and _d(a.get('last_played')) is not None and _d(a['last_played']) >= since]


def team_flag(absent: list[dict], pool: list[dict], team_ppg: float | None, target: date) -> dict | None:
    """absent: [{'name','status','mpg','ppg','last_played'}]; pool: [{'name','ppg'}] who played for the
    team in the last 14 days (absent players included); team_ppg: team points per game. Flag or None."""
    rows = recent(absent, target)
    if not rows:
        return None
    ranked = sorted((p for p in pool if p.get('ppg') is not None), key=lambda p: -p['ppg'])
    top = [p['name'] for p in ranked[:TOP_N]]
    out_names = {a['name'] for a in rows}
    stars = [n for n in top if n in out_names]
    pts = sum(a.get('ppg') or 0 for a in rows)
    mins = sum(a.get('mpg') or 0 for a in rows)
    share = pts / team_ppg if team_ppg else None
    why = []
    if stars:
        why.append(f"{', '.join(stars)} top-{TOP_N} scorer")
    if share is not None and share >= SHARE_MIN - 1e-9:
        why.append(f'{share:.0%} of team points (>= {SHARE_MIN:.0%})')
    if mins >= MINUTES_MIN - 1e-9:
        why.append(f'{mins:.0f} mpg out (>= {MINUTES_MIN:.0f})')
    if not why:
        return None
    return {'reason': '; '.join(why), 'top2': top, 'points_out': round(pts, 1), 'minutes_out': round(mins, 1),
            'team_ppg': round(team_ppg, 1) if team_ppg else None,
            'share': round(share, 3) if share is not None else None,
            'players': [{'name': a['name'], 'status': a['status'], 'ppg': round(a.get('ppg') or 0, 1),
                         'mpg': round(a.get('mpg') or 0, 1), 'last_played': str(_d(a['last_played']))}
                        for a in sorted(rows, key=lambda a: -(a.get('ppg') or 0))]}


def apply_guard(a: dict, flags: dict[str, dict | None], source: str = '') -> dict:
    """Downgrade a game's spread/total STRONG/LEAN to MONITOR if either team is INJURY_BLIND. In place."""
    blind = {t: flags.get(t) for t in (a['home'], a['away']) if flags.get(t)}
    if not blind:
        return a
    before = {'tier': a.get('tier'), 'ats_strength': a.get('ats_strength'), 'ou_tier': a.get('ou_tier'),
              'spread_pick_team': a.get('spread_pick_team'), 'spread_pick_side': a.get('spread_pick_side')}
    for key in ('tier', 'ats_strength', 'ou_tier'):
        if a.get(key) in PICK_TIERS:
            a[key] = 'MONITOR'
    if before['tier'] in PICK_TIERS or before['ats_strength'] in PICK_TIERS:
        a['spread_pick_team'] = a['spread_pick_side'] = None       # no spread pick on a blind game
    a['injury_blind'] = {'teams': blind, 'source': source, 'before_guard': before}
    return a


def describe(a: dict) -> str:
    ib = a.get('injury_blind')
    if not ib:
        return ''
    parts = []
    for team, f in ib['teams'].items():
        names = ', '.join(f"{p['name']} {p['ppg']:.0f}p/{p['mpg']:.0f}m" for p in f['players'])
        parts.append(f"{team}: {names} ({f['reason']})")
    b = ib['before_guard']
    was = ', '.join(w for w in (f"spread {b['tier']}" if b['tier'] in PICK_TIERS else '',
                                f"total {b['ou_tier']}" if b['ou_tier'] in PICK_TIERS else '') if w)
    return f"INJURY_BLIND {a['away']} @ {a['home']}: " + ' | '.join(parts) + \
        (f"  -> {was} downgraded to MONITOR" if was else '')


# ── sources: each returns {team: {'absent': [...], 'pool': [...], 'team_ppg': float|None}} ──

def _cutoff(target: date, now: datetime | None = None) -> datetime:
    """As-of time for a slate: now for today, else 06:00 ET the next morning (slate rollover)."""
    now = now or datetime.now(timezone.utc)
    end = datetime.combine(target + timedelta(days=1), datetime.min.time(), ET).replace(hour=6)
    return min(now, end.astimezone(timezone.utc))


_PG_PLAYER = f"""select avg(pts), avg(m), max(d) from (
    select gl.pts::float8 pts, gl.min::float8 m, g.game_date d from game_logs gl join games g on g.game_id = gl.game_id
    where gl.player_id = %s and g.game_date < %s and not gl.did_not_play and gl.min > 0
    order by g.game_date desc limit {WINDOW}) t"""


def from_worker(target: date, teams: set[str], now: datetime | None = None) -> dict[str, dict]:
    sys.path.insert(0, str(ROOT / 'worker'))
    from prop_worker.db import connect

    cutoff = _cutoff(target, now)
    since = target - timedelta(days=RECENT_DAYS)
    out = {t: {'absent': [], 'pool': [], 'team_ppg': None} for t in teams}
    with connect() as c:
        c.read_only = True
        seen: set[int] = set()
        for source, cond in (('nba_pdf', 'game_date = %(d)s'), ('espn', 'true')):
            newest = c.execute(f"""select max(last_seen_at) from injuries where sport = 'nba' and source = %(src)s
                                   and {cond} and fetched_at <= %(cut)s""",
                               {'src': source, 'd': target, 'cut': cutoff}).fetchone()[0]
            if newest is None:
                continue
            rows = c.execute(
                f"""select * from (
                        select distinct on (i.player_id) i.player_id, p.full_name, i.team_abbr, i.status, i.last_seen_at
                        from injuries i join players p on p.player_id = i.player_id
                        where i.sport = 'nba' and i.source = %(src)s and {cond} and i.fetched_at <= %(cut)s
                          and i.team_abbr = any(%(teams)s)
                        order by i.player_id, i.fetched_at desc, i.id desc) t
                    where last_seen_at >= %(newest)s - interval '5 minutes'""",
                {'src': source, 'd': target, 'cut': cutoff, 'teams': sorted(teams), 'newest': newest}).fetchall()
            for pid, name, team, status, _ in rows:
                if pid in seen:
                    continue                    # official report wins over ESPN
                seen.add(pid)
                if (status or '').lower() in OUT_STATUSES:
                    ppg, mpg, last = c.execute(_PG_PLAYER, (pid, target)).fetchone()
                    out[team]['absent'].append({'name': name, 'status': status, 'ppg': ppg, 'mpg': mpg,
                                                'last_played': last, 'source': source})
        if not seen:
            raise LookupError(f'no worker injury rows for {target}')
        for team, t in out.items():
            for pid, name in c.execute(
                    """select distinct gl.player_id, p.full_name from game_logs gl
                       join games g on g.game_id = gl.game_id join players p on p.player_id = gl.player_id
                       where gl.team_abbr = %s and g.game_date >= %s and g.game_date < %s and gl.min > 0""",
                    (team, since, target)).fetchall():
                t['pool'].append({'name': name, 'ppg': c.execute(_PG_PLAYER, (pid, target)).fetchone()[0]})
            # from player points, not games.home_score: games backfilled from nba_edge.db have no scores
            t['team_ppg'] = c.execute(
                f"""select avg(s) from (select sum(gl.pts)::float8 s from game_logs gl join games g on g.game_id = gl.game_id
                    where gl.sport = 'nba' and gl.team_abbr = %(t)s and g.game_date < %(d)s
                    group by g.game_id, g.game_date order by g.game_date desc limit {WINDOW}) x""",
                {'t': team, 'd': target}).fetchone()[0]
        c.rollback()
    return out


def _norm(name: str) -> str:
    import unicodedata
    n = unicodedata.normalize('NFKD', name or '')
    n = ''.join(ch for ch in n if not unicodedata.combining(ch)).lower()
    n = n.replace('.', '').replace("'", '').replace('-', ' ')
    for suffix in (' jr', ' sr', ' iii', ' ii', ' iv'):
        if n.endswith(suffix):
            n = n[:-len(suffix)]
    return ' '.join(n.split())


def espn_snapshot_absences(raw: dict, teams: set[str]) -> dict[str, list[dict]]:
    from src.data.espn_odds import _normalize
    out: dict[str, list[dict]] = {t: [] for t in teams}
    for block in raw.get('injuries', []):
        for inj in block.get('injuries', []):
            ath = inj.get('athlete') or {}
            team = _normalize(((ath.get('team') or {}).get('abbreviation')) or '')
            if team in out and (inj.get('status') or '').lower() in OUT_STATUSES:
                out[team].append({'name': ath.get('displayName'), 'status': inj.get('status'), 'source': 'espn'})
    return out


def local_player(conn: sqlite3.Connection, name: str, before: str) -> tuple:
    """(ppg, mpg, last_played) over the player's last 15 played games in nba_edge.db, by name."""
    ids = [pid for pid, full in conn.execute('select player_id, full_name from players') if _norm(full) == _norm(name)]
    if not ids:
        return None, None, None
    return conn.execute(f"""select avg(pts), avg(min), max(game_date) from (select pts, min, game_date from game_logs
                            where player_id in ({','.join('?' * len(ids))}) and game_date < ? and min > 0
                            order by game_date desc limit {WINDOW})""", (*ids, before)).fetchone()


def local_team(conn: sqlite3.Connection, team: str, target: date) -> tuple[list[dict], float | None]:
    since, before = (target - timedelta(days=RECENT_DAYS)).isoformat(), target.isoformat()
    pool = []
    for pid, name in conn.execute("""select distinct g.player_id, p.full_name from game_logs g
                                     join players p on p.player_id = g.player_id
                                     where g.team_abbr = ? and g.game_date >= ? and g.game_date < ? and g.min > 0""",
                                  (team, since, before)).fetchall():
        ppg = conn.execute(f"""select avg(pts) from (select pts from game_logs where player_id = ? and game_date < ?
                               and min > 0 order by game_date desc limit {WINDOW})""", (pid, before)).fetchone()[0]
        pool.append({'name': name, 'ppg': ppg})
    team_ppg = conn.execute(f"""select avg(s) from (select sum(pts) s from game_logs where team_abbr = ? and game_date < ?
                                group by game_id, game_date order by game_date desc limit {WINDOW})""",
                            (team, before)).fetchone()[0]
    return pool, team_ppg


def from_espn_snapshot(target: date, teams: set[str], db_path: Path) -> dict[str, dict]:
    from src.utils.slate import latest_revision
    path = latest_revision(ROOT / 'data/injury_snapshots' / f'{target.isoformat()}.json')
    if path is None or not Path(path).exists():
        raise LookupError(f'no ESPN injury snapshot for {target}')
    raw = json.loads(Path(path).read_text())['raw']
    absences = espn_snapshot_absences(raw, teams)
    out = {}
    with sqlite3.connect(str(db_path)) as conn:
        for team, rows in absences.items():
            for r in rows:
                r['ppg'], r['mpg'], r['last_played'] = local_player(conn, r['name'], target.isoformat())
            pool, team_ppg = local_team(conn, team, target)
            out[team] = {'absent': rows, 'pool': pool, 'team_ppg': team_ppg}
    return out


def _redact(text: str) -> str:
    try:
        sys.path.insert(0, str(ROOT / 'worker'))
        from prop_worker.masking import redact
        return redact(text)
    except Exception:  # noqa: BLE001
        return re.sub(r'postgres(ql)?://\S+', '<redacted>', text)


def team_flags(target: date, teams: set[str], db_path: Path, now: datetime | None = None) -> tuple[dict, str, list]:
    """({team: flag|None}, source label, errors) — worker first, ESPN snapshot second, else nothing checked."""
    errors = []
    for label, load in (('worker injuries', lambda: from_worker(target, teams, now)),
                        ('ESPN snapshot', lambda: from_espn_snapshot(target, teams, db_path))):
        try:
            data = load()
        except Exception as exc:  # noqa: BLE001 — fall through to the next source
            errors.append(_redact(f'{label}: {type(exc).__name__}: {str(exc)[:120]}'))
            continue
        return ({t: team_flag(d['absent'], d['pool'], d['team_ppg'], target) for t, d in data.items()},
                label, errors)
    return {t: None for t in teams}, 'none', errors
