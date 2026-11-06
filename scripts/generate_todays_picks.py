"""
Live daily picks generator — v8.

Fetches real Sleeper Pick'em lines (primary), today's NBA schedule, current rosters,
and the ESPN injury report. Outputs two SAFE Flex cards: 3-leg and 5-leg. Falls back
to PrizePicks then L10-as-line mode with a warning if Sleeper is unreachable.

Usage:
    python scripts/generate_todays_picks.py [--date 2026-04-04] [--season 2025-26]

Probability thresholds (against real Sleeper lines, not L10 fake lines):
    All stats: P >= 57-58%  — any model signal above 57% is edge on a no-vig platform.
"""

import argparse
import json
import math
import sys
import time
import sqlite3
import unicodedata
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

from src.utils.slate import slate_today
from config.settings import PROCESSED_DIR, DB_PATH, NBA_API_DELAY, CURRENT_SEASON, BLEND_L10_WEIGHT
from src.playoffs.adjustments import (
    load_playoff_elevation_ratios,
    load_playoff_elevation_ratios_per_stat,
    apply_playoff_adjustments,
    should_suppress,
    playoff_context_note,
)
from src.utils.season import detect_phase, get_playoff_round, ROUND_LABELS
from src.utils.probability import prob_over, load_model

# Sleeper API uses the start year of the season (e.g. 2025 for 2025-26)
_SLEEPER_API_YEAR = CURRENT_SEASON.split('-')[0]


# ── Constants ──────────────────────────────────────────────────────────────────

COMPOSITE_STATS = {
    'pra':     ['pts', 'reb', 'ast'],
    'reb_ast': ['reb', 'ast'],
}

# Thresholds for use against REAL Sleeper lines (not fake L10 baselines).
# Sleeper lines are set near expected value, so any P > 55% represents real edge.
# The old 0.70-0.75 thresholds were calibrated on L10-as-line (a much easier bar).
# Against real market lines, the model rarely exceeds 0.65 — that's expected and fine.
# Daily cards are built by sorting on prob and picking the top N above these floors.
STAT_MIN_PROB = {
    'reb':     0.57,
    'ast':     0.57,
    'pra':     0.57,
    'reb_ast': 0.57,
    'pts':     0.57,
}

# Playoff-specific minimums — tighter thresholds derived from backtest.
# AST top-quintile (P≥0.75 vs synthetic line) hits 68%; REB non-monotonic → 0.60 floor.
# PTS: model discriminates at P≥0.65 against real Sleeper lines.
STAT_MIN_PROB_PLAYOFFS = {
    'reb':     0.60,
    'ast':     0.62,
    'pra':     0.60,
    'reb_ast': 0.60,
    'pts':     0.60,
}

# Tier-aware playoff gate — applied on top of STAT_MIN_PROB_PLAYOFFS.
# Effective floor = max(stat_min, tier_min).
# Source: policy-gating ablation (4-season backtest, 19,445 player-game rows).
#   ROTATION at P≥55%: 54.8% HR across all stats — below SAFE Flex breakeven.
#   ROTATION at P≥65%: 60.1% HR, +5.3pp gain, −723 picks (10.9% volume reduction).
#   CORE at P≥55%: 61.6% HR — gating to 60% costs 885 picks for +1.0pp, not worth it.
#   STAR at P≥55%: 57.5% HR — flat below P≥70%, no benefit gating lower.
TIER_MIN_PROB_PLAYOFFS = {
    'STAR':     0.55,   # stat-level floor governs; tier gate adds nothing below P≥70%
    'CORE':     0.55,   # 61.6% at P≥55% is already strong; gating sacrifices good picks
    'ROTATION': 0.65,   # 54.8%→60.1% improvement; filters low-quality ROTATION picks
}

# Stat×tier overrides — applied on top of TIER_MIN_PROB_PLAYOFFS.
# STAR PTS: confidence inversion at [65%,70%) band (39.5% HR) — model non-monotone
#   in 55-70% range. Only 70%+ picks are reliable (65.7%→69.7% HR with STD fix).
#   Ablation (ablation_star_pts.py Config D): no single lever restores monotonicity;
#   policy gate is the correct production protection.
STAT_TIER_MIN_PROB_PLAYOFFS: dict[tuple[str, str], float] = {
    ('STAR', 'pts'): 0.70,
}

# Round-progressive probability floors (playoffs only).
# Tighter thresholds in later rounds: backtest shows R1 has best accuracy,
# Finals has worst (PTS 59.7% R1 → 55.6% Finals). These are added on top of
# STAT_MIN_PROB_PLAYOFFS so the effective floor = max(stat_floor, round_floor).
ROUND_PROB_FLOORS: dict[int, float] = {
    1: 0.60,   # First Round — baseline
    2: 0.62,   # Conference Semifinals
    3: 0.63,   # Conference Finals
    4: 0.65,   # Finals — most volatile, highest model error
    0: 0.60,   # Unknown / fallback
}


# PrizePicks stat_type strings → our internal stat keys
PP_STAT_MAP = {
    'Points':        'pts',
    'Rebounds':      'reb',
    'Assists':       'ast',
    'Pts+Rebs+Asts': 'pra',
    'Rebs+Asts':     'reb_ast',
    '3-PT Made':     'fg3m',
}

STATS = ['reb', 'ast', 'pra', 'reb_ast', 'pts']
MIN_MINUTES = 25   # raised from 20 — keeps out fringe/garbage-time players
DEF_WINDOW = 15

# ── Edge buffer (item 4) ───────────────────────────────────────────────────────
# Leg must have pred at least this far from line, OR prob >= MIN_EDGE_PROB.
EDGE_BUFFER_GAP: dict[str, float] = {
    'pts':     3.0,
    'ast':     1.0,
    'reb_ast': 2.0,
    'pra':     2.0,
    'reb':     2.0,
}
MIN_EDGE_PROB = 0.65   # override: include even if gap < threshold at this confidence

# Market-anchored mode (default): final P = sportsbook no-vig consensus moved to the
# pick'em line, nudged by the model (src/models/market_blend.py). Validated
# out-of-sample on 2025-26: legs with P >= 0.56 hit 60.5% (n=7,624) vs ~54.2%
# pick'em breakeven; the model alone was ~5x overconfident against real lines.
MARKET_MIN_PROB = 0.56

# ── Direction exposure (item 6) ────────────────────────────────────────────────
MAX_UNDERS_5LEG    = 4     # max unders on a 5-leg card
HIGH_CONF_OVERRIDE = 0.75  # waive the 4-under limit when avg confidence is this high
SIMILAR_CONF_WINDOW = 0.03 # within this prob gap, prefer OVER over UNDER

# ── Strict Sleeper mode ────────────────────────────────────────────────────────
# Sleeper API projections are NOT Pick'em app lines — they are per-game averages
# that differ from what Sleeper actually posts in its Pick'em product.  Any line
# not explicitly supplied via --lines is a proxy and must not appear in final cards.
STRICT_SLEEPER_MODE  = True  # default ON whenever line source is Sleeper
STRICT_CARD1_MIN_LEGS = 3    # verified override legs required to build Card 1
STRICT_CARD2_MIN_LEGS = 8    # verified override legs required to build Card 2

PP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Referer': 'https://app.prizepicks.com/',
    'Origin': 'https://app.prizepicks.com',
}

# ── Sleeper integration ────────────────────────────────────────────────────────

SLEEPER_STAT_MAP = {
    'pts':         'pts',
    'reb':         'reb',
    'ast':         'ast',
    'reb_ast':     'reb_ast',
    'pts_reb_ast': 'pra',
}

SLEEPER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
    'Accept': 'application/json',
}

_SLEEPER_PLAYER_CACHE = Path(__file__).parent.parent / 'data' / 'sleeper_players.json'


# ── PrizePicks integration ─────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    """Lowercase, strip accents, remove punctuation — for fuzzy name matching."""
    name = unicodedata.normalize('NFKD', name)
    name = ''.join(c for c in name if not unicodedata.combining(c))
    name = name.lower()
    name = name.replace("'", '').replace('.', '').replace('-', ' ')
    name = ' '.join(name.split())
    for suffix in (' jr', ' sr', ' ii', ' iii', ' iv'):
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
    return name


def _fetch_pp_raw() -> tuple[dict, list]:
    """
    Hit the PrizePicks projections endpoint and return (player_map, projections_list).
    player_map: {player_id_str: display_name}
    projections_list: raw 'data' array from the API response
    Returns ({}, []) on failure.
    """
    try:
        r = requests.get(
            'https://api.prizepicks.com/projections?league_id=7&per_page=1000',
            headers=PP_HEADERS, timeout=12,
        )
        if r.status_code != 200:
            return {}, []
        data = r.json()
        if 'data' not in data:
            return {}, []
    except Exception:
        return {}, []

    player_map = {
        item['id']: item['attributes'].get('display_name', '')
        for item in data.get('included', [])
        if item.get('type') == 'new_player'
    }
    return player_map, data.get('data', [])


def fetch_prizepicks_lines() -> dict:
    """
    Fetch tonight's NBA standard lines from PrizePicks.
    Returns {(normalized_player_name, stat_key): line_score} or {} on failure.

    Deduplication: when multiple standard entries exist for the same
    (player, stat) pair the one with the most recent updated_at timestamp
    is kept — it reflects the current live line.  Falls back to highest
    numeric projection ID when updated_at is absent.
    """
    player_map, projs = _fetch_pp_raw()
    if not projs:
        return {}

    # {key: (line_score, updated_at_str, proj_id_int)}
    _seen: dict = {}
    for proj in projs:
        attr = proj.get('attributes', {})
        if attr.get('odds_type') != 'standard' or attr.get('status') != 'pre_game':
            continue
        stat_raw = attr.get('stat_type', '')
        stat_key = PP_STAT_MAP.get(stat_raw)
        if not stat_key:
            continue
        rels = proj.get('relationships', {})
        pid = rels.get('new_player', {}).get('data', {}).get('id', '')
        player_name = player_map.get(pid, '')
        if not player_name:
            continue
        key = (normalize_name(player_name), stat_key)
        updated = attr.get('updated_at') or ''
        proj_id  = int(proj.get('id') or 0)
        if key not in _seen:
            _seen[key] = (float(attr['line_score']), updated, proj_id)
        else:
            cur_updated, cur_id = _seen[key][1], _seen[key][2]
            # Prefer most recently updated; break ties by highest proj ID
            if (updated > cur_updated) or (updated == cur_updated and proj_id > cur_id):
                _seen[key] = (float(attr['line_score']), updated, proj_id)

    return {k: v[0] for k, v in _seen.items()}


def debug_prizepicks_lines(name_patterns: list[str]) -> None:
    """
    Dump every projection from the PrizePicks API whose player name contains
    any of the strings in name_patterns (case-insensitive substring match).
    Prints all fields relevant to understanding which line the app is showing.

    Invoked directly:
        python scripts/generate_todays_picks.py --debug-pp "julius randle,shai gilgeous"
    Or via the CLI wrapper:
        nba picks --props-only --debug-pp "julius randle,shai gilgeous"
    """
    player_map, projs = _fetch_pp_raw()
    if not projs:
        print("  [debug-pp] API returned no data")
        return

    patterns = [p.strip().lower() for p in name_patterns]
    hits = []
    for proj in projs:
        attr  = proj.get('attributes', {})
        rels  = proj.get('relationships', {})
        pid   = rels.get('new_player', {}).get('data', {}).get('id', '')
        pname = player_map.get(pid, '')
        if not any(pat in pname.lower() for pat in patterns):
            continue
        hits.append({
            'id':             proj.get('id'),
            'player':         pname,
            'stat_type':      attr.get('stat_type'),
            'line_score':     attr.get('line_score'),
            'odds_type':      attr.get('odds_type'),       # standard / goblin / demon
            'status':         attr.get('status'),
            'start_time':     attr.get('start_time'),
            'created_at':     attr.get('created_at'),
            'updated_at':     attr.get('updated_at'),
            'projection_type': attr.get('projection_type'),
            'flash_sale_line': attr.get('flash_sale_line_score'),
            'is_promo':       attr.get('is_promo'),
            'discount_pct':   attr.get('discount_percentage'),
            'description':    attr.get('description'),      # opponent / game
            'maps_to':        PP_STAT_MAP.get(attr.get('stat_type', ''), '—'),
        })

    if not hits:
        print(f"  [debug-pp] No projections found for patterns: {patterns}")
        return

    w = 130
    print(f"\n  PP DEBUG — {len(hits)} projection(s) matching {patterns}")
    print(f"  {'─' * w}")
    hdr = f"  {'ID':<12} {'Player':<32} {'Stat':<28} {'Type':<10} {'Line':>6} {'Status':<12} {'MapsTo':<10} {'Updated':<32} {'Note'}"
    print(hdr)
    print(f"  {'─' * w}")
    for h in sorted(hits, key=lambda x: (x['player'], x['stat_type'] or '', x['odds_type'] or '')):
        note_parts = []
        if h['flash_sale_line'] is not None:
            note_parts.append(f"flash={h['flash_sale_line']}")
        if h['is_promo']:
            note_parts.append('promo')
        if h['discount_pct']:
            note_parts.append(f"disc={h['discount_pct']}%")
        note = '  '.join(note_parts)
        updated = (h['updated_at'] or '')[:19]
        print(f"  {str(h['id']):<12} {h['player']:<32} {str(h['stat_type']):<28} "
              f"{str(h['odds_type']):<10} {h['line_score']:>6} {str(h['status']):<12} "
              f"{h['maps_to']:<10} {updated:<32} {note}")
    print(f"  {'─' * w}")
    print(f"  Only 'standard' + 'pre_game' entries are used by fetch_prizepicks_lines().\n")


