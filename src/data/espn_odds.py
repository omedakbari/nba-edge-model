"""
ESPN Odds Fetcher — pulls today's NBA spread, moneyline, and O/U.

Source: ESPN public scoreboard API (DraftKings lines, no API key required).
Returns one dict per game with all lines normalized to our team abbreviations.

Usage:
    from src.data.espn_odds import fetch_odds, moneyline_to_implied_prob

    games = fetch_odds()           # today
    games = fetch_odds('2026-04-20')  # specific date

    for g in games:
        print(g['home'], 'vs', g['away'])
        print('Spread:', g['spread'])        # home-perspective: neg = home fav
        print('Home ML:', g['home_ml'])      # e.g. -180
        print('Away ML:', g['away_ml'])      # e.g. +155
        print('O/U:', g['over_under'])
"""

import time
import warnings
from datetime import date, datetime
from typing import Optional

import requests

warnings.filterwarnings("ignore")

# ── ESPN API ──────────────────────────────────────────────────────────────────

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
REQUEST_TIMEOUT = 10

# ── Team abbreviation normalization ───────────────────────────────────────────
# ESPN uses standard NBA abbreviations but a handful differ from our DB canonical set.
# Map ESPN abbr → our canonical abbr.

ESPN_TO_CANONICAL: dict[str, str] = {
    # ESPN sometimes uses these alternate codes
    'GS':  'GSW',
    'NY':  'NYK',
    'SA':  'SAS',
    'NO':  'NOP',
    'OKL': 'OKC',
    'PHO': 'PHX',
    'BRK': 'BKN',
    'NJ':  'BKN',
    'CHA': 'CHA',
    'UTA': 'UTA',
    # Most are already correct; add new ones if ESPN changes codes mid-season
}

CANONICAL_TO_ESPN: dict[str, str] = {v: k for k, v in ESPN_TO_CANONICAL.items() if k != v}


def _normalize(abbr: str) -> str:
    """Normalize ESPN abbreviation to our canonical set."""
    return ESPN_TO_CANONICAL.get(abbr.upper(), abbr.upper())


# ── Odds helpers ──────────────────────────────────────────────────────────────

def moneyline_to_implied_prob(ml: int | str | None) -> Optional[float]:
    """
    Convert American moneyline odds to implied probability (no vig).
    Returns None if ml is None or unparseable.

    +150 → 0.40   (risk 100 to win 150, P = 100/(150+100))
    -200 → 0.667  (risk 200 to win 100, P = 200/(200+100))
    """
    if ml is None:
        return None
    try:
        ml = int(str(ml).replace('+', '').replace(' ', ''))
    except (ValueError, TypeError):
        return None
    if ml > 0:
        return round(100 / (ml + 100), 4)
    else:
        return round(abs(ml) / (abs(ml) + 100), 4)


def implied_prob_to_moneyline(p: float) -> int:
    """Convert probability to American moneyline integer."""
    p = max(0.01, min(0.99, p))
    if p >= 0.5:
        return -round(p / (1 - p) * 100)
    else:
        return round((1 - p) / p * 100)


def spread_to_win_prob(spread: float, std: float = 11.0) -> float:
    """Convert point spread (home perspective) to P(home win) via normal CDF."""
    from scipy import stats as scipy_stats
    return float(scipy_stats.norm.cdf(spread / std))


# ── Fetcher ───────────────────────────────────────────────────────────────────

