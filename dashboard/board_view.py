"""Props board (Phase 4b) and Kalshi ladders (4d): pure frame builders for dashboard/today.py.

Board columns: Tip | Game | Player | Team | Opp | Market | Side | Line/Threshold | Venue | Projection | P raw |
P calibrated | Edge | L5 | L10 | L20 | Season | H2H | L10 values | MIN L10 | Price | Flags.
Hit rates are 'hits/n' at the leg's own line and side (worker/prop_worker/card.leg_stats).
Projection: the ML model's projection from the fair board (data/fair/<date>.json, newest revision) when that
player/stat is on it. The fair board only covers props with a fresh sportsbook (DK via ESPN) line, so it is
usually blank while that feed is down. The card itself has no projection (its p is hit-rate history + market).
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FAIR_STAT = {'pts': 'PTS', 'reb': 'REB', 'ast': 'AST', 'fg3m': '3PM', 'pra': 'PRA', 'ra': 'RA', 'reb_ast': 'RA'}
BOARD_COLUMNS = ['Tip', 'Game', 'Player', 'Team', 'Opp', 'Market', 'Side', 'Line/Threshold', 'Venue', 'Projection',
                 'P raw', 'P calibrated', 'Edge', 'L5', 'L10', 'L20', 'Season', 'H2H', 'L10 values', 'MIN L10',
                 'Price', 'Flags']


def norm_name(name: str) -> str:
    s = unicodedata.normalize('NFKD', name or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower().replace('.', '').replace("'", '')
    for suffix in (' jr', ' sr', ' ii', ' iii', ' iv'):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return ' '.join(s.split())


def fair_projections(day: str, folder: Path = ROOT / 'data' / 'fair') -> dict[tuple[str, str], float]:
    """{(normalized name, stat label): ML projection} from the newest fair-board revision for the day."""
    from src.utils.slate import latest_revision
    try:
        rows = json.loads(latest_revision(folder / f'{day}.json').read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for r in rows if isinstance(rows, list) else []:
        stat = FAIR_STAT.get(r.get('stat'))
        if stat and r.get('pred') is not None:
            out[(norm_name(r.get('player', '')), stat)] = round(float(r['pred']), 1)
    return out


def _price(leg: dict) -> str:
    if leg.get('platform') == 'kalshi' and leg.get('price') is not None:
        mx = f" / {leg['max_price'] * 100:.0f}¢" if leg.get('max_price') is not None else ''
        return f"{leg['price'] * 100:.0f}¢{mx}"
    return "pick'em"


def board_frame(legs: list[dict], projections: dict | None = None) -> pd.DataFrame:
    projections = projections or {}
    rows = []
    for g in legs:
        threshold = g.get('line')
        rows.append({
            'Tip': g.get('tip_pt'), 'Game': g.get('game'), 'Player': g.get('player'), 'Team': g.get('team'),
            'Opp': g.get('opp'), 'Market': g.get('market') or f"{g.get('stat')} {threshold}",
            'Side': g.get('side'), 'Line/Threshold': threshold,
            'Venue': {'kalshi': 'Kalshi', 'sleeper': 'Sleeper'}.get(g.get('platform'), g.get('platform')),
            'Projection': projections.get((norm_name(g.get('player', '')), g.get('stat'))),
            'P raw': g.get('p_raw'), 'P calibrated': g.get('p'), 'Edge': g.get('edge'),
            'L5': g.get('l5', ''), 'L10': g.get('l10', ''), 'L20': g.get('l20', ''), 'Season': g.get('season', ''),
            'H2H': g.get('h2h', ''),
            'L10 values': ' '.join(f'{v:g}' for v in (g.get('l10_values') or []) if v is not None),
            'MIN L10': g.get('min_l10'), 'Price': _price(g), 'Flags': ', '.join(g.get('flags') or [])})
    return pd.DataFrame(rows, columns=BOARD_COLUMNS)


LADDER_COLUMNS = ['Tip', 'Game', 'Player', 'Team', 'Market', 'Strike', 'YES ask', 'NO ask', 'Mid', 'L10 at strike',
                  'P raw (YES)', 'P cal (YES)', 'Edge YES', 'Edge NO', 'Stale']


def ladder_frame(ladders: list[dict]) -> pd.DataFrame:
    """One row per Kalshi strike: asks, mid, raw and calibrated P(YES), and the calibrated edge of each side."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    rows = []
    for lad in ladders:
        tip = lad.get('tip')
        tip_pt = datetime.fromisoformat(tip).astimezone(ZoneInfo('America/Los_Angeles')).strftime('%-I:%M%p').lower() \
            if tip else ''
        for s in lad['strikes']:
            pc, ya, na = s.get('p_cal_yes'), s.get('yes_ask'), s.get('no_ask')
            rows.append({'Tip': tip_pt, 'Game': lad.get('game'), 'Player': lad.get('player'), 'Team': lad.get('team'),
                         'Market': f"{lad.get('stat')} {s['strike']}+", 'Strike': s['strike'], 'YES ask': ya,
                         'NO ask': na, 'Mid': s.get('mid'), 'L10 at strike': s.get('l10'),
                         'P raw (YES)': s.get('p_raw_yes'), 'P cal (YES)': pc,
                         'Edge YES': round(pc - ya, 4) if pc is not None and ya is not None else None,
                         'Edge NO': round((1 - pc) - na, 4) if pc is not None and na is not None else None,
                         'Stale': bool(s.get('stale'))})
    return pd.DataFrame(rows, columns=LADDER_COLUMNS)