# ── Sleeper Pick'em integration ────────────────────────────────────────────────

def _load_sleeper_player_map() -> dict:
    """
    Returns {normalized_name: {'id': pid_str, 'team': abbr}} for active NBA players.
    Cached in data/sleeper_players.json and refreshed once daily.
    """
    cache = _SLEEPER_PLAYER_CACHE
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 24 * 3600:
        try:
            with open(cache) as f:
                return json.load(f)
        except Exception:
            pass

    try:
        r = requests.get(
            'https://api.sleeper.app/v1/players/nba',
            headers=SLEEPER_HEADERS, timeout=20,
        )
        if r.status_code != 200:
            return {}
        raw = r.json()
    except Exception:
        return {}

    name_map: dict = {}
    for pid, p in raw.items():
        if not p.get('active'):
            continue
        fn = (p.get('first_name') or '').strip()
        ln = (p.get('last_name') or '').strip()
        full = f'{fn} {ln}'.strip()
        if not full:
            continue
        norm = normalize_name(full)
        name_map[norm] = {'id': pid, 'team': p.get('team') or ''}

    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, 'w') as f:
            json.dump(name_map, f)
    except Exception:
        pass

    return name_map


def _get_sleeper_tonight_teams() -> set:
    """Return set of NBA team abbrs with games within the next 30 hours."""
    try:
        r = requests.get(
            'https://api.sleeper.com/scores/lines_game_picker',
            headers=SLEEPER_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return set()
        games = r.json()
    except Exception:
        return set()

    now_ms   = time.time() * 1000
    limit_ms = (time.time() + 30 * 3600) * 1000
    teams: set = set()
    for g in games:
        if g.get('sport') != 'nba':
            continue
        start = g.get('start_time', 0)
        if isinstance(start, (int, float)) and now_ms <= start <= limit_ms:
            m = g.get('metadata', {})
            away = m.get('away_team', {}).get('team', '')
            home = m.get('home_team', {}).get('team', '')
            if away:
                teams.add(away)
            if home:
                teams.add(home)
    return teams


def _fetch_sleeper_proj_one(pid: str) -> dict:
    """
    Fetch Sleeper per-game projection stats for one player. Returns stats dict or {}.

    Always uses season_type=regular: the 'post' variant returns RotoWire series-total
    projections aggregated over the week_shard (number of games in the series), which
    inflates lines 2× for back-to-back playoff series games and makes them appear as
    'alternate/boosted' lines vs the app. The 'regular' variant returns stable per-game
    averages that match the order of magnitude shown in the Sleeper Pick'em UI.

    NOTE: The actual Sleeper Pick'em line-setting endpoint is not publicly accessible.
    These RotoWire per-game averages are the closest proxy available without auth.
    """
    url = (f'https://api.sleeper.com/projections/nba/player/{pid}'
           f'?season_type=regular&season={_SLEEPER_API_YEAR}')
    try:
        r = requests.get(url, headers=SLEEPER_HEADERS, timeout=8)
        if r.status_code != 200:
            return {}
        data = r.json()
        if isinstance(data, list) and data:
            return data[0].get('stats', {})
        if isinstance(data, dict):
            return data.get('stats', {})
    except Exception as e:
        print(f"  [Sleeper] projection fetch failed for player {pid}: {e}", file=sys.stderr)
    return {}


def fetch_sleeper_lines(season_type: str = 'post') -> dict:
    """
    Fetch tonight's NBA player prop lines from Sleeper Pick'em.
    Returns {(normalized_player_name, stat_key): line_score} or {} on failure.

    Uses Sleeper's per-player projection endpoint; values are rounded to the nearest
    0.5 to match the lines displayed in the Sleeper Pick'em app.
    """
    tonight_teams = _get_sleeper_tonight_teams()
    if not tonight_teams:
        return {}

    player_map = _load_sleeper_player_map()
    if not player_map:
        return {}

    # Only players on teams playing tonight
    tonight_players = {
        norm: info for norm, info in player_map.items()
        if info['team'] in tonight_teams
    }
    if not tonight_players:
        return {}

    def _fetch(norm: str, pid: str):
        return norm, _fetch_sleeper_proj_one(pid)

    lines: dict = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_fetch, n, info['id']): n for n, info in tonight_players.items()}
        for fut in as_completed(futs):
            try:
                norm_name, stats = fut.result()
                if not stats:
                    continue
                for sl_key, stat_key in SLEEPER_STAT_MAP.items():
                    raw = stats.get(sl_key)
                    if raw is None:
                        continue
                    raw = float(raw)
                    if raw <= 0:
                        continue
                    # Round to nearest 0.5 (half rounds up)
                    line = math.floor(raw * 2 + 0.5) / 2
                    lines[(norm_name, stat_key)] = line
            except Exception as _e:
                norm_name = futs.get(fut, '?')
                print(f"  [Sleeper] fetch error for {norm_name}: {_e}", file=sys.stderr)

    return lines


def debug_sleeper_lines(name_patterns: list[str], season_type: str = 'post') -> None:
    """
    Dump raw Sleeper projection records for players matching name_patterns.

    Shows both season_type=regular (what fetch_sleeper_lines actually uses — per-game
    averages) and season_type=post (series totals, the old buggy source) side by side
    so you can see the difference and verify the fix is working.

    Usage:
        python scripts/generate_todays_picks.py --debug-sleeper "jalen green,ausar thompson,ayo dosunmu"
    Or via CLI wrapper:
        nba picks --debug-sleeper "jalen green,ausar thompson"
    """
    player_map = _load_sleeper_player_map()
    if not player_map:
        print("  [debug-sleeper] Could not load Sleeper player map")
        return

    patterns = [p.strip().lower() for p in name_patterns]

    # Find all players matching any pattern
    matches = {
        norm: info
        for norm, info in player_map.items()
        if any(pat in norm for pat in patterns)
    }
    if not matches:
        print(f"  [debug-sleeper] No players found matching: {patterns}")
        return

    # Markets we care about — used to highlight relevant stats rows
    WATCH_KEYS = list(SLEEPER_STAT_MAP.keys())
    w = 120

    for norm_name, info in sorted(matches.items()):
        pid  = info['id']
        team = info['team']

        print(f"\n  {'─' * w}")
        print(f"  PLAYER  : {norm_name}  (sleeper_id={pid}, team={team})")

        # Show both season_type variants so the bug / fix are clearly visible
        for st in ('regular', 'post'):
            url = (f'https://api.sleeper.com/projections/nba/player/{pid}'
                   f'?season_type={st}&season={_SLEEPER_API_YEAR}')
            in_use = '  ← FETCH_SLEEPER_LINES USES THIS' if st == 'regular' else '  ← OLD BUGGY SOURCE (series totals)'
            print(f"\n  ── season_type={st}{in_use}")
            print(f"     URL: {url}")

            try:
                r = requests.get(url, headers=SLEEPER_HEADERS, timeout=10)
                print(f"     HTTP: {r.status_code}")
                if r.status_code != 200:
                    print(f"     [error] HTTP {r.status_code}")
                    continue
                data = r.json()
            except Exception as exc:
                print(f"     [error] {exc}")
                continue

            if not data:
                print("     [empty response]")
                continue

            items = data if isinstance(data, list) else [data]
            print(f"     Records: {len(items)}")

            for i, item in enumerate(items):
                stats = item.get('stats') or {}
                meta  = {k: v for k, v in item.items() if k != 'stats'}

                flag_fields = [
                    'type', 'category', 'rank', 'odds_type', 'line_type',
                    'source', 'status', 'week', 'game_id', 'date',
                    'sport', 'season', 'season_type', 'player_id',
                    'projection_id', 'opponent', 'start_time', 'multiplier',
                    'week_shard', 'company',
                    'is_boost', 'is_promo', 'is_alt', 'boost_type',
                ]
                flag_vals = {k: meta.get(k) for k in flag_fields if k in meta}
                extra_meta = {k: v for k, v in meta.items() if k not in flag_fields and k != 'player'}

                print(f"     Record[{i}] flags: {json.dumps(flag_vals, default=str)}")
                if extra_meta:
                    print(f"             other : {json.dumps(extra_meta, default=str)[:200]}")

                # Market lines this record would produce
                for sl_key in WATCH_KEYS:
                    raw = stats.get(sl_key)
                    if raw is None:
                        continue
                    raw_f = float(raw)
                    line  = math.floor(raw_f * 2 + 0.5) / 2
                    stat_key = SLEEPER_STAT_MAP[sl_key]
                    if raw_f > 0:
                        print(f"       {sl_key:<16} raw={raw_f:>8.3f}  → line={line:>5.1f}  ({stat_key})")

                other_stats = {k: v for k, v in stats.items() if k not in WATCH_KEYS}
                if other_stats:
                    print(f"       + {len(other_stats)} other stat keys")

    print(f"\n  {'─' * w}")
    print(f"  fetch_sleeper_lines() always uses season_type=regular (per-game averages).")
    print(f"  season_type=post returns RotoWire series totals — inflated 2× on 2-game weeks.\n")


# ── Manual line overrides ─────────────────────────────────────────────────────

# Canonical display labels for stat keys — used by template and checklist output.
# REB+AST is preferred over REB_AST because parse_line_overrides accepts it directly.
_STAT_TEMPLATE_LABEL: dict[str, str] = {
    'pts':     'PTS',
    'reb':     'REB',
    'ast':     'AST',
    'pra':     'PRA',
    'reb_ast': 'REB+AST',
    'fg3m':    'FG3M',
}

# Stat aliases accepted in --lines / parse_line_overrides
_OVERRIDE_STAT_MAP: dict[str, str] = {
    'pts':     'pts',
    'reb':     'reb',
    'ast':     'ast',
    'pra':     'pra',
    'reb+ast': 'reb_ast',
    'reb_ast': 'reb_ast',
    'ra':      'reb_ast',
    'fg3m':    'fg3m',
    '3pm':     'fg3m',
}


def parse_line_overrides(text: str) -> dict:
    """
    Parse manual Sleeper Pick'em lines from a string.

    Format: comma-separated entries, each "<Player Name> <STAT> <line>"
        "Jalen Green PTS 19.5, Ausar Thompson PRA 19.5, Ayo Dosunmu PTS 19.5"

    Supported stat aliases (case-insensitive):
        PTS  REB  AST  PRA  REB+AST  REB_AST  RA  FG3M  3PM

    Returns {(normalized_player_name, stat_key): line_score}
    Prints a warning and skips any entry it cannot parse.
    """
    result: dict = {}
    for raw in text.split(','):
        entry = raw.strip()
        if not entry:
            continue
        parts = entry.rsplit(None, 2)          # split from right: [name, stat, line]
        if len(parts) != 3:
            print(f"  [--lines] Skipping — expected '<Name> <STAT> <line>': {entry!r}")
            continue
        player_str, stat_str, line_str = parts
        try:
            line = float(line_str)
        except ValueError:
            print(f"  [--lines] Invalid line value {line_str!r}: {entry!r}")
            continue
        stat_key = _OVERRIDE_STAT_MAP.get(stat_str.lower())
        if stat_key is None:
            valid = list(_OVERRIDE_STAT_MAP.keys())
            print(f"  [--lines] Unknown stat {stat_str!r} (valid: {valid}): {entry!r}")
            continue
        norm = normalize_name(player_str)
        result[(norm, stat_key)] = line
    return result


# ── Injury report ─────────────────────────────────────────────────────────────