def fetch_odds(target_date: str | None = None, retries: int = 2) -> list[dict]:
    """
    Fetch NBA odds for a given date from ESPN.

    Args:
        target_date: YYYY-MM-DD string (default: today)
        retries: number of retry attempts on network failure

    Returns:
        List of game dicts, one per game:
        {
          'game_id':     str      ESPN event ID
          'game_date':   str      YYYY-MM-DD
          'game_time':   str      HH:MM ET or 'TBD'
          'status':      str      'scheduled'|'in_progress'|'final'
          'home':        str      canonical team abbr
          'away':        str      canonical team abbr
          'home_full':   str      full team name
          'away_full':   str      full team name
          'spread':      float|None  home-team spread (neg = home fav)
          'spread_team': str|None    team the spread applies to from ESPN
          'over_under':  float|None
          'home_ml':     int|None    American odds
          'away_ml':     int|None
          'home_implied':float|None  implied P(home win) from ML
          'away_implied':float|None
          'odds_provider':str    e.g. 'Draft Kings'
          'home_score':  int|None  (if game in progress or final)
          'away_score':  int|None
        }
    """
    params = {}
    if target_date:
        d = datetime.strptime(target_date, '%Y-%m-%d')
        params['dates'] = d.strftime('%Y%m%d')

    for attempt in range(retries + 1):
        try:
            resp = requests.get(ESPN_SCOREBOARD, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(2)
                continue
            return []

    events = data.get('events', [])
    games = []

    for ev in events:
        comps = ev.get('competitions', [])
        if not comps:
            continue
        comp = comps[0]

        # Teams
        competitors = comp.get('competitors', [])
        home_comp = next((c for c in competitors if c.get('homeAway') == 'home'), {})
        away_comp = next((c for c in competitors if c.get('homeAway') == 'away'), {})

        home_abbr = _normalize(home_comp.get('team', {}).get('abbreviation', ''))
        away_abbr = _normalize(away_comp.get('team', {}).get('abbreviation', ''))
        home_full = home_comp.get('team', {}).get('displayName', home_abbr)
        away_full = away_comp.get('team', {}).get('displayName', away_abbr)

        # Status
        status_obj = comp.get('status', {}).get('type', {})
        state = status_obj.get('state', 'pre')   # pre / in / post
        if state == 'pre':
            status = 'scheduled'
        elif state == 'in':
            status = 'in_progress'
        else:
            status = 'final'

        # Scores (if available)
        home_score = None
        away_score = None
        try:
            home_score = int(home_comp.get('score', ''))
        except (ValueError, TypeError):
            pass
        try:
            away_score = int(away_comp.get('score', ''))
        except (ValueError, TypeError):
            pass

        # Game time
        game_time = ev.get('date', '')[:16].replace('T', ' ')[:10]   # just date
        status_detail = status_obj.get('shortDetail', '')

        # Odds
        odds_list = comp.get('odds', [])
        odds = odds_list[0] if odds_list else {}
        provider = odds.get('provider', {}).get('name', 'Unknown')

        spread       = odds.get('spread')   # float, from home perspective? Let's check
        details_str  = odds.get('details', '')  # e.g. "CHA -5.5"
        over_under   = odds.get('overUnder')

        # Parse spread carefully from details string
        # "CHA -5.5" → team CHA has -5.5, meaning CHA is favored by 5.5
        spread_val  = None
        spread_team = None
        if details_str and details_str.strip() not in ('', 'N/A', 'EVEN', 'PK', 'Even'):
            parts = details_str.strip().split()
            if len(parts) == 2:
                try:
                    spread_team = _normalize(parts[0])
                    spread_val  = float(parts[1])
                except ValueError:
                    pass
            elif details_str.strip() in ('EVEN', 'PK', 'Pick'):
                spread_val  = 0.0
                spread_team = home_abbr

        # Convert spread to home-team perspective
        # If spread_team is home, spread is already home_spread
        # If spread_team is away, home_spread = -spread_val
        home_spread = None
        if spread_val is not None and spread_team is not None:
            if spread_team == home_abbr:
                home_spread = spread_val
            else:
                home_spread = -spread_val

        # Moneyline
        ml_obj = odds.get('moneyline', {})
        home_ml_str = ml_obj.get('home', {}).get('close', {}).get('odds')
        away_ml_str = ml_obj.get('away', {}).get('close', {}).get('odds')

        def _parse_ml(s):
            if s is None:
                return None
            try:
                return int(str(s).replace('+', '').strip())
            except (ValueError, TypeError):
                return None

        home_ml = _parse_ml(home_ml_str)
        away_ml = _parse_ml(away_ml_str)

        home_implied = moneyline_to_implied_prob(home_ml)
        away_implied = moneyline_to_implied_prob(away_ml)

        games.append({
            'game_id':      str(ev.get('id', '')),
            'game_date':    ev.get('date', '')[:10],
            'game_time':    status_detail,
            'status':       status,
            'home':         home_abbr,
            'away':         away_abbr,
            'home_full':    home_full,
            'away_full':    away_full,
            'spread':       home_spread,
            'spread_team':  spread_team,
            'over_under':   over_under,
            'home_ml':      home_ml,
            'away_ml':      away_ml,
            'home_implied': home_implied,
            'away_implied': away_implied,
            'odds_provider':provider,
            'home_score':   home_score,
            'away_score':   away_score,
        })

    return games


def fetch_odds_or_warn(target_date: str | None = None) -> list[dict]:
    """Fetch odds with a user-visible warning on failure."""
    games = fetch_odds(target_date)
    if not games:
        print(f"  ⚠️  ESPN odds unavailable for {target_date or 'today'} — check network or try again.")
    return games
