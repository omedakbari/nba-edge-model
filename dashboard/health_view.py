"""Today page health panel (AUDIT_2026-11-24.md item 10, Phase 4a): one green/yellow/red tile per data source
with its last success. Pure functions (sources()) so the rules are tested without Streamlit.

Sources and rules:
  Game logs     worker boxscores + validate jobs, and the local nba_edge.db newest box score
                (> 2 days behind yesterday -> yellow)
  Rosters       worker rosters job
  Injuries      worker injuries job; the card's injury data STALE flag -> red
  DK (ESPN)     sportsbook sides offered vs passed (legacy "Model +EV" table only): 0 passed -> yellow
  Kalshi        worker kalshi job
  PropLine      worker propline job (DK/FD/MGM/Pinnacle + Sleeper lines the card uses)
  Grading       catch-up lag (src/utils/grading.py) -> yellow; worker grade/bets jobs
  Calibration   card calibration artifact health (missing -> red, stale/thin -> yellow)
  Daily run     the last run_daily exit: 0 green, 3 (WARN) yellow, anything else red
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

RANK = {'green': 0, 'yellow': 1, 'red': 2, 'unknown': 1}


def worst(*levels: str) -> str:
    levels = [lv for lv in levels if lv]
    return max(levels, key=lambda lv: RANK.get(lv, 1)) if levels else 'unknown'


def _job(health: dict, name: str) -> dict:
    for j in health.get('jobs', []):
        if j['job'] == name:
            return j
    return {'job': name, 'level': 'unknown', 'reasons': [f'no {name} run in 3 days'], 'last_green': None}


def _tile(source: str, level: str, last: str | None, detail: str) -> dict:
    return {'source': source, 'level': level, 'last_success': last, 'detail': detail}


def sources(*, health: dict, today: date, local_max_game_date: str | None, card: dict | None,
            feed_lines: list[str], grading_lag: list[str], team_last_graded: str | None,
            calibration_warnings: list[str], calibration_fit: str | None, run: dict | None) -> list[dict]:
    out = []
    jobs_ok = health.get('ok', False)

    def job_tile(source, names, extra_level=None, extra_detail=''):
        if not jobs_ok:
            return _tile(source, 'unknown', None, f"Supabase job_runs unreadable: {health.get('error')}")
        js = [_job(health, n) for n in names]
        level = worst(*(j['level'] for j in js), extra_level)
        last = min((j.get('last_green') for j in js if j.get('last_green')), default=None)
        reasons = '; '.join(f"{j['job']}: {r}" for j in js for r in j.get('reasons', []))
        return _tile(source, level, last, '; '.join(x for x in (reasons, extra_detail) if x) or 'ok')

    yesterday = today - timedelta(days=1)
    behind = (yesterday - date.fromisoformat(local_max_game_date)).days if local_max_game_date else None
    local_level = 'yellow' if behind is None or behind > 2 else 'green'
    out.append(job_tile('Game logs', ['boxscores', 'validate'], local_level,
                        f"local DB newest box score {local_max_game_date or 'none'}"
                        + (f" ({behind} days behind yesterday)" if behind and behind > 2 else '')))
    out.append(job_tile('Rosters', ['rosters']))
    inj = (card or {}).get('injury') or {}
    out.append(job_tile('Injuries', ['injuries'], 'red' if inj.get('stale') else None,
                        'card injury data STALE' if inj.get('stale') else ''))
    if feed_lines:
        dead = any(' 0/' in f for f in feed_lines)
        out.append(_tile('DK (ESPN)', 'yellow' if dead else 'green', None,
                         '; '.join(feed_lines) + ' — legacy Model +EV table only; the card uses PropLine'))
    else:
        out.append(_tile('DK (ESPN)', 'unknown', None, 'no quote snapshot for today'))
    out.append(job_tile('Kalshi', ['kalshi']))
    out.append(job_tile('PropLine / Sleeper', ['propline']))
    g = job_tile('Grading', ['grade', 'bets'], 'yellow' if grading_lag else None, '; '.join(grading_lag))
    g['last_success'] = team_last_graded or g['last_success']
    out.append(g)
    cal_level = 'red' if any('missing' in w for w in calibration_warnings) else ('yellow' if calibration_warnings else 'green')
    out.append(_tile('Calibration', cal_level, calibration_fit, '; '.join(calibration_warnings) or 'ok'))
    if run is None:
        out.append(_tile('Daily run', 'unknown', None, 'no dashboard-launched run yet'))
    else:
        code = run.get('exit')
        level = 'green' if code == 0 else 'yellow' if code == 3 else 'unknown' if code is None else 'red'
        out.append(_tile('Daily run', level, run.get('started') if code in (0, 3) else None,
                         {0: 'OK', 3: 'finished with warnings', None: 'running'}.get(code, f'exit {code}')
                         + (' (dry run)' if run.get('dry') else '')))
    return out


ICON = {'green': '✓', 'yellow': '!', 'red': '✕', 'unknown': '?'}
CLS = {'green': 'ok', 'yellow': 'warn', 'red': 'fail', 'unknown': 'warn'}


def panel_html(tiles: list[dict], esc, ago) -> str:
    cells = []
    for t in tiles:
        last = ago(t['last_success']) if t['last_success'] and 'T' in t['last_success'] else (t['last_success'] or '—')
        cells.append(f'<div class="htile {CLS[t["level"]]}" title="{esc(t["detail"])}">'
                     f'<div class="hname">{ICON[t["level"]]} {esc(t["source"])}</div>'
                     f'<div class="hlast">last ok: {esc(last)}</div></div>')
    return '<div class="hgrid">' + ''.join(cells) + '</div>'