def fetch_injury_report() -> dict:
    """
    Fetch NBA injury statuses from ESPN.
    Returns {normalized_player_name: status_string}
    e.g. {'anthony edwards': 'Day-To-Day', 'giannis antetokounmpo': 'Out'}
    """
    from src.data.errors import SourceError
    try:
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries',
            headers={'User-Agent': 'Mozilla/5.0'}, timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or not isinstance(data.get('injuries'), list):
            raise ValueError('Missing injuries array')
    except (requests.RequestException, ValueError) as exc:
        raise SourceError(str(exc), source='ESPN injuries') from exc

    from config.settings import PROJECT_ROOT
    from src.utils.slate import utc_now, revision_path
    directory = PROJECT_ROOT / 'data/injury_snapshots'
    directory.mkdir(parents=True, exist_ok=True)
    revision_path(directory / f'{slate_today().isoformat()}.json').write_text(json.dumps(
        dict(captured_at=utc_now(), source='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries',
             raw=data), indent=2))
    injured = {}
    for team_entry in data.get('injuries', []):
        for inj in team_entry.get('injuries', []):
            status = inj.get('status', '')
            name = inj.get('athlete', {}).get('displayName', '')
            if name and status:
                injured[normalize_name(name)] = status
    return injured


# ── Schedule / roster fetching ─────────────────────────────────────────────────

# Static NBA team ID map (NBA.com IDs; stable across seasons).
# Used by ESPN/Sleeper fallbacks that return abbrs but not team IDs.
_NBA_TEAM_IDS: dict[str, int] = {
    'ATL': 1610612737, 'BOS': 1610612738, 'BKN': 1610612751,
    'CHA': 1610612766, 'CHI': 1610612741, 'CLE': 1610612739,
    'DAL': 1610612742, 'DEN': 1610612743, 'DET': 1610612765,
    'GSW': 1610612744, 'HOU': 1610612745, 'IND': 1610612754,
    'LAC': 1610612746, 'LAL': 1610612747, 'MEM': 1610612763,
    'MIA': 1610612748, 'MIL': 1610612749, 'MIN': 1610612750,
    'NOP': 1610612740, 'NYK': 1610612752, 'OKC': 1610612760,
    'ORL': 1610612753, 'PHI': 1610612755, 'PHX': 1610612756,
    'POR': 1610612757, 'SAC': 1610612758, 'SAS': 1610612759,
    'TOR': 1610612761, 'UTA': 1610612762, 'WAS': 1610612764,
}


def _games_from_espn(target_date: str) -> list[dict]:
    """Build game list from ESPN scoreboard. Fallback when NBA.com returns empty."""
    try:
        from src.data.espn_odds import fetch_odds
    except ImportError:
        return []

    espn_games = fetch_odds(target_date)
    result = []
    for g in espn_games:
        if g.get('status') != 'scheduled':
            continue
        home_abbr = g['home']
        away_abbr = g['away']
        home_id = _NBA_TEAM_IDS.get(home_abbr)
        away_id = _NBA_TEAM_IDS.get(away_abbr)
        if home_id is None or away_id is None:
            print(f"  Warning: unknown abbr in ESPN data: {away_abbr} @ {home_abbr}")
            continue
        result.append({
            'game_id': g['game_id'],
            'home_team_id': home_id,
            'away_team_id': away_id,
            'home_abbr': home_abbr,
            'away_abbr': away_abbr,
        })
    return result


