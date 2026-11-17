"""card: a short research-only shortlist built from the slate (PropLine consensus/no-vig, Sleeper, Kalshi).

Run:  python -m prop_worker.slate --card [--date 2026-11-17] [--out data/cards/2026-11-17.txt]

UNVALIDATED. These formulas were chosen by hand after one bad week; nothing here is backtested, and
the stake stays $0 until the bets ledger (prop_worker.bets) shows calibration.

Sleeper legs (one per player/stat that has a Sleeper line):
  line      Sleeper's line. Hit rates are taken AT that line. If it differs from the consensus line the
            leg is flagged "slp soft" (easier for the chosen side) or "slp hard" (harder).
  p_hist    (h10 + h15 + hszn + 1.5) / (n10 + n15 + nszn + 3)        for the over at the line
  p_blend   0.35 * p_hist + 0.65 * nv       nv = no-vig P(over) at the consensus line
            no nv -> 0.5 + (p_hist - 0.5) / 2, flagged "no market"
  side      over if p_blend >= .5 else under;  p = max(p_blend, 1 - p_blend);  kept if p >= .62
  Caveat: when Sleeper's line differs, nv is still the consensus line's number (no line-shift model).
Kalshi strikes (line 3.5 = "4+"):
  pk        0.35 * (h10_at_strike + .5) / (n10 + 1) + 0.65 * yes_mid
            yes_mid = (yes_ask + 1 - no_ask) / 2 (yes_ask alone if there is no NO ask)
  YES       p = pk,     price = yes_ask;   NO  p = 1 - pk, price = no_ask
  shown     if p - price >= .05, price <= .85 and p >= .68; max = p - .05 (don't pay more)
Flags:
  on the line      3+ of the last 10 played games landed exactly on the integer where the bet loses by
                   one (under 3.5 / 4+ NO -> 4; over 3.5 / 4+ YES -> 3; whole lines -> the push value)
  market disagrees |p_hist - nv| > .25 (Kalshi: |L10 rate - yes_mid| > .25)
  no market        no consensus no-vig price
Excluded: players listed OUT / D / Q / GTD (official report, else ESPN), games already started,
quotes not seen within STALE_MIN of the slate's newest quote (pulled lines).
Sorted by tip time, then p. Stat priority REB/AST > PRA/RA > PTS/3PM decides what is cut when the
card would pass MAX_ROWS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .hitrates import GameLine, hit_rates, played, stat_value

PT = ZoneInfo("America/Los_Angeles")
W_HIST, W_MKT = 0.35, 0.65
SLEEPER_MIN_P = 0.62
KALSHI_MIN_P = 0.68
KALSHI_MIN_EDGE = 0.05
KALSHI_MAX_PRICE = 0.85
DISAGREE = 0.25
ON_LINE_MIN = 3
STALE_MIN = 45
MAX_ROWS = 50            # + header/footer stays under 60 lines
EXCLUDED = {"OUT", "D", "Q", "GTD"}
PRIORITY = {"reb": 0, "ast": 0, "pra": 1, "ra": 1, "pts": 2, "fg3m": 2}
STAT_LABEL = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "pra": "PRA", "ra": "RA"}


# ── formulas ────────────────────────────────────────────────────────────────

def p_hist(h10: int, n10: int, h15: int, n15: int, hszn: int, nszn: int) -> float:
    return (h10 + h15 + hszn + 1.5) / (n10 + n15 + nszn + 3)


def p_blend(ph: float, nv: float | None) -> tuple[float, bool]:
    """(blended P(over), has_market)."""
    if nv is None:
        return 0.5 + (ph - 0.5) / 2, False
    return W_HIST * ph + W_MKT * nv, True


def side_and_p(pb: float) -> tuple[str, float]:
    return ("over", pb) if pb >= 0.5 else ("under", 1 - pb)


def yes_mid(yes_ask: float | None, no_ask: float | None) -> float | None:
    if yes_ask is None:
        return None
    return yes_ask if no_ask is None else (yes_ask + 1 - no_ask) / 2


def p_kalshi(h10: int, n10: int, mid: float) -> float:
    return W_HIST * (h10 + 0.5) / (n10 + 1) + W_MKT * mid


def kalshi_pick(pk: float, yes_ask: float | None, no_ask: float | None) -> tuple[str, float, float] | None:
    """('YES'|'NO', p, price) for the side that clears edge/price/p, else None."""
    best = None
    for side, p, price in (("YES", pk, yes_ask), ("NO", 1 - pk, no_ask)):
        if price is None:
            continue
        if p - price >= KALSHI_MIN_EDGE - 1e-9 and price <= KALSHI_MAX_PRICE and p >= KALSHI_MIN_P:
            if best is None or p - price > best[1] - best[2]:
                best = (side, p, price)
    return best


def hinge(line: float, side: str) -> int:
    """The value at which this bet loses by the smallest margin (or pushes, on a whole line)."""
    if float(line).is_integer():
        return int(line)
    return math.ceil(line) if side in ("under", "NO") else math.floor(line)


def on_the_line(last10: list[float], line: float, side: str) -> bool:
    h = hinge(line, side)
    return sum(v == h for v in last10) >= ON_LINE_MIN


def last_values(games: list[GameLine], stat: str, before: date, n: int = 10) -> list[float]:
    past = sorted((g for g in games if played(g) and g.game_date < before), key=lambda g: g.game_date,
                  reverse=True)
    vals = [stat_value(g, stat) for g in past]
    return [v for v in vals if v is not None][:n]


# ── assembling ──────────────────────────────────────────────────────────────

@dataclass
class Row:
    tip: datetime | None
    game: str
    player: str
    stat: str
    platform: str            # sleeper | kalshi
    side: str                # over/under | YES/NO
    line: float
    p: float
    price: float | None = None
    max_price: float | None = None
    flags: list[str] = field(default_factory=list)
    player_id: int | None = None

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.stat, 3)


def _game(p) -> str:
    return getattr(p, "matchup", "") or (f"{p.team}-{p.opp}" if p.team and p.opp else p.team or "?")


def build_rows(props, *, before: date, season_start: date, h2h_since: date, now: datetime | None = None,
               ) -> tuple[list[Row], dict]:
    """props: slate.Prop list from slate.build (needs .games and .kalshi_all)."""
    stats = {"excluded_injured": 0, "started": 0, "stale": 0}
    seen_all = [b["seen"] for p in props for b in p.books.values() if b.get("seen")]
    seen_all += [k["seen"] for p in props for k in getattr(p, "kalshi_all", p.kalshi) if k.get("seen")]
    fresh_after = max(seen_all) - timedelta(minutes=STALE_MIN) if seen_all else None
    rows: list[Row] = []
    for p in props:
        if p.injury and p.injury.get("code") in EXCLUDED:
            stats["excluded_injured"] += 1
            continue
        if now is not None and p.tip is not None and p.tip <= now:
            stats["started"] += 1
            continue
        games = getattr(p, "games", [])
        last10 = last_values(games, p.stat, before)

        def rates(line):
            return hit_rates(games, p.stat, line, before=before, season_start=season_start, opp=p.opp,
                             h2h_since=h2h_since, side="over")

        # Sleeper leg
        slp = p.books.get("sleeper")
        if slp and slp.get("line") is not None:
            if fresh_after is not None and slp.get("seen") and slp["seen"] < fresh_after:
                stats["stale"] += 1
            else:
                line = float(slp["line"])
                r = rates(line)
                ph = p_hist(r["l10"].hits, r["l10"].n, r["l15"].hits, r["l15"].n, r["season"].hits, r["season"].n)
                pb, has_mkt = p_blend(ph, p.no_vig_over)
                side, prob = side_and_p(pb)
                if prob >= SLEEPER_MIN_P - 1e-9 and r["l10"].n:
                    flags = []
                    if p.consensus is not None and line != p.consensus:
                        easier = (line < p.consensus) if side == "over" else (line > p.consensus)
                        flags.append("slp soft" if easier else "slp hard")
                    if not has_mkt:
                        flags.append("no market")
                    elif abs(ph - p.no_vig_over) > DISAGREE:
                        flags.append("market disagrees")
                    if on_the_line(last10, line, side):
                        flags.append("on the line")
                    rows.append(Row(p.tip, _game(p), p.name, p.stat, "sleeper", side, line, prob, flags=flags,
                                    player_id=p.player_id))

        # Kalshi strikes
        for k in getattr(p, "kalshi_all", p.kalshi):
            if fresh_after is not None and k.get("seen") and k["seen"] < fresh_after:
                stats["stale"] += 1
                continue
            mid = yes_mid(k.get("yes_ask"), k.get("no_ask"))
            if mid is None:
                continue
            l10 = rates(k["line"])["l10"]
            if not l10.n:
                continue
            pk = p_kalshi(l10.hits, l10.n, mid)
            pick = kalshi_pick(pk, k.get("yes_ask"), k.get("no_ask"))
            if pick is None:
                continue
            side, prob, price = pick
            flags = []
            if abs((l10.hits + 0.5) / (l10.n + 1) - mid) > DISAGREE:
                flags.append("market disagrees")
            if on_the_line(last10, k["line"], side):
                flags.append("on the line")
            rows.append(Row(p.tip, _game(p), p.name, p.stat, "kalshi", side, float(k["line"]), prob, price,
                            round(prob - KALSHI_MIN_EDGE, 2), flags, p.player_id))
    return rows, stats


def shortlist(rows: list[Row], max_rows: int = MAX_ROWS) -> tuple[list[Row], int]:
    keep = sorted(rows, key=lambda r: (r.priority, -r.p))[:max_rows]
    far = datetime.max.replace(tzinfo=PT)
    return sorted(keep, key=lambda r: (r.tip or far, -r.p, r.priority, r.player)), len(rows) - len(keep)


# ── output ──────────────────────────────────────────────────────────────────

def _tip(t: datetime | None) -> str:
    if t is None:
        return "  ?   "
    s = t.astimezone(PT).strftime("%I:%M%p").lstrip("0").lower()
    return s[:-1]                       # 4:30p


def _line(r: Row) -> str:
    if r.platform == "kalshi":
        return f"{math.ceil(r.line) if not r.line.is_integer() else int(r.line)}+"
    return f"{r.line:g}"


def _cents(v: float) -> str:
    return f"{round(v * 100):d}c"


def render(rows: list[Row], cut: int, *, slate: str, stats: dict) -> str:
    head = f"{'tip PT':<6} {'game':<7} {'player':<22} {'stat':<4} {'side':<5} {'line':>5} {'p':>4} {'price/max':>9}  flags"
    out = [f"CARD {slate}  RESEARCH-ONLY (UNVALIDATED): hand-set formulas, not backtested, stake $0",
           f"p_blend = .35 hist + .65 no-vig; Sleeper p >= {SLEEPER_MIN_P:.2f}; Kalshi edge >= 5c, price <= 85c, "
           f"p >= {KALSHI_MIN_P:.2f}",
           head, "-" * len(head)]
    for r in rows:
        price = "slp" if r.platform == "sleeper" else f"{_cents(r.price)}/{_cents(r.max_price)}"
        side = r.side.upper() if r.platform == "sleeper" else r.side
        out.append(f"{_tip(r.tip):<6} {r.game:<7} {r.player[:22]:<22} {STAT_LABEL.get(r.stat, r.stat):<4} "
                   f"{side:<5} {_line(r):>5} {r.p:>4.2f} {price:>9}  {', '.join(r.flags)}".rstrip())
    if not rows:
        out.append("(nothing clears the thresholds)")
    out.append(f"{len(rows)} shown" + (f", {cut} cut by stat priority/p" if cut else "") +
               f"; hidden: {stats['excluded_injured']} injured (OUT/D/Q/GTD), {stats['started']} started, "
               f"{stats['stale']} stale quotes")
    out.append("price/max: Kalshi ask / most to pay (p - 5c). slp = Sleeper pick'em leg. "
               "slp soft/hard: Sleeper line easier/harder than consensus for this side.")
    return "\n".join(out)
