"""Name normalization, the team+name matcher, and Kalshi ticker parsing.

normalize_name matches scripts/generate_todays_picks.py so the worker and the old pipeline agree.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date

_SUFFIXES = (" jr", " sr", " ii", " iii", " iv", " v")


def normalize_name(name: str) -> str:
    """Lowercase, strip accents and punctuation, drop Jr/Sr/II-style suffixes."""
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().replace("'", "").replace("’", "").replace(".", "").replace("-", " ").replace(",", " ")
    name = " ".join(name.split())
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


def season_start(today: date) -> date:
    """NBA seasons start in October: 2026-11-10 -> 2026-10-01, 2027-03-01 -> 2026-10-01."""
    return date(today.year if today.month >= 8 else today.year - 1, 10, 1)


@dataclass(frozen=True)
class Candidate:
    player_id: int
    full_name: str
    team_abbr: str | None
    name_norm: str
    team_as_of: date | None = None


@dataclass
class MatchResult:
    player_id: int | None
    method: str | None = None        # team_name | team_last_initial | name_unique_stale_team
    reason: str | None = None        # set when player_id is None
    candidates: list[dict] = field(default_factory=list)


class PlayerIndex:
    """Team + normalized-name lookup over internal players.

    Tier 1  same team, same normalized name, exactly one hit           -> team_name
    Tier 2  same team, same last name, same first initial, one hit     -> team_last_initial
            (Nic/Nicolas Claxton, Cam/Cameron Thomas, Herb/Herbert Jones)
    Tier 3  name unique league-wide AND our team for that player is unknown, or comes from
            before `stale_before` (this season's start)                -> name_unique_stale_team
            (offseason moves; rookies with no game logs yet)
    Otherwise no match. A same-name player with current-season evidence of another team is never
    matched; it's listed as a candidate in the unmatched report.
    """

    def __init__(self, players: list[Candidate]):
        self.by_id: dict[int, Candidate] = {}
        self.by_team_name: dict[tuple, list[Candidate]] = {}
        self.by_team_last: dict[tuple, list[Candidate]] = {}
        self.by_name: dict[str, list[Candidate]] = {}
        for p in players:
            self.add(p)

    def _keys(self, p: Candidate):
        parts = p.name_norm.split()
        yield self.by_team_name, (p.team_abbr, p.name_norm)
        if parts:
            yield self.by_team_last, (p.team_abbr, parts[-1])
        yield self.by_name, p.name_norm

    def add(self, p: Candidate) -> None:
        self.by_id[p.player_id] = p
        for index, key in self._keys(p):
            index.setdefault(key, []).append(p)

    def remove(self, player_id: int) -> None:
        p = self.by_id.pop(player_id, None)
        if p is None:
            return
        for index, key in self._keys(p):
            index[key] = [c for c in index.get(key, []) if c.player_id != player_id]

    def move(self, player_id: int, team_abbr: str, as_of: date) -> Candidate:
        moved = replace(self.by_id[player_id], team_abbr=team_abbr, team_as_of=as_of)
        self.remove(player_id)
        self.add(moved)
        return moved

    def match(self, name: str, team_abbr: str | None, stale_before: date | None = None) -> MatchResult:
        norm = normalize_name(name)
        if not norm:
            return MatchResult(None, reason="empty_name")
        if not team_abbr:
            return MatchResult(None, reason="no_team", candidates=_c(self.by_name.get(norm, [])))
        exact = self.by_team_name.get((team_abbr, norm), [])
        if len(exact) == 1:
            return MatchResult(exact[0].player_id, "team_name")
        if len(exact) > 1:
            return MatchResult(None, reason="ambiguous_team_name", candidates=_c(exact))
        parts = norm.split()
        loose = [p for p in self.by_team_last.get((team_abbr, parts[-1]), [])
                 if p.name_norm[:1] == norm[:1]]
        if len(loose) == 1:
            return MatchResult(loose[0].player_id, "team_last_initial")
        if len(loose) > 1:
            return MatchResult(None, reason="ambiguous_last_initial", candidates=_c(loose))
        elsewhere = self.by_name.get(norm, [])
        if len(elsewhere) == 1 and stale_before is not None:
            c = elsewhere[0]
            if c.team_abbr is None or (c.team_as_of is not None and c.team_as_of < stale_before):
                return MatchResult(c.player_id, "name_unique_stale_team")
        if len(elsewhere) > 1:
            return MatchResult(None, reason="ambiguous_name", candidates=_c(elsewhere))
        return MatchResult(None, reason="other_team_only" if elsewhere else "no_candidate",
                           candidates=_c(elsewhere))


def _c(players: list[Candidate]) -> list[dict]:
    return [{"player_id": p.player_id, "name": p.full_name, "team": p.team_abbr,
             "team_as_of": p.team_as_of.isoformat() if p.team_as_of else None} for p in players[:5]]


# ── Kalshi tickers ────────────────────────────────────────────────────────────
# KXNBAPTS-26NOV10MIAUTA-UTABSENSABAUGH28-25
#   series   event: yyMONdd + away + home      fragment: team + initial + LAST + jersey   threshold

_EVENT = re.compile(r"^(\d{2}[A-Z]{3}\d{2})([A-Z]{3})([A-Z]{3})$")
_FRAGMENT = re.compile(r"^([A-Z]{3})([A-Z]+?)(\d*)$")


@dataclass(frozen=True)
class KalshiTicker:
    series: str
    date_code: str          # '26NOV10'
    away: str
    home: str
    fragment: str | None    # 'UTABSENSABAUGH28' (player props only)
    team: str | None        # 'UTA'
    tail: str | None        # threshold or side, e.g. '25', 'UTA', 'POR8'


def parse_kalshi_ticker(ticker: str) -> KalshiTicker | None:
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    m = _EVENT.match(parts[1])
    if not m:
        return None
    date_code, away, home = m.groups()
    fragment = team = tail = None
    if len(parts) >= 4:                      # player prop: series-event-fragment-threshold
        fragment, tail = parts[2], parts[3]
        fm = _FRAGMENT.match(fragment)
        team = fm.group(1) if fm else None
        if team not in (away, home):
            team = None
    elif len(parts) == 3:                    # game market: series-event-side
        tail = parts[2]
    return KalshiTicker(parts[0], date_code, away, home, fragment, team, tail)


def kalshi_player_name(market: dict) -> str:
    """'Brice Sensabaugh: 25+ points' -> 'Brice Sensabaugh'."""
    text = market.get("title") or market.get("yes_sub_title") or ""
    return text.split(":", 1)[0].strip()