def _games_from_sleeper(target_date: str | None = None) -> list[dict]:
    """Build game list from Sleeper game-picker API. Last-resort fallback."""
    try:
        r = requests.get(
            'https://api.sleeper.com/scores/lines_game_picker',
            headers=SLEEPER_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return []
        raw_games = r.json()
    except Exception:
        return []

    now_ms   = time.time() * 1000
    limit_ms = (time.time() + 30 * 3600) * 1000
    result = []
    for g in raw_games:
        if g.get('sport') != 'nba':
            continue
        start = g.get('start_time', 0)
        if not (isinstance(start, (int, float)) and now_ms <= start <= limit_ms):
            continue
        if target_date and datetime.fromtimestamp(start / 1000).date().isoformat() != target_date:
            continue
        m = g.get('metadata', {})
        away_abbr = (m.get('away_team', {}).get('team', '') or '').upper()
        home_abbr = (m.get('home_team', {}).get('team', '') or '').upper()
        if not away_abbr or not home_abbr:
            continue
        home_id = _NBA_TEAM_IDS.get(home_abbr)
        away_id = _NBA_TEAM_IDS.get(away_abbr)
        if home_id is None or away_id is None:
            print(f"  Warning: unknown abbr in Sleeper data: {away_abbr} @ {home_abbr}")
            continue
        result.append({
            'game_id': str(g.get('game_id', f'{away_abbr}{home_abbr}')),
            'home_team_id': home_id,
            'away_team_id': away_id,
            'home_abbr': home_abbr,
            'away_abbr': away_abbr,
        })
    return result


def get_todays_games(target_date: str, allow_sleeper: bool = False) -> list[dict]:
    """Fetch scheduled games for target_date ('YYYY-MM-DD').

    Primary: NBA.com ScoreboardV3.
    Fallback 1: ESPN scoreboard (canonical abbrs + static team ID map).
    Optional fallback: Sleeper game-picker, only when explicitly enabled.
    """
    from nba_api.stats.endpoints import scoreboardv3

    dt = datetime.strptime(target_date, "%Y-%m-%d")
    nba_date = dt.strftime("%m/%d/%Y")

    time.sleep(NBA_API_DELAY)
    nba_games: list[dict] = []
    nba_schedule_found = False
    try:
        board = scoreboardv3.ScoreboardV3(game_date=nba_date, league_id='00')
        dfs = board.get_data_frames()
        games_df = dfs[1]   # one row per game (gameId, gameCode, gameStatusText, …)
        teams_df = dfs[2]   # two rows per game (gameId, teamId, teamTricode, …)

        if not games_df.empty:
            nba_schedule_found = True
            team_lookup: dict[tuple[str, str], int] = {}
            for _, row in teams_df.iterrows():
                team_lookup[(str(row['gameId']), str(row['teamTricode']))] = int(row['teamId'])

            for _, row in games_df.iterrows():
                if int(row.get('gameStatus', 1)) != 1:
                    continue
                game_id = str(row['gameId'])
                game_code = row.get('gameCode', '')
                try:
                    matchup = game_code.split('/')[1]   # e.g. 'CHAORL'
                    away_abbr = matchup[:3]             # 'CHA'
                    home_abbr = matchup[3:]             # 'ORL'
                except (IndexError, AttributeError):
                    continue

                home_id = team_lookup.get((game_id, home_abbr))
                away_id = team_lookup.get((game_id, away_abbr))
                if home_id is None or away_id is None:
                    continue

                nba_games.append({
                    'game_id': game_id,
                    'home_team_id': home_id,
                    'away_team_id': away_id,
                    'home_abbr': home_abbr,
                    'away_abbr': away_abbr,
                })
    except Exception as e:
        print(f"  Error fetching NBA.com schedule: {e}")

    if nba_schedule_found:
        return nba_games

    print("  *** NBA.com returned 0 games — trying ESPN schedule fallback... ***")
    espn_games = _games_from_espn(target_date)
    if espn_games:
        print(f"  ESPN fallback found {len(espn_games)} game(s).")
        return espn_games

    if allow_sleeper:
        print("  *** ESPN returned 0 games — trying Sleeper game-picker fallback... ***")
        sleeper_games = _games_from_sleeper(target_date)
        if sleeper_games:
            print(f"  Sleeper fallback found {len(sleeper_games)} game(s).")
            return sleeper_games

    print("  *** WARNING: No upcoming games found from schedule sources. ***")
    print("  *** If games are scheduled today, check network/API connectivity. ***")
    return []


def detect_nba_game_type(target_date: str) -> str:
    """
    Query the NBA scoreboard to determine whether games on target_date are
    'PlayIn', 'Playoffs', or 'Regular Season'.

    Uses ScoreboardV3 game metadata:
      - gameLabel contains 'play-in'  → PlayIn
      - poRoundDesc is non-empty      → Playoffs
      - neither                       → Regular Season

    Returns one of: 'PlayIn', 'Playoffs', 'Regular Season'
    If the API call fails, returns '' (caller should not auto-correct).
    """
    from nba_api.stats.endpoints import scoreboardv3

    dt = datetime.strptime(target_date, "%Y-%m-%d")
    nba_date = dt.strftime("%m/%d/%Y")

    time.sleep(NBA_API_DELAY)
    try:
        board = scoreboardv3.ScoreboardV3(game_date=nba_date, league_id='00')
        data = board.get_dict()
        games = data.get('scoreboard', {}).get('games', [])
    except Exception:
        return ''

    if not games:
        return ''

    for game in games:
        label = game.get('gameLabel', '') or ''
        po_round = game.get('poRoundDesc', '') or ''
        if 'play-in' in label.lower():
            return 'PlayIn'
        if po_round:
            return 'Playoffs'

    return 'Regular Season'


def get_roster_player_ids(team_id: int, season: str, target_date: str | None = None) -> list[int]:
    from nba_api.stats.endpoints import commonteamroster
    time.sleep(NBA_API_DELAY)
    try:
        roster = commonteamroster.CommonTeamRoster(team_id=team_id, season=season)
        df = roster.get_data_frames()[0]
        return [int(pid) for pid in df['PLAYER_ID'].tolist()]
    except Exception as e:
        print(f"  Warning: could not fetch roster for team {team_id}: {e}")

    # API failed — recover from DB using only the team's last 10 game dates.
    # Scoping to recent games prevents traded/waived players (who played earlier
    # in the season) from leaking into today's candidate pool.
    team_abbr = next((a for a, tid in _NBA_TEAM_IDS.items() if tid == team_id), None)
    if team_abbr:
        try:
            conn = sqlite3.connect(str(DB_PATH))
            rows = conn.execute(
                """
                SELECT DISTINCT player_id FROM game_logs gl
                WHERE team_abbr = ? AND season = ? AND game_date < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM game_logs newer WHERE newer.player_id=gl.player_id
                      AND newer.game_date>gl.game_date AND newer.game_date < ?
                  )
                  AND game_date IN (
                      SELECT DISTINCT game_date FROM game_logs
                      WHERE team_abbr = ? AND season = ? AND game_date < ?
                      ORDER BY game_date DESC
                      LIMIT 10
                  )
                ORDER BY game_date DESC
                """,
                (team_abbr, season, target_date or slate_today().isoformat(),
                 target_date or slate_today().isoformat(), team_abbr, season,
                 target_date or slate_today().isoformat()),
            ).fetchall()
            conn.close()
            if rows:
                print(f"  Roster fallback: loaded {len(rows)} player_ids for {team_abbr} from DB (last 10 games)")
                return [int(r[0]) for r in rows]
        except Exception as db_e:
            print(f"  Roster DB fallback also failed for {team_abbr}: {db_e}")
    return []


def get_todays_player_contexts(target_date: str, season: str) -> list[dict]:
    games = get_todays_games(target_date)
    if not games:
        print("  No games found for this date.")
        return []

    print(f"  Found {len(games)} game(s) on {target_date}")
    contexts = []
    for game in games:
        label = f"{game['away_abbr']} @ {game['home_abbr']}"
        print(f"    {label}...", end="", flush=True)
        home_ids = get_roster_player_ids(game['home_team_id'], season, target_date)
        away_ids = get_roster_player_ids(game['away_team_id'], season, target_date)
        for pid in home_ids:
            contexts.append({
                'player_id': pid,
                'matchup': f"{game['home_abbr']} vs. {game['away_abbr']}",
                'team_abbr': game['home_abbr'],
                'opp_abbr': game['away_abbr'],
                'is_home': 1,
                'game_id': game.get('game_id', ''),
            })
        for pid in away_ids:
            contexts.append({
                'player_id': pid,
                'matchup': f"{game['away_abbr']} @ {game['home_abbr']}",
                'team_abbr': game['away_abbr'],
                'opp_abbr': game['home_abbr'],
                'is_home': 0,
                'game_id': game.get('game_id', ''),
            })
        print(f" {len(home_ids) + len(away_ids)} players")
    return contexts


# ── Opponent context from DB ───────────────────────────────────────────────────

def get_opponent_def_stats(opp_abbr: str, before_date: str, window: int = DEF_WINDOW) -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    query = """
        SELECT game_id,
               SUM(pts)  AS pts_allowed,
               SUM(reb)  AS reb_allowed,
               SUM(ast)  AS ast_allowed,
               SUM(fg3m) AS fg3m_allowed
        FROM game_logs
        WHERE opp_abbr = ? AND game_date < ?
        GROUP BY game_id
        ORDER BY game_id DESC
        LIMIT ?
    """
    df = pd.read_sql(query, conn, params=(opp_abbr, before_date, window))
    conn.close()
    if df.empty:
        return {}
    return {
        f'pts_allowed_L{window}':  df['pts_allowed'].mean(),
        f'reb_allowed_L{window}':  df['reb_allowed'].mean(),
        f'ast_allowed_L{window}':  df['ast_allowed'].mean(),
        f'fg3m_allowed_L{window}': df['fg3m_allowed'].mean(),
    }


def get_team_pace(team_abbr: str, before_date: str, window: int = DEF_WINDOW) -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    query = """
        SELECT game_id,
               SUM(fga) AS tfga,
               SUM(tov) AS ttov,
               SUM(fta) AS tfta
        FROM game_logs
        WHERE team_abbr = ? AND game_date < ?
        GROUP BY game_id
        ORDER BY game_id DESC
        LIMIT ?
    """
    df = pd.read_sql(query, conn, params=(team_abbr, before_date, window))
    conn.close()
    if df.empty:
        return {}
    df['est_poss'] = df['tfga'] + df['ttov'] + 0.44 * df['tfta']
    return {f'pace_L{window}': df['est_poss'].mean()}


# ── Feature construction ───────────────────────────────────────────────────────

def current_player_features(player_ids, target_date):
    """Compute today's rolling features using every eligible game through yesterday.

    Stored feature rows are shifted for their historical game; copying the last
    row omits that game's actuals. A placeholder prediction row advances the window.
    """
    from src.features.player_features import build_player_features
    if not player_ids:
        return {}
    placeholders = ','.join('?' for _ in player_ids)
    with sqlite3.connect(str(DB_PATH)) as conn:
        history = pd.read_sql(
            f"SELECT * FROM game_logs WHERE player_id IN ({placeholders}) "
            "AND game_date < ? AND min >= 12 ORDER BY player_id, game_date",
            conn, params=list(player_ids) + [target_date])
    if history.empty:
        return {}
    history['game_date'] = pd.to_datetime(history['game_date'])
    # Twenty past observations cover the largest rolling window.
    history = history.groupby('player_id', group_keys=False).tail(20)
    future = history.sort_values('game_date').groupby('player_id').tail(1).copy()
    future['game_date'] = pd.to_datetime(target_date)
    raw_columns = set(history.columns)
    calculated = build_player_features(pd.concat([history, future], ignore_index=True))
    engineered = [c for c in calculated if c not in raw_columns]
    return calculated[calculated['game_date'] == pd.to_datetime(target_date)].set_index(
        'player_id')[engineered].to_dict(orient='index')


def build_todays_features(features_df: pd.DataFrame,
                           player_contexts: list[dict],
                           target_date: str,
                           is_playoffs: bool = False,
                           season: str | None = None) -> pd.DataFrame:
    # A player assigned to two teams/games is an ambiguous roster fallback,
    # never a last-row-wins join.
    context_keys = [(c['player_id'], c['team_abbr'], c['opp_abbr'], c['is_home'])
                    for c in player_contexts]
    if len({k[0] for k in context_keys}) != len(context_keys):
        raise ValueError('Ambiguous or duplicate player roster contexts')
    features_df = features_df.copy()
    features_df['game_date'] = pd.to_datetime(features_df['game_date'])
    target_dt = pd.to_datetime(target_date)

    recent_df = (
        features_df[features_df['game_date'] < target_dt]
        .sort_values('game_date')
        .groupby('player_id')
        .tail(1)
        .reset_index()
    )
    recent_map = recent_df.set_index('player_id').to_dict(orient='index')
    current = current_player_features(sorted({c['player_id'] for c in player_contexts}), target_date)

    # Pre-index playoff game logs for series context lookups (only needed in playoff mode).
    # Scoped to the current season so prior-year matchups against the same opponent don't
    # inflate series_game_num (e.g. LeBron vs DEN had 9 cross-season games — without the
    # season filter his series_game_num for game 1 of 2025-26 playoffs would be 10).
    if is_playoffs and 'season_type' in features_df.columns and season:
        playoff_logs = features_df[
            (features_df['season_type'] == 'Playoffs') &
            (features_df['season'] == season) &
            (features_df['game_date'] < target_dt)
        ].sort_values('game_date')
    else:
        playoff_logs = pd.DataFrame()

    def_cache: dict = {}
    pace_cache: dict = {}
    rows = []

    for ctx in player_contexts:
        pid = ctx['player_id']
        if pid not in recent_map:
            continue
        row = dict(recent_map[pid])
        if pid not in current:
            continue
        row.update(current[pid])
        row['player_id'] = pid

        min_l10 = row.get('min_L10') or 0
        if min_l10 < MIN_MINUTES:
            continue

        # min_L10 is a rolling average that can reflect regular season minutes
        # for players not in the playoff rotation. Guard against this by requiring
        # at least one playoff appearance this season once playoff data exists.
        if is_playoffs and not playoff_logs.empty:
            if not (playoff_logs['player_id'] == pid).any():
                continue

        last_game_date = row['game_date']
        days_rest = (target_dt - last_game_date).days
        row['days_rest'] = min(int(days_rest), 10)
        row['is_b2b'] = int(days_rest == 1)
        row['game_month'] = target_dt.month
        row['is_home'] = ctx['is_home']
        row['matchup'] = ctx['matchup']
        row['opp_abbr'] = ctx['opp_abbr']
        row['team_abbr'] = ctx['team_abbr']
        row['game_date'] = target_dt
        row['game_id'] = str(ctx.get('game_id', ''))
        row['is_playoff'] = int(is_playoffs)
        if not is_playoffs:
            row['playoff_round'] = 0
            row['series_game_num'] = 1
            row['series_team_wins'] = row['series_opp_wins'] = 0
        if season:
            row['season'] = season
            with sqlite3.connect(str(DB_PATH)) as conn:
                row['season_game_num'] = 1 + conn.execute(
                    'SELECT COUNT(DISTINCT game_id) FROM game_logs '
                    'WHERE player_id=? AND season=? AND game_date<? AND min>=12',
                    (pid, season, target_date)).fetchone()[0]

        opp = ctx['opp_abbr']
        if opp not in def_cache:
            def_cache[opp] = get_opponent_def_stats(opp, target_date)
        row.update(def_cache[opp])

        team = ctx['team_abbr']
        if team not in pace_cache:
            pace_cache[team] = get_team_pace(team, target_date)
        # Training pace_L15 belongs to the opponent; own_pace_L15 is separate.
        if opp not in pace_cache:
            pace_cache[opp] = get_team_pace(opp, target_date)
        row.update(pace_cache[opp])
        row.update({f'own_{k}': v for k, v in pace_cache[team].items()})

        # Playoff round from game_id (position 7: 1=R1, 2=CSF, 3=CF, 4=Finals).
        # ESPN/Sleeper fallbacks return their own event IDs (not NBA format) so
        # position [7] is meaningless there — resolve by finding a valid NBA game_id
        # for this series from (1) features parquet, (2) DB for this matchup,
        # (3) DB for any recent playoff game (all series in a phase share the same round).
        game_id_str = ctx.get('game_id', '')
        if is_playoffs:
            is_valid_nba_id = len(game_id_str) == 10 and game_id_str.startswith('004')
            if not is_valid_nba_id:
                resolved_id = ''
                # 1. Features parquet: look for prior games in this series
                if not playoff_logs.empty and 'game_id' in playoff_logs.columns:
                    opp = ctx['opp_abbr']
                    team = ctx['team_abbr']
                    series_gids = playoff_logs[
                        (playoff_logs['team_abbr'] == team) &
                        (playoff_logs['opp_abbr'] == opp) &
                        (playoff_logs['game_id'].astype(str).str.startswith('004'))
                    ]['game_id']
                    if not series_gids.empty:
                        resolved_id = str(series_gids.iloc[-1])

                # 2. DB: this specific matchup (Game 1 of a new series)
                if not resolved_id:
                    try:
                        conn = sqlite3.connect(str(DB_PATH))
                        db_row = conn.execute(
                            """
                            SELECT game_id FROM game_logs
                            WHERE team_abbr = ? AND opp_abbr = ?
                              AND season_type = 'Playoffs'
                              AND game_id LIKE '004%'
                            ORDER BY game_date DESC LIMIT 1
                            """,
                            (ctx['team_abbr'], ctx['opp_abbr']),
                        ).fetchone()
                        conn.close()
                        if db_row:
                            resolved_id = str(db_row[0])
                    except Exception:
                        pass

                # 3. DB: any recent playoff game — all concurrent series share the round
                if not resolved_id:
                    try:
                        conn = sqlite3.connect(str(DB_PATH))
                        db_row = conn.execute(
                            """
                            SELECT game_id FROM game_logs
                            WHERE season_type = 'Playoffs' AND game_id LIKE '004%'
                            ORDER BY game_date DESC LIMIT 1
                            """
                        ).fetchone()
                        conn.close()
                        if db_row:
                            resolved_id = str(db_row[0])
                    except Exception:
                        pass

                if resolved_id:
                    game_id_str = resolved_id
            row['playoff_round'] = get_playoff_round(game_id_str)
        else:
            row['playoff_round'] = 0

        # Playoff series context: compute series_game_num and series_L3 features
        # from actual prior games in this series, not the stale parquet row values.
        if is_playoffs and not playoff_logs.empty:
            series_games = playoff_logs[
                (playoff_logs['player_id'] == pid) &
                (playoff_logs['opp_abbr'] == opp)
            ]
            row['is_playoff'] = 1
            row['series_game_num'] = len(series_games) + 1

            # Team-level series wins/losses for elimination game detection.
            # Use distinct game_ids to avoid duplicating player rows.
            if 'wl' in playoff_logs.columns and 'team_abbr' in playoff_logs.columns:
                team = ctx['team_abbr']
                team_series_log = playoff_logs[
                    (playoff_logs['team_abbr'] == team) &
                    (playoff_logs['opp_abbr'] == opp)
                ][['game_id', 'wl']].drop_duplicates('game_id')
                row['series_team_wins'] = int((team_series_log['wl'] == 'W').sum())
                row['series_opp_wins']  = int((team_series_log['wl'] == 'L').sum())
            else:
                row['series_team_wins'] = 0
                row['series_opp_wins']  = 0

            for stat in ['pts', 'reb', 'ast']:
                col = f'{stat}_series_L3'
                if not series_games.empty and stat in series_games.columns:
                    row[col] = series_games[stat].tail(3).mean()
                else:
                    row[col] = float('nan')
            # pra = pts + reb + ast composite
            if not series_games.empty and all(c in series_games.columns for c in ['pts', 'reb', 'ast']):
                row['pra_series_L3'] = (
                    series_games[['pts', 'reb', 'ast']].sum(axis=1).tail(3).mean()
                )
            else:
                row['pra_series_L3'] = float('nan')
        elif is_playoffs:
            row['is_playoff'] = 1
            row['series_game_num'] = 1
            row['series_team_wins'] = 0
            row['series_opp_wins']  = 0
            for col in ['pts_series_L3', 'reb_series_L3', 'ast_series_L3', 'pra_series_L3']:
                row[col] = float('nan')

        # NaN guard for series_L3 features — model requires them; fill with L10 when no
        # series context exists (Game 1, non-playoff mode, or first career playoff game).
        # series_game_num also defaults to 1 for non-playoff games.
        for col, l10_col in [('pts_series_L3', 'pts_L10'),
                              ('reb_series_L3', 'reb_L10'),
                              ('ast_series_L3', 'ast_L10'),
                              ('pra_series_L3', 'pra_L10')]:
            if pd.isna(row.get(col)):
                row[col] = row.get(l10_col, float('nan'))
        if pd.isna(row.get('series_game_num')):
            row['series_game_num'] = 1

        rows.append(row)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).reset_index(drop=True)


# ── Model loading and prediction ───────────────────────────────────────────────
# load_model()/prob_over() live in src.utils.probability (shared across scripts)


def predict_stat_raw(df: pd.DataFrame, stat: str,
                     model_cache: dict | None = None) -> pd.Series:
    """
    Raw model prediction (no L10 blend) — used when comparing to real lines.
    model_cache: optional pre-loaded {stat: model_data} dict to avoid repeated
    disk reads when scoring all stats in a single run.
    """
    def _get(s):
        if model_cache and s in model_cache:
            return model_cache[s]
        return load_model(s)

    def required_columns(model):
        missing = set(model['feature_cols']) - set(df.columns)
        if missing:
            raise ValueError(f'Missing inference features: {sorted(missing)}')
        return model['feature_cols']

    components = COMPOSITE_STATS.get(stat)
    if components:
        models = {c: _get(c) for c in components}
        valid = pd.Series(True, index=df.index)
        for m in models.values():
            cols = required_columns(m)
            valid &= df[cols].notna().all(axis=1)
        preds = pd.Series(0.0, index=df.index)
        if valid.sum() > 0:
            for m in models.values():
                cols = required_columns(m)
                preds.loc[valid] += m['model'].predict(df.loc[valid, cols])
        preds[~valid] = np.nan
    else:
        m = _get(stat)
        cols = required_columns(m)
        valid = df[cols].notna().all(axis=1)
        preds = pd.Series(np.nan, index=df.index)
        if valid.sum() > 0:
            preds.loc[valid] = m['model'].predict(df.loc[valid, cols])
    return preds


# ── Candidate generation ───────────────────────────────────────────────────────

