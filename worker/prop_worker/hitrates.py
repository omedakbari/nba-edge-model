"""Hit-rate and line maths shared by `prop_worker.slate` and the web app (web/lib/hitRates.ts).

Both implementations are checked against the same cases: worker/tests/fixtures/hit_rates_cases.json.
Change a rule here, change it there, and add a case.

Rules:
  * A game counts only if the player played: not did_not_play and minutes > 0.
  * stat value: pts, reb, ast, fg3m as stored; pra = pts + reb + ast; ra = reb + ast.
  * over hit = value > line; under hit = value < line; value == line is a push (counted in n, not a hit).
  * L5 / L10 / L15 = the player's last 5 / 10 / 15 played games before the slate date (fewer if
    that's all there is; n says how many).
  * season = played games on or after the season start (Oct 1).
  * h2h = played games against tonight's opponent, this season and last.
  * consensus line = the line most of the consensus books offer; a tie goes to Pinnacle's line if
    Pinnacle is among the tied lines, else the lower line.
  * no-vig P(over) at a line = mean over books at that line of p_over / (p_over + p_under), where
    p = implied probability of the American price.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

STATS = ("pts", "reb", "ast", "fg3m", "pra", "ra")
WINDOWS = (("l5", 5), ("l10", 10), ("l15", 15))
CONSENSUS_BOOKS = ("draftkings", "fanduel", "betmgm", "pinnacle")


@dataclass(frozen=True)
class GameLine:
    game_date: date
    opp: str
    minutes: float | None
    did_not_play: bool
    pts: int | None
    reb: int | None
    ast: int | None
    fg3m: int | None


def played(g: GameLine) -> bool:
    return not g.did_not_play and (g.minutes or 0) > 0


def stat_value(g: GameLine, stat: str) -> float | None:
    parts = {"pra": ("pts", "reb", "ast"), "ra": ("reb", "ast")}.get(stat, (stat,))
    values = [getattr(g, p) for p in parts]
    return None if any(v is None for v in values) else float(sum(values))


@dataclass(frozen=True)
class Rate:
    hits: int
    n: int
    pushes: int = 0

    @property
    def pct(self) -> float | None:
        return self.hits / self.n if self.n else None

    def as_dict(self) -> dict:
        return {"hits": self.hits, "n": self.n, "pushes": self.pushes, "pct": self.pct}


def _rate(games: list[GameLine], stat: str, line: float, side: str) -> Rate:
    hits = pushes = n = 0
    for g in games:
        v = stat_value(g, stat)
        if v is None:
            continue
        n += 1
        if v == line:
            pushes += 1
        elif (v > line) if side == "over" else (v < line):
            hits += 1
    return Rate(hits, n, pushes)


def hit_rates(games: list[GameLine], stat: str, line: float, *, before: date, season_start: date,
              opp: str | None, h2h_since: date, side: str = "over") -> dict[str, Rate]:
    """games: any order, any dates. Only played games before `before` count."""
    if side not in ("over", "under"):
        raise ValueError("side must be 'over' or 'under'")
    if stat not in STATS:
        raise ValueError(f"stat must be one of {STATS}")
    past = sorted((g for g in games if played(g) and g.game_date < before),
                  key=lambda g: g.game_date, reverse=True)
    out = {key: _rate(past[:n], stat, line, side) for key, n in WINDOWS}
    out["season"] = _rate([g for g in past if g.game_date >= season_start], stat, line, side)
    out["h2h"] = _rate([g for g in past if opp and g.opp == opp and g.game_date >= h2h_since], stat, line, side)
    return out


def consensus_line(lines: dict[str, float]) -> tuple[float | None, int]:
    """(line, number of books offering it) from {book: line} for the consensus books."""
    offered = {b: float(v) for b, v in lines.items() if b in CONSENSUS_BOOKS and v is not None}
    if not offered:
        return None, 0
    counts = Counter(offered.values())
    top = max(counts.values())
    tied = sorted(v for v, c in counts.items() if c == top)
    pin = offered.get("pinnacle")
    choice = pin if pin in tied else tied[0]
    return choice, top


def implied(american: float | None) -> float | None:
    if american is None:
        return None
    a = float(american)
    if a == 0:
        return None
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def no_vig_over(pairs: list[tuple[float | None, float | None]]) -> float | None:
    """Mean no-vig P(over) over books given [(over_american, under_american)] at the same line."""
    ps = []
    for over, under in pairs:
        po, pu = implied(over), implied(under)
        if po is not None and pu is not None and po + pu > 0:
            ps.append(po / (po + pu))
    return sum(ps) / len(ps) if ps else None