def get_rotation_tightening_teams(target_date: str) -> set[str]:
    """
    Return set of team abbrs whose last game had a tighter rotation than their
    playoff average. Signal: last game's ≥15-min player count < season PO avg - 1.

    Backtest finding: when rotation tightens, hit rate improves 3-7pp (62.5-66.5%
    vs 53.9-59.1%). Used as a display annotation only — does not alter probabilities.
    """
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute("""
            WITH last_game AS (
                SELECT team_abbr, MAX(game_date) AS last_date
                FROM game_logs
                WHERE game_date < ? AND min >= 15
                GROUP BY team_abbr
            ),
            last_count AS (
                SELECT g.team_abbr, COUNT(DISTINCT g.player_id) AS n
                FROM game_logs g
                JOIN last_game lg ON g.team_abbr = lg.team_abbr
                                  AND g.game_date = lg.last_date
                WHERE g.min >= 15
                GROUP BY g.team_abbr
            ),
            po_avg AS (
                SELECT team_abbr, AVG(cnt) AS avg_n
                FROM (
                    SELECT team_abbr, game_id, COUNT(DISTINCT player_id) AS cnt
                    FROM game_logs
                    WHERE min >= 15 AND season_type = 'Playoffs'
                    GROUP BY team_abbr, game_id
                )
                GROUP BY team_abbr
            )
            SELECT lc.team_abbr
            FROM last_count lc
            JOIN po_avg pa ON lc.team_abbr = pa.team_abbr
            WHERE lc.n <= pa.avg_n - 1.0
        """, (target_date,)).fetchall()
    except Exception:
        return set()
    finally:
        conn.close()
    return {r[0] for r in rows}


def build_candidates(df: pd.DataFrame, pp_lines: dict,
                     injury_report: dict,
                     is_playoffs: bool = False,
                     is_play_in: bool = False,
                     playoff_ratios: dict | None = None,
                     line_source: str = 'sleeper',
                     override_keys: set | None = None,
                     rotation_tight_teams: set | None = None,
                     book_props: dict | None = None) -> pd.DataFrame:
    """
    Score every (player, stat) pair. Returns OVER and UNDER candidates.

    Filters:
      - Players listed as 'Out' in injury report are removed entirely.
      - Players with 'Day-To-Day' or 'Doubtful' are flagged but kept.
      - When pp_lines is populated: only surfaces picks with a real PP line.
      - Falls back to L10 baseline if pp_lines is empty.

    Playoff mode (is_playoffs=True):
      - Applies tier-based prediction and variance adjustments.
      - Round-progressive thresholds: tighter floors in later rounds (R1→Finals).
      - Finals/CF extra prediction discounts (model over-predicts most in Finals).
      - Elimination game std expansion when series score is 3-x.
      - Suppresses high-risk rotation plays (DNP/minute-cut signals).
      - Labels each pick with its playoff tier, round, and adjustment info.

    rotation_tight_teams: set of team abbrs where last game had fewer active
      players than playoff average — added as annotation to playoff_note.

    book_props: {(normalized name, stat): {'line', 'p_over', 'n_books'}} from
      src.data.espn_props.fetch_book_props. When given (market-anchored mode),
      a leg's prob is the market+model blend at the pick'em line, legs with no
      book line are dropped, and MARKET_MIN_PROB replaces the model-only floors.
      None = legacy model-only probabilities.

    Returns a DataFrame with a 'direction' column: 'OVER' or 'UNDER'.
    """
    from src.models import market_blend
    market_mode = book_props is not None
    using_real_lines = bool(pp_lines)
    rows = []

    # Pre-load all models once — avoids repeated pickle reads (8 files per run)
    base_stats = list({c for stat in STATS for c in (COMPOSITE_STATS.get(stat) or [stat])})
    model_cache = {s: load_model(s) for s in base_stats}

    out_statuses = {'out', 'out (injury)', 'inactive', 'did not play'}
    warn_statuses = {'day-to-day', 'doubtful', 'questionable'}

    for stat in STATS:
        l10_col = f'{stat}_L10'
        std_col = f'{stat}_std_L10'
        if l10_col not in df.columns:
            continue

        base_min_p = STAT_MIN_PROB_PLAYOFFS[stat] if is_playoffs else STAT_MIN_PROB[stat]
        preds = predict_stat_raw(df, stat, model_cache=model_cache)

        for idx, row in df.iterrows():
            pred_raw = preds.get(idx, np.nan)
            l10_val = row.get(l10_col)

            if not pd.isna(pred_raw) and not pd.isna(l10_val) and float(l10_val) > 0:
                # Unified L10/model blend (config.BLEND_L10_WEIGHT), applied in both
                # regular-season and playoff mode. Empirically derived — see
                # config/settings.py's BLEND_L10_WEIGHT comment for the derivation.
                pred = BLEND_L10_WEIGHT * float(l10_val) + (1 - BLEND_L10_WEIGHT) * float(pred_raw)
            else:
                pred = pred_raw

            if pd.isna(pred):
                continue

            player_name = row.get('full_name', '')
            norm_name = normalize_name(player_name)

            # Injury filter
            inj_status = injury_report.get(norm_name, '')
            if any(s in inj_status.lower() for s in out_statuses):
                continue  # player is out — skip entirely
            inj_flag = '⚠️ ' if any(s in inj_status.lower() for s in warn_statuses) else ''

            pp_key = (norm_name, stat)
            if using_real_lines:
                if pp_key not in pp_lines:
                    continue
                line = pp_lines[pp_key]
            else:
                line = row.get(l10_col)
                if pd.isna(line) or float(line) == 0:
                    continue

            std_val = row.get(std_col, np.nan)
            std_float = float('nan') if pd.isna(std_val) else float(std_val)

            # Extract round and elimination context from row (set in build_todays_features)
            playoff_round = int(row.get('playoff_round', 0) or 0)
            team_wins = int(row.get('series_team_wins', 0) or 0)
            opp_wins  = int(row.get('series_opp_wins',  0) or 0)
            is_elim_game = max(team_wins, opp_wins) == 3

            # ── Playoff adjustment layer ───────────────────────────────────────
            playoff_note = ''
            tier = None
            if is_playoffs:
                adj_pred, adj_std, tier, pred_mult, std_mult = apply_playoff_adjustments(
                    float(pred), std_float, stat, row, playoff_ratios,
                    play_in=is_play_in,
                    playoff_round=playoff_round,
                    elimination_game=is_elim_game,
                )
                # Suppress high-risk rotation plays
                if should_suppress(row, tier):
                    continue
                has_history = bool(playoff_ratios and int(row.get('player_id', -1)) in playoff_ratios)
                playoff_note = playoff_context_note(tier, pred_mult, std_mult, False, has_history)

                # Annotate rotation tightening signal
                team_abbr = str(row.get('team_abbr', ''))
                if rotation_tight_teams and team_abbr in rotation_tight_teams:
                    playoff_note = (playoff_note + ' | ↑rot-tight') if playoff_note else '↑rot-tight'

                # Annotate elimination game
                if is_elim_game:
                    playoff_note = (playoff_note + ' | elim') if playoff_note else 'elim'

                pred = adj_pred
                std_float = adj_std
            # ──────────────────────────────────────────────────────────────────

            p_over = prob_over(float(pred), float(line), std_float)
            model_p_over = p_over
            mkt_p_over = np.nan
            book = None
            if market_mode:
                book = book_props.get((norm_name, stat))
                if book is None or not market_blend.has_params(stat):
                    continue   # no sharp reference → no trustworthy probability
                mkt_p_over = market_blend.shift_line(stat, book['line'], book['p_over'], float(line))
                p_over = market_blend.blend(stat, mkt_p_over, model_p_over)
            p_under = 1.0 - p_over

            # Round-progressive floor: later rounds require higher confidence.
            # Effective floor = max(stat floor, round floor).
            min_p = max(base_min_p, ROUND_PROB_FLOORS.get(playoff_round, base_min_p)) if is_playoffs else base_min_p

            # Tier-aware gate (playoffs only): raise min_p for ROTATION picks.
            # Effective threshold = max(stat-level floor, tier floor, stat×tier override).
            if is_playoffs and tier is not None:
                min_p = max(min_p, TIER_MIN_PROB_PLAYOFFS.get(tier, 0.55))
                # Stat×tier override (e.g. STAR PTS requires P≥70%: non-monotone below)
                stat_tier_key = (tier, stat)
                min_p = max(min_p, STAT_TIER_MIN_PROB_PLAYOFFS.get(stat_tier_key, 0.0))

            if market_mode:
                min_p = MARKET_MIN_PROB

            is_dtd = bool(inj_flag)
            final_std = std_float if not np.isnan(std_float) else max(abs(float(pred)) * 0.2, 1.0)
            base = {
                'player_id':    row['player_id'],
                'player':       inj_flag + player_name,
                'player_name':  player_name,              # clean name, no flag
                'team':         row.get('team_abbr', ''),
                'opp':          row.get('opp_abbr', ''),
                'game':         _canonical_game(row.get('matchup', 'UNK')),
                'stat':         stat.upper(),
                'stat_key':     stat,                     # lowercase for DB lookup
                'line':         round(float(line), 1),
                'pred':         round(float(pred), 1),
                'std':          round(float(final_std), 3),
                'line_source':  (
                    'sleeper_override' if (override_keys and pp_key in override_keys)
                    else (line_source if using_real_lines else 'l10_avg')
                ),
                'injury_flag':  inj_status,
                'playoff_note': playoff_note,
                'is_dtd':       is_dtd,
                'model_p_over': round(float(model_p_over), 4),
                'mkt_p_over':   round(float(mkt_p_over), 4) if market_mode else np.nan,
                'book_line':    book['line'] if book else np.nan,
                'book_p_over':  book['p_over'] if book else np.nan,
                'n_books':      book['n_books'] if book else 0,
            }

            buf_gap = EDGE_BUFFER_GAP.get(stat, 0.0)
            # Model-gap buffer is a model-only heuristic; in market mode the blend
            # already discounts model disagreement, so the probability floor suffices.
            over_edge_ok  = market_mode or (float(pred) - float(line) >= buf_gap) or (p_over  >= MIN_EDGE_PROB)
            under_edge_ok = market_mode or (float(line) - float(pred) >= buf_gap) or (p_under >= MIN_EDGE_PROB)

            if p_over >= min_p and over_edge_ok:
                rows.append({**base, 'direction': 'OVER', 'prob': p_over})

            # UNDER pick: model thinks player will fall well short of the line
            if p_under >= min_p and under_edge_ok:
                rows.append({**base, 'direction': 'UNDER', 'prob': p_under})

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values('prob', ascending=False).reset_index(drop=True)


# ── Card builder ───────────────────────────────────────────────────────────────

def _canonical_game(matchup: str) -> str:
    """Return a stable game key regardless of home/away perspective.

    'MIN @ SAS' and 'SAS vs. MIN' both become 'MIN_SAS' so the max-2/game
    limit correctly treats them as the same physical game.
    """
    for sep in (' @ ', ' vs. ', ' vs '):
        if sep in matchup:
            teams = sorted(t.strip() for t in matchup.split(sep, 1))
            return '_'.join(teams)
    return matchup


def build_card(candidates: pd.DataFrame, n_legs: int, exclude_keys: set,
               safe_only: bool = True, strict_override: bool = False,
               exclude_players: set | None = None) -> list[dict]:
    """
    Greedy highest-confidence selection with:
      - One prop per player, max 2/game, max 2/stat-type
      - OVER preference within SIMILAR_CONF_WINDOW (direction exposure control)
      - Max MAX_UNDERS_5LEG unders on 5-leg cards unless avg conf >= HIGH_CONF_OVERRIDE
      - safe_only=True (default): skip DTD/Doubtful players
      - strict_override=True: only use legs with line_source='sleeper_override'
    """
    if candidates.empty:
        return []

    used_players: set = set()
    game_counts: dict = {}
    stat_counts: dict = {}
    card: list[dict] = []
    under_count = 0

    # Re-sort with a tiny OVER bonus so OVERs beat UNDERs within SIMILAR_CONF_WINDOW
    working = candidates.copy()
    if 'direction' in working.columns:
        bonus = working['direction'].map({'OVER': SIMILAR_CONF_WINDOW / 2, 'UNDER': 0.0}).fillna(0.0)
        order = (working['prob'] + bonus).argsort()[::-1].values
        working = working.iloc[order].reset_index(drop=True)

    _excl_players = exclude_players or set()

    def _basic_ok(r) -> bool:
        if strict_override and r.get('line_source') != 'sleeper_override':
            return False
        if safe_only and r.get('is_dtd', False):
            return False
        if (r['player_id'], r['stat'], r['direction']) in exclude_keys:
            return False
        if r['player_id'] in _excl_players:
            return False
        if r['player_id'] in used_players:
            return False
        if game_counts.get(r['game'], 0) >= 2:
            return False
        if stat_counts.get(r['stat'], 0) >= 2:
            return False
        return True

    def _under_ok(r) -> bool:
        if n_legs == 5 and r['direction'] == 'UNDER' and under_count >= MAX_UNDERS_5LEG:
            avg_conf = sum(c['prob'] for c in card) / len(card) if card else 0.0
            return avg_conf >= HIGH_CONF_OVERRIDE
        return True

    for _, row in working.iterrows():
        if not _basic_ok(row) or not _under_ok(row):
            continue

        game = row['game']
        game_cnt = game_counts.get(game, 0)

        # Soft correlation penalty: prefer different-game alternative within 3%
        if game_cnt == 1 and len(card) >= 2:
            has_diff_alt = False
            for _, alt in working.iterrows():
                if (alt['game'] != game and
                        _basic_ok(alt) and
                        alt['prob'] >= row['prob'] - 0.03):
                    has_diff_alt = True
                    break
            if has_diff_alt:
                continue

        card.append(row.to_dict())
        used_players.add(row['player_id'])
        game_counts[game] = game_cnt + 1
        stat_counts[row['stat']] = stat_counts.get(row['stat'], 0) + 1
        if row['direction'] == 'UNDER':
            under_count += 1
        if len(card) == n_legs:
            break

    # Fallback: fill remaining slots without soft constraints
    if len(card) < n_legs:
        for _, row in working.iterrows():
            if len(card) == n_legs:
                break
            if not _basic_ok(row):
                continue
            card.append(row.to_dict())
            used_players.add(row['player_id'])
            game_counts[row['game']] = game_counts.get(row['game'], 0) + 1
            stat_counts[row['stat']] = stat_counts.get(row['stat'], 0) + 1
            if row['direction'] == 'UNDER':
                under_count += 1

    return card


# ── Output ─────────────────────────────────────────────────────────────────────

def print_ranked_board(candidates: pd.DataFrame, top_n: int = 20,
                        is_playoffs: bool = False,
                        strict_mode: bool = False) -> None:
    """Print the full ranked prop board before combo selection."""
    if candidates.empty:
        return
    w = 100 if strict_mode else 84
    label = "RANKED PROP BOARD (all qualifying legs, sorted by confidence)"
    if strict_mode:
        label += "  — ✅ = verified override  ⚠ = proxy / not actionable"
    print(f"\n  {label}")
    col_hdr = 'Context' if is_playoffs else 'Game'
    if strict_mode:
        print(f"  {'#':<3} {'Player':<26} {'Stat':<9} {'Dir':<5} {'Line':>6} {'Model':>6} {'P':>6}  {'Source':<22}  {col_hdr}")
    else:
        print(f"  {'#':<3} {'Player':<26} {'Stat':<9} {'Dir':<5} {'Line':>6} {'Model':>6} {'P':>6}  {col_hdr}")
    print(f"  {'-' * w}")
    for rank, (_, row) in enumerate(candidates.head(top_n).iterrows(), 1):
        direction = row.get('direction', 'OVER')
        arrow = '▲' if direction == 'OVER' else '▼'
        context = row.get('playoff_note', '') or row['game']
        if strict_mode:
            src = row.get('line_source', '')
            if src == 'sleeper_override':
                src_label = '✅ verified override'
            else:
                src_label = '⚠  proxy / not actionable'
            print(f"  {rank:<3} {row['player']:<26} {row['stat']:<9} {arrow}{direction:<4} "
                  f"{row['line']:>6.1f} {row['pred']:>6.1f} {row['prob']:>6.1%}  {src_label:<22}  {context}")
        else:
            print(f"  {rank:<3} {row['player']:<26} {row['stat']:<9} {arrow}{direction:<4} "
                  f"{row['line']:>6.1f} {row['pred']:>6.1f} {row['prob']:>6.1%}  {context}")
    if len(candidates) > top_n:
        print(f"  ... and {len(candidates) - top_n} more legs above minimum threshold")


def print_screenshot_checklist(candidates: pd.DataFrame, top_n: int = 15) -> None:
    """Print players and stats to look up in Sleeper, grouped by player."""
    if candidates.empty:
        return
    seen: set = set()
    player_stats: dict[str, list[str]] = {}
    for _, row in candidates.iterrows():
        key = (row['player_name'], row['stat_key'])
        if key in seen:
            continue
        seen.add(key)
        name = row['player_name']
        if name not in player_stats:
            player_stats[name] = []
        player_stats[name].append(_STAT_TEMPLATE_LABEL.get(row['stat_key'], row['stat_key'].upper()))
        if len(seen) >= top_n:
            break
    print(f"\n  ── SCREENSHOT CHECKLIST  (top {top_n} legs, grouped by player) {'─' * 8}")
    for name, stats in player_stats.items():
        print(f"  {name:<28}  {'  '.join(stats)}")
    print(f"  {'─' * 58}")


def print_sleeper_template(candidates: pd.DataFrame, top_n: int = 15) -> None:
    """Print a fill-in-the-blank template to paste into ChatGPT alongside screenshots."""
    if candidates.empty:
        return
    seen: set = set()
    entries: list[str] = []
    for _, row in candidates.iterrows():
        key = (row['player_name'], row['stat_key'])
        if key in seen:
            continue
        seen.add(key)
        stat_label = _STAT_TEMPLATE_LABEL.get(row['stat_key'], row['stat_key'].upper())
        entries.append(f"{row['player_name']} {stat_label} ___")
        if len(entries) >= top_n:
            break
    today = slate_today().isoformat()
    print(f"\n  {'─' * 72}")
    print(f"  SLEEPER SCREENSHOT TEMPLATE  —  top {len(entries)} props")
    print(f"  Paste into ChatGPT with your Sleeper screenshots.")
    print(f"  ChatGPT fills the blanks → save as a .txt file, one entry per line.")
    print(f"  {'─' * 72}")
    for entry in entries:
        print(f"  {entry}")
    print(f"  {'─' * 72}")
    print(f"  Then run: nba picks --props-only --quick --lines-file")
    print()


def print_card(card: list[dict], label: str, is_playoffs: bool = False) -> None:
    w = 78
    print(f"\n  {label}")
    if is_playoffs:
        print(f"  {'Player':<28} {'Stat':<9} {'Dir':<5} {'Line':>7} {'Model':>7} {'P(dir)':>7}  Playoff Context")
    else:
        print(f"  {'Player':<28} {'Stat':<9} {'Dir':<5} {'Line':>7} {'Model':>7} {'P(dir)':>7}  Matchup")
    print(f"  {'-' * w}")
    for leg in card:
        direction = leg.get('direction', 'OVER')
        arrow = '▲' if direction == 'OVER' else '▼'
        context = leg.get('playoff_note', '') or leg['game']
        print(f"  {leg['player']:<28} {leg['stat']:<9} {arrow} {direction:<4} {leg['line']:>7.1f} "
              f"{leg['pred']:>7.1f} {leg['prob']:>7.1%}  {context}")
    avg_prob = sum(leg['prob'] for leg in card) / len(card)
    combo_edge = sum(leg['prob'] for leg in card) / len(card) - 0.5
    print(f"  {'-' * w}")
    print(f"  {'Avg confidence':>44}  {avg_prob:>7.1%}  (edge: {combo_edge:+.1%} vs 50%)")


# ── Live re-pricing ────────────────────────────────────────────────────────────

def _book_p_over_at_line(row) -> float:
    """Book no-vig P(over book_line) for a market-anchored candidate row."""
    return float(row['book_p_over'])


def reprice_candidates(
    candidates: pd.DataFrame,
    pp_lines_fresh: dict,
    is_playoffs: bool,
) -> tuple[pd.DataFrame, list[dict]]:
    """
    Re-score every candidate against freshly-fetched Sleeper lines.
    Drops legs where: line moved and edge is now below threshold, OR the line
    moved from a whole number to a half-number on an UNDER (push protection lost).
    Returns (updated_candidates, movement_report).
    """
    if candidates.empty or not pp_lines_fresh:
        return candidates, []

    min_prob_map = STAT_MIN_PROB_PLAYOFFS if is_playoffs else STAT_MIN_PROB
    updated_rows: list[dict] = []
    movement_report: list[dict] = []

    for _, row in candidates.iterrows():
        stat      = row['stat_key']
        direction = row['direction']
        orig_line = float(row['line'])
        orig_prob = float(row['prob'])
        pred      = float(row['pred'])
        std       = float(row.get('std', max(abs(pred) * 0.2, 1.0)))

        pp_key   = (normalize_name(row['player_name']), stat)
        new_line = float(pp_lines_fresh.get(pp_key, orig_line))
        new_row  = dict(row)
        new_row['orig_line'] = orig_line
        new_row['orig_prob'] = orig_prob

        line_moved = abs(new_line - orig_line) >= 0.5

        if line_moved:
            new_p_over = prob_over(pred, new_line, std)
            market_leg = not pd.isna(row.get('book_line', np.nan))
            if market_leg:
                # Market-anchored leg: re-anchor the book line to the new pick'em line
                from src.models import market_blend
                mkt = market_blend.shift_line(stat, float(row['book_line']),
                                              _book_p_over_at_line(row), new_line)
                new_p_over = market_blend.blend(stat, mkt, new_p_over)
            new_prob   = new_p_over if direction == 'OVER' else (1.0 - new_p_over)

            # Whole-number → half-number UNDER: push protection disappears.
            # e.g. UNDER 3.0 → UNDER 2.5 means exactly 3 now loses instead of pushing.
            is_wth_under = (
                direction == 'UNDER' and
                orig_line == float(int(orig_line)) and   # was a whole number
                new_line == orig_line - 0.5              # moved down by exactly 0.5
            )

            buf_gap = EDGE_BUFFER_GAP.get(stat, 0.0)
            if market_leg:
                still_edgy = True
            elif direction == 'OVER':
                still_edgy = (pred - new_line >= buf_gap) or (new_prob >= MIN_EDGE_PROB)
            else:
                still_edgy = (new_line - pred >= buf_gap) or (new_prob >= MIN_EDGE_PROB)

            floor = MARKET_MIN_PROB if market_leg else min_prob_map.get(stat, 0.57)
            still_playable = (
                new_prob >= floor and
                still_edgy and
                not is_wth_under
            )

            movement_report.append({
                'player':         row['player_name'],
                'stat':           row['stat'],
                'direction':      direction,
                'orig_line':      orig_line,
                'new_line':       new_line,
                'orig_prob':      orig_prob,
                'new_prob':       new_prob,
                'wth_under':      is_wth_under,
                'still_playable': still_playable,
            })

            if not still_playable:
                continue  # drop — edge gone or push protection lost

            new_row['line'] = round(new_line, 1)
            new_row['prob'] = new_prob

        updated_rows.append(new_row)

    if not updated_rows:
        return pd.DataFrame(columns=candidates.columns), movement_report

    updated = (
        pd.DataFrame(updated_rows)
        .sort_values('prob', ascending=False)
        .reset_index(drop=True)
    )
    return updated, movement_report


def print_movement_report(movement_report: list[dict]) -> None:
    """Print a concise line-movement table before the final cards."""
    w = 88
    print(f"\n  LINE MOVEMENT CHECK  ({len(movement_report)} prop(s) repriced against fresh lines)")
    print(f"  {'─' * w}")
    print(f"  {'Player':<24} {'Stat':<9} {'Dir':<5} {'Orig':>6} {'Live':>6}  "
          f"{'Old P':>6}  {'New P':>6}  {'ΔEdge':>7}  Status")
    print(f"  {'─' * w}")
    for m in movement_report:
        delta = m['new_prob'] - m['orig_prob']
        if m['wth_under']:
            status = '⛔ WHOLE→HALF UNDER — dropped'
        elif not m['still_playable']:
            status = '❌ edge below threshold — dropped'
        else:
            status = '✅ still playable'
        print(f"  {m['player']:<24} {m['stat']:<9} {m['direction']:<5} "
              f"{m['orig_line']:>6.1f} {m['new_line']:>6.1f}  "
              f"{m['orig_prob']:>6.1%}  {m['new_prob']:>6.1%}  "
              f"{delta:>+7.1%}  {status}")
    print(f"  {'─' * w}")


# ── Saved-line helpers ────────────────────────────────────────────────────────

def load_saved_card_lines(date_str: str) -> dict:
    """
    Load previously saved card lines from data/picks/YYYY-MM-DD.json.
    Returns {(normalize_name(player), stat_key): {'line', 'prob', 'direction'}}
    or {} if no file exists for that date.
    """
    picks_path = Path(__file__).parent.parent / "data" / "picks" / f"{date_str}.json"
    if not picks_path.exists():
        return {}
    try:
        with open(picks_path) as f:
            data = json.load(f)
    except Exception:
        return {}
    result = {}
    for leg in data.get('picks', []):
        key = (normalize_name(leg['player_name']), leg['stat_key'])
        result[key] = {
            'line':      float(leg['line']),
            'prob':      float(leg['prob']),
            'direction': leg['direction'],
        }
    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def save_picks(card1: list[dict], card2: list[dict], date_str: str,
               season: str, line_source: str,
               is_playoffs: bool = False, is_play_in: bool = False) -> Path:
    """Serialize both cards to data/picks/YYYY-MM-DD.json.

    is_playoffs: True only for actual NBA Playoffs (not Play-In).
    is_play_in:  True for the Play-In tournament.
    Grading uses these flags to select the correct season_type when querying
    game_logs ('Playoffs', 'PlayIn', or 'Regular Season').
    """
    picks_dir = Path(__file__).parent.parent / "data" / "picks"
    picks_dir.mkdir(parents=True, exist_ok=True)
    out_path = picks_dir / f"{date_str}.json"

    def _serialize_card(card: list[dict], label: str, n_legs: int) -> list[dict]:
        out = []
        for leg in card:
            out.append({
                'player_id':   int(leg['player_id']),
                'player_name': leg['player_name'],
                'team':        leg['team'],
                'opp':         leg['opp'],
                'stat':        leg['stat'],         # uppercase display label
                'stat_key':    leg['stat_key'],     # lowercase for DB lookup
                'direction':   leg['direction'],
                'line':        leg['line'],
                'model_pred':  leg['pred'],
                'prob':        round(leg['prob'], 4),
                'card':        label,
                'n_legs':      n_legs,
                'line_source': leg['line_source'],
                'injury_flag': leg['injury_flag'],
            })
        return out

    # Serialize flags so the grader can pick the right season_type without
    # needing CLI arguments at grade time.
    # is_playoffs=True AND is_play_in=False  →  season_type 'Playoffs'
    # is_play_in=True                        →  season_type 'PlayIn'
    # both False                             →  season_type 'Regular Season'
    payload = {
        'date':         date_str,
        'season':       season,
        'generated_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        'line_source':  line_source,
        'is_playoffs':  is_playoffs and not is_play_in,
        'is_play_in':   is_play_in,
        'picks':        _serialize_card(card1, 'card1', len(card1))
                      + _serialize_card(card2, 'card2', len(card2)),
    }

    with open(out_path, 'w') as f:
        json.dump(payload, f, indent=2)

    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily SAFE Flex picks (live schedule + real PP lines)")
    parser.add_argument('--date', default=slate_today().isoformat(),
                        help='Game date YYYY-MM-DD (default: today)')
    parser.add_argument('--season', default=CURRENT_SEASON,
                        help=f'NBA season string (default: {CURRENT_SEASON})')
    parser.add_argument('--save', action='store_true',
                        help='Save picks to data/picks/YYYY-MM-DD.json for later grading')
    parser.add_argument('--playoffs', action='store_true',
                        help='Activate playoffs mode: applies tier-based prediction and variance adjustments')
    parser.add_argument('--play-in', action='store_true',
                        help='Activate Play-In mode: shallower discounts/narrower stdev than full playoffs, '
                             'no elevation ratios (no Play-In history in DB yet)')
    parser.add_argument('--lines', metavar='OVERRIDES',
                        help='Manual Sleeper Pick\'em lines, comma-separated: '
                             '"Jalen Green PTS 19.5, Ausar Thompson PRA 19.5". '
                             'Merges with (and wins over) fetched proxy lines. '
                             'Stats: PTS REB AST PRA REB+AST FG3M')
    parser.add_argument('--lines-file', metavar='FILE', dest='lines_file',
                        help='Path to a text file of verified Sleeper lines, one per line: '
                             '"Jalen Green PTS 19.5". Blank lines and #-comments are ignored. '
                             '--lines overrides any duplicate entries in the file.')
    parser.add_argument('--model-only', action='store_true',
                        help='Legacy model-only probabilities (no sportsbook anchor). '
                             'Not validated against real lines — ~5x overconfident.')
    parser.add_argument('--template', action='store_true',
                        help='Print a fill-in-the-blank prop template to paste into ChatGPT '
                             'alongside Sleeper screenshots')
    parser.add_argument('--debug-pp', metavar='NAMES',
                        help='Dump all PP projections for comma-separated player name substrings '
                             '(e.g. "julius randle,shai gilgeous") then exit')
    parser.add_argument('--debug-sleeper', metavar='NAMES',
                        help='Dump raw Sleeper API records for comma-separated player name substrings '
                             '(e.g. "jalen green,ausar thompson,ayo dosunmu") then exit; '
                             'shows every record returned by the projections endpoint so you can '
                             'identify which type/flags distinguish standard vs alternate lines')
    args = parser.parse_args()

    # ── Debug dump modes — print raw API data for given player(s) then exit ──
    if args.debug_pp:
        patterns = [p.strip() for p in args.debug_pp.split(',') if p.strip()]
        debug_prizepicks_lines(patterns)
        sys.exit(0)

    if args.debug_sleeper:
        patterns = [p.strip() for p in args.debug_sleeper.split(',') if p.strip()]
        season_type = 'post' if (args.playoffs or args.play_in) else 'regular'
        debug_sleeper_lines(patterns, season_type=season_type)
        sys.exit(0)

    if args.play_in and args.playoffs:
        print("Error: --play-in and --playoffs are mutually exclusive.")
        sys.exit(1)

    feature_path = PROCESSED_DIR / "features.parquet"
    if not feature_path.exists():
        print("Error: features.parquet not found. Run: python -m src.features.pipeline")
        sys.exit(1)

    # Validate --lines-file early, before any expensive fetches or DB loads.
    if getattr(args, 'lines_file', None) and not Path(args.lines_file).exists():
        print(f"\n  [--lines-file] File not found: {args.lines_file}")
        sys.exit(1)

    is_playoffs = args.playoffs or args.play_in   # both modes use the playoff adjustment path
    is_play_in  = args.play_in

    # Auto-detect season phase when the user did not explicitly pass a phase flag.
    # This prevents running in regular-season mode during playoffs (wrong thresholds + no adjustments).
    if not is_playoffs and not is_play_in:
        db_phase = detect_phase(args.season)
        if db_phase == 'playoffs':
            print(f"\n  AUTO-DETECT: DB shows Playoffs games in the last 14 days.")
            print(f"  Enabling --playoffs mode automatically. Pass --playoffs explicitly to suppress.")
            is_playoffs = True
            is_play_in  = False
        elif db_phase == 'play-in':
            print(f"\n  AUTO-DETECT: DB shows Play-In games in the last 14 days.")
            print(f"  Enabling --play-in mode automatically. Pass --play-in explicitly to suppress.")
            is_playoffs = True
            is_play_in  = True
        elif db_phase == 'offseason':
            # No recent DB games: either the season hasn't started (opening week,
            # preseason) or it's over. Today's schedule tells us which.
            # NBA game_id prefix: 001 = preseason, 002 = regular season.
            todays = get_todays_games(args.date)
            prefixes = {str(g.get('game_id', ''))[:3] for g in todays}
            if '002' in prefixes:
                print(f"\n  SEASON OPENING: regular-season games today but none in the DB's last 14 days.")
                print(f"  Running regular-season mode; rolling features carry over from last season.")
            elif '001' in prefixes:
                print(f"\n  PRESEASON: no picks. Minutes are unpredictable (stars play ~15-20 min or sit),")
                print(f"  books post only milestone ladders (no O/U lines to anchor to), and the model")
                print(f"  has never seen a preseason game. Picks resume on opening night.")
                sys.exit(0)
            else:
                print(f"\n  ERROR: No games found in the last 14 days and none scheduled today.")
                print(f"  Check your --date argument or wait for the season to begin.")
                sys.exit(1)

    # Auto-correct Play-In vs Playoffs from the NBA schedule API when a playoff flag is active,
    # to catch the mislabeling bug (e.g. passing --playoffs on a Play-In game night).
    if is_playoffs:
        detected = detect_nba_game_type(args.date)
        if detected == 'PlayIn' and not is_play_in:
            print(f"\n  AUTO-CORRECT: Schedule shows today's games are Play-In, not Playoffs.")
            print(f"       Switching to --play-in mode. Use --play-in explicitly to suppress this.")
            is_play_in = True
        elif detected == 'Playoffs' and is_play_in:
            print(f"\n  AUTO-CORRECT: Schedule shows today's games are Playoffs, not Play-In.")
            print(f"       Switching to --playoffs mode. Use --playoffs explicitly to suppress this.")
            is_play_in = False
        elif detected == 'Regular Season':
            print(f"\n  WARNING: Schedule shows regular-season games today, but playoff mode is active.")
            print(f"       Continuing with playoffs mode as requested — verify the date is correct.")
        elif not detected:
            print(f"\n  WARNING: Could not verify game type from NBA API — using detected/flag mode.")

    playoff_ratios: dict = {}
    rotation_tight_teams: set = set()

    print(f"\n  NBA Edge Model — Daily Picks")
    if is_play_in:
        mode_label = "PLAY-IN EDITION"
    elif is_playoffs:
        mode_label = "PLAYOFFS EDITION"
    else:
        mode_label = "Regular Season"
    print(f"  Date: {args.date}  |  Season: {args.season}  |  Mode: {mode_label}")

    if is_playoffs and not is_play_in:
        print(f"\n  Loading playoff history from DB (per-stat elevation ratios)...")
        playoff_ratios = load_playoff_elevation_ratios_per_stat(DB_PATH)
        if playoff_ratios:
            print(f"  Found per-stat elevation ratios for {len(playoff_ratios)} players")
        else:
            # Fall back to legacy PTS-only ratios
            playoff_ratios = load_playoff_elevation_ratios(DB_PATH)
            if playoff_ratios:
                print(f"  Found legacy PTS-based ratios for {len(playoff_ratios)} players")
            else:
                print(f"  No playoff history in DB — using tier-based adjustments only")
                print(f"  (Run 'python scripts/pull_season_data.py --playoffs' to add history)")
    elif is_play_in:
        print(f"  Play-In mode: elevation ratios disabled (no Play-In game log history in DB).")

    # Fetch Sleeper lines (primary); fall back to PrizePicks if unavailable
    sleeper_season_type = 'post' if (is_playoffs or is_play_in) else 'regular'
    print(f"\n  Fetching Sleeper lines ({sleeper_season_type})...")
    sleeper_lines = fetch_sleeper_lines(season_type=sleeper_season_type)
    using_sleeper = bool(sleeper_lines)
    if using_sleeper:
        print(f"  Found {len(sleeper_lines)} Sleeper lines for tonight")
        pp_lines = sleeper_lines
    else:
        print(f"  Sleeper unavailable — trying PrizePicks fallback...")
        pp_lines = fetch_prizepicks_lines()
        if pp_lines:
            print(f"  Found {len(pp_lines)} PrizePicks standard lines (fallback)")
        else:
            print(f"  *** Both Sleeper and PrizePicks unavailable — falling back to L10 averages ***")
            print(f"  *** Output will NOT be directly actionable on Sleeper ***")

    # Merge --lines-file into args.lines (file is loaded first; --lines wins on duplicates)
    if getattr(args, 'lines_file', None):
        lines_path = Path(args.lines_file)
        if not lines_path.exists():
            print(f"\n  [--lines-file] File not found: {args.lines_file}")
            sys.exit(1)
        try:
            raw_lines = lines_path.read_text(encoding='utf-8').splitlines()
            file_entries = [l.strip() for l in raw_lines
                            if l.strip() and not l.strip().startswith('#')]
            file_text = ', '.join(file_entries)
        except Exception as e:
            print(f"\n  [--lines-file] Could not read {args.lines_file}: {e}")
            sys.exit(1)
        if file_text:
            print(f"\n  Loading lines file: {args.lines_file}  ({len(file_entries)} entries)")
            args.lines = (file_text + ', ' + args.lines) if args.lines else file_text

    # Apply manual line overrides — merges with proxy; override values win for any
    # (player, stat) key that appears in both.  Works even if proxy fetch failed.
    override_lines: dict = {}
    if args.lines:
        override_lines = parse_line_overrides(args.lines)
        if override_lines:
            print(f"\n  Manual line overrides ({len(override_lines)} entries):")
            for (name, stat), line in sorted(override_lines.items()):
                prev = pp_lines.get((name, stat))
                note = f"  (was {prev})" if prev is not None and prev != line else ''
                print(f"    {name:<30} {stat.upper():<10} {line}{note}")
            pp_lines.update(override_lines)
            using_sleeper = using_sleeper or bool(pp_lines)
        elif args.lines.strip():
            print(f"\n  [--lines] No valid entries parsed — check format.")

    # Fetch injury report
    print(f"  Fetching injury report from ESPN...")
    injury_report = fetch_injury_report()
    out_count = sum(1 for s in injury_report.values() if 'out' in s.lower())
    dtd_count = sum(1 for s in injury_report.values() if 'day' in s.lower() or 'doubt' in s.lower())
    print(f"  {out_count} players OUT  |  {dtd_count} Day-To-Day/Doubtful")

    book_props = None
    if not getattr(args, 'model_only', False):
        from src.data.espn_props import fetch_book_props
        print(f"  Fetching sportsbook player props (ESPN / DraftKings)...")
        raw_book = fetch_book_props(args.date)
        book_props = {(normalize_name(n), st): v for (n, st), v in raw_book.items()}
        print(f"  {len(book_props)} book prop lines  (market-anchored mode; --model-only to disable)")
        if not book_props:
            print("  *** No sportsbook props found — market-anchored mode will produce no legs. ***")
            print("  *** Books may not have posted yet; retry closer to tip, or use --model-only (not validated). ***")

    print(f"\n  Loading features...")
    features_df = pd.read_parquet(feature_path)
    db_latest = pd.to_datetime(features_df['game_date']).max()
    target_dt = pd.to_datetime(args.date)
    staleness = (target_dt - db_latest).days
    if staleness > 5:
        severity = "*** CRITICAL" if staleness > 14 else "*** WARNING"
        print(f"\n  {severity}: DB is {staleness} days stale (latest game: {db_latest.date()}) ***")
        print(f"  *** Rolling features are stale — run before trusting picks:")
        print(f"  *** python scripts/update_data.py --playoffs  (rebuilds features automatically) ***")

    print(f"\n  Fetching today's schedule from NBA.com...")
    player_contexts = get_todays_player_contexts(args.date, args.season)

    if not player_contexts:
        print("  No players found for this date.")
        sys.exit(0)

    print(f"\n  Building features for {len(player_contexts)} roster slots...")
    df_today = build_todays_features(features_df, player_contexts, args.date,
                                      is_playoffs=is_playoffs, season=args.season)

    if df_today.empty:
        print("  No players with sufficient game history.")
        sys.exit(0)

    print(f"  Players qualifying (min_L10 >= {MIN_MINUTES} min): {len(df_today)}")

    # Detect playoff round from today's game IDs and print with mode
    if is_playoffs and not is_play_in:
        detected_rounds = df_today['playoff_round'].dropna().astype(int)
        detected_rounds = detected_rounds[detected_rounds > 0]
        if not detected_rounds.empty:
            cur_round = int(detected_rounds.mode().iloc[0])
            round_label = ROUND_LABELS.get(cur_round, f"Round {cur_round}")
            print(f"  Playoff round detected: {round_label} (R{cur_round})")
        else:
            cur_round = 0
            print(f"  Playoff round: unknown (game_id format unrecognized)")

        # Rotation tightening signal (display only — does not alter probabilities)
        # Intersect with today's slate teams so off-slate teams (e.g. HOU on a CLE/DET day)
        # never bleed into the annotation.
        slate_teams = {ctx['team_abbr'] for ctx in player_contexts}
        rotation_tight_teams = get_rotation_tightening_teams(args.date) & slate_teams
        if rotation_tight_teams:
            print(f"  Rotation tightening teams: {', '.join(sorted(rotation_tight_teams))} (↑rot-tight annotation applied)")

    if using_sleeper:
        line_source_str = 'sleeper'
    elif pp_lines:
        line_source_str = 'prizepicks'
    else:
        line_source_str = 'l10_avg'

    # Keys that came from --lines are the only legs allowed in strict-mode cards
    override_keys: set = set(override_lines.keys()) if override_lines else set()

    candidates = build_candidates(df_today, pp_lines, injury_report,
                                  is_playoffs=is_playoffs,
                                  is_play_in=is_play_in,
                                  playoff_ratios=playoff_ratios if is_playoffs else None,
                                  line_source=line_source_str,
                                  override_keys=override_keys,
                                  rotation_tight_teams=rotation_tight_teams if is_playoffs else None,
                                  book_props=book_props)

    if candidates.empty:
        if pp_lines:
            src = "Sleeper" if using_sleeper else "PrizePicks"
            print(f"  No players matched {src} lines above probability thresholds.")
        else:
            print("  No legs met minimum probability thresholds.")
        sys.exit(0)

    print(f"  Qualified legs: {len(candidates)}")

    # Compare previously saved card lines against current live Sleeper lines.
    # The old within-run re-fetch always returned identical lines (fetched seconds
    # apart), so movement was never detected.  The real signal lives in the prior
    # save file: if lines shifted since the last --save run, that's what we flag.
    movement_report: list[dict] = []
    if pp_lines:
        saved_lines = load_saved_card_lines(args.date)
        if saved_lines:
            live_src = "Sleeper" if using_sleeper else "PP"
            print(f"\n  Checking {len(saved_lines)} saved-card leg(s) against live {live_src} lines...")
            # Patch candidates: for any leg that was in the prior save, temporarily
            # set its line/prob to the saved values so reprice_candidates can detect
            # movement and compute correct delta-prob for the report.
            cands_patched = candidates.copy()
            for i, row in cands_patched.iterrows():
                key = (normalize_name(row['player_name']), row['stat_key'])
                if key in saved_lines:
                    cands_patched.at[i, 'line'] = saved_lines[key]['line']
                    # Saved probs from a different probability method (e.g. pre-market-
                    # anchoring files) must not overwrite freshly computed ones.
                    if book_props is None:
                        cands_patched.at[i, 'prob'] = saved_lines[key]['prob']
            candidates, movement_report = reprice_candidates(
                cands_patched, pp_lines, is_playoffs=is_playoffs,
            )
            n_moved   = len(movement_report)
            n_dropped = sum(1 for m in movement_report if not m['still_playable'])
            if n_moved:
                print(f"  {n_moved} saved-card line(s) moved | {n_dropped} leg(s) dropped after repricing")
                if candidates.empty:
                    print("  All legs dropped after repricing — no playable edges remain.")
                    sys.exit(0)
            else:
                print(f"  Lines stable vs saved picks — no movement detected")
        else:
            print(f"\n  No saved picks for today — line-movement check skipped (first run)")

    # Separate DTD/Doubtful into a conditional pool (excluded from SAFE cards)
    dtd_candidates = (
        candidates[candidates['is_dtd']].copy()
        if 'is_dtd' in candidates.columns else pd.DataFrame()
    )

    strict_mode = STRICT_SLEEPER_MODE and using_sleeper
    n_verified = 0

    if strict_mode:
        n_verified = int((candidates['line_source'] == 'sleeper_override').sum())
        if n_verified < STRICT_CARD1_MIN_LEGS:
            card1 = []
            card2 = []
            print(f"\n  Not enough verified Sleeper lines to build Card 1.")
            print(f"  Need {STRICT_CARD1_MIN_LEGS} --lines overrides; have {n_verified}.")
            print(f"  Supply real app lines with: --lines \"Player Name STAT line, ...\"")
        else:
            card1 = build_card(candidates, n_legs=3, exclude_keys=set(), strict_override=True)
            used = {(leg['player_id'], leg['stat'], leg['direction']) for leg in card1}
            used_pids = {leg['player_id'] for leg in card1}
            if n_verified >= STRICT_CARD2_MIN_LEGS:
                card2 = build_card(candidates, n_legs=5, exclude_keys=used, strict_override=True,
                                   exclude_players=used_pids)
            else:
                card2 = []
    else:
        card1 = build_card(candidates, n_legs=3, exclude_keys=set())
        used = {(leg['player_id'], leg['stat'], leg['direction']) for leg in card1}
        used_pids = {leg['player_id'] for leg in card1}
        card2 = build_card(candidates, n_legs=5, exclude_keys=used, exclude_players=used_pids)

    # Conditional card: DTD/Doubtful legs — confirm active before betting
    conditional_card: list[dict] = []
    if not dtd_candidates.empty:
        cond_excl = {(leg['player_id'], leg['stat'], leg['direction'])
                     for leg in card1 + card2}
        conditional_card = build_card(
            dtd_candidates,
            n_legs=min(3, len(dtd_candidates)),
            exclude_keys=cond_excl,
            safe_only=False,
            strict_override=strict_mode,
        )

    if override_lines and using_sleeper:
        line_label = f"Sleeper proxy + {len(override_lines)} verified override(s)"
    elif override_lines:
        line_label = f"{len(override_lines)} verified override(s) only"
    elif using_sleeper:
        line_label = "Sleeper proxy lines (STRICT MODE — no actionable cards without --lines)"
    elif pp_lines:
        line_label = "PrizePicks lines (Sleeper fallback)"
    else:
        line_label = "L10 avg lines (Sleeper + PP unavailable)"
    if is_play_in:
        playoffs_label = "  [PLAY-IN EDITION]"
    elif is_playoffs:
        playoffs_label = "  [PLAYOFFS EDITION]"
    else:
        playoffs_label = ""
    print(f"\n{'=' * 76}")
    print(f"  SAFE FLEX DAILY PICKS  —  {line_label}{playoffs_label}")
    print(f"{'=' * 76}")

    # Show line-movement summary before cards
    if movement_report:
        print_movement_report(movement_report)

    # Show ranked board first (all qualifying legs — proxy and override both appear)
    if is_playoffs:
        print_ranked_board(candidates, top_n=25, is_playoffs=True, strict_mode=strict_mode)
    elif strict_mode:
        print_ranked_board(candidates, top_n=25, is_playoffs=False, strict_mode=True)

    if strict_mode:
        print_screenshot_checklist(candidates, top_n=15)

    if getattr(args, 'template', False):
        print_sleeper_template(candidates, top_n=15)

    if card1:
        print_card(card1, f"CARD 1  —  {len(card1)}-Leg SAFE Flex", is_playoffs=is_playoffs)
    elif not (strict_mode and n_verified < STRICT_CARD1_MIN_LEGS):
        print("\n  CARD 1: Not enough qualifying legs (need 3).")

    if card2:
        print_card(card2, f"CARD 2  —  {len(card2)}-Leg SAFE Flex", is_playoffs=is_playoffs)
    elif strict_mode and (not card1 or n_verified < STRICT_CARD2_MIN_LEGS):
        if card1:
            print(f"\n  CARD 2: Need {STRICT_CARD2_MIN_LEGS} verified override legs to build Card 2 "
                  f"(have {n_verified}).")
    else:
        print("\n  CARD 2: Not enough non-overlapping legs (need 5).")

    if conditional_card:
        print_card(
            conditional_card,
            "CONDITIONAL CARD  —  DTD/Doubtful players (confirm active before betting)",
            is_playoffs=is_playoffs,
        )

    print(f"\n{'=' * 76}")
    if pp_lines:
        if override_lines:
            src_label = "Override line"
        elif using_sleeper:
            src_label = "Sleeper proxy line"
        else:
            src_label = "PP line"
        print(f"  {src_label} = line used for edge calc  |  Model = blended prediction")
    if book_props is not None:
        print(f"  P = sportsbook no-vig consensus at the pick'em line, nudged by the model")
        print(f"  THRESHOLD   all stats >= {round(MARKET_MIN_PROB*100)}%  (market-anchored; pick'em breakeven ~54%)")
    else:
        thresholds = STAT_MIN_PROB_PLAYOFFS if is_playoffs else STAT_MIN_PROB
        thresholds_str = "  |  ".join(f"{s.upper()} >= {round(v*100)}%" for s, v in thresholds.items())
        print(f"  THRESHOLDS  {thresholds_str}   (MODEL-ONLY — not validated against real lines)")
    if is_playoffs and book_props is None:
        tier_str = "  |  ".join(f"{t} >= {round(v*100)}%" for t, v in TIER_MIN_PROB_PLAYOFFS.items())
        print(f"  TIER GATES  {tier_str}")
        if STAT_TIER_MIN_PROB_PLAYOFFS:
            override_str = "  |  ".join(f"{t} {s.upper()} >= {round(v*100)}%" for (t, s), v in STAT_TIER_MIN_PROB_PLAYOFFS.items())
            print(f"  STAT GATES  {override_str}")
    print(f"  RULES       0 shared legs  |  max 2/game  |  1/player  |  max 2/stat-type")
    if is_playoffs:
        print(f"  ADJUSTMENTS per-stat elevation ratios | expanded stdev | suppression active")
    print(f"  ⚠️  = Day-To-Day/Doubtful — verify lineup before betting")
    if strict_mode:
        print(f"  {'─' * 72}")
        print(f"  Only manual Sleeper overrides are actionable. Proxy lines are informational only.")
        phase_flag = ' --play-in' if is_play_in else ' --playoffs' if is_playoffs else ''
        print(f"  To add real app lines: nba picks{phase_flag} --lines \"Name STAT line, ...\"")
    print(f"{'=' * 76}\n")

    if args.save:
        out_path = save_picks(card1 or [], card2 or [], args.date, args.season, line_source_str,
                              is_playoffs=is_playoffs, is_play_in=is_play_in)
        n_saved = len(card1 or []) + len(card2 or [])
        print(f"  Saved {n_saved} picks → {out_path}")
        print(f"  Grade after games: python scripts/log_pick_results.py --date {args.date}\n")


if __name__ == "__main__":
    main()
