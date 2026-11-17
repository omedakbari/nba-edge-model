"""bets: the bet ledger. Card picks are logged automatically; grading is nightly; marking placed is optional.

Run (Mac, against Supabase):
  python -m prop_worker.bets list [--date 2026-11-16 | --days 7]
  python -m prop_worker.bets placed <id> [stake]          mark a logged pick as placed (stake optional)
  python -m prop_worker.bets card <id> <id> ... [--stake 5] [--mult 3]   group Sleeper legs into one card
  python -m prop_worker.bets add --player "Devin Booker" --stat reb --side no --line 4+ --platform kalshi
                                 --price 62 [--date D] [--stake S] [--mult M] [--p .7] [--source own]
                                 [--placed] [--yes]
  python -m prop_worker.bets void <id>
  python -m prop_worker.bets grade [--through 2026-11-16]   (the nightly job does this at 6am ET)
  python -m prop_worker.bets report [--days 7]

Logged automatically: every leg `slate --card --log` prints (run_daily does this) as source=card,
placed=false, with its p and Kalshi ask. A re-run doesn't duplicate (first logging wins).

Grading (from game_logs, i.e. the ESPN box scores the worker ingests):
  value = stat on that ET date (pra = pts+reb+ast, ra = reb+ast); over/yes wins if value > line,
  under/no if value < line, equal = push. DNP, 0 minutes, or absent from a final game's box score =
  void. Games not final yet stay ungraded and are retried every night.
  A Sleeper card (card_id) is a loss if any leg loses, void if every leg is void/push, else a win.
ROI (report): flat 1 unit per bet, only where the payout is known: Kalshi with a price (win pays
(100 - c) / c), a single Sleeper leg with a multiplier, or a Sleeper card with a multiplier.
Placed bets also need a stake to count. Unknown platform/stake/price -> hit rate only.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import config
from .db import connect
from .hitrates import STATS
from .masking import redact
from .names import normalize_name
from .util import slate_date, utcnow

SIDES = {"over": "over", "o": "over", "under": "under", "u": "under", "yes": "yes", "y": "yes", "no": "no", "n": "no"}
P_BUCKETS = ((0.80, ".80+"), (0.70, ".70-.80"), (0.60, ".60-.70"), (0.0, "<.60"))


# ── parsing / resolving ─────────────────────────────────────────────────────

def parse_line(text: str) -> float:
    """'4+' -> 3.5 (Kalshi strike), '3.5' -> 3.5, 'O3.5'/'U2.5' -> 3.5/2.5."""
    t = str(text).strip().lower().lstrip("ou")
    if t.endswith("+"):
        return float(t[:-1]) - 0.5
    return float(t)


def show_line(line: float, side: str) -> str:
    if side in ("yes", "no") and not float(line).is_integer():
        return f"{math.ceil(line)}+"
    return f"{float(line):g}"


def resolve_player(conn, name: str) -> list[tuple[int, str, str | None]]:
    """Candidates for a typed name: exact normalized match first, else every word must match."""
    norm = normalize_name(name)
    rows = conn.execute("""select player_id, full_name, team_abbr from players
                           where sport = %s and name_norm = %s order by is_active desc, player_id""",
                        (config.SPORT, norm)).fetchall()
    if rows:
        return rows
    words = norm.split()
    if not words:
        return []
    cond = " and ".join(["name_norm ~ %s"] * len(words))
    return conn.execute(f"""select player_id, full_name, team_abbr from players
                            where sport = %s and is_active and {cond} order by full_name""",
                        (config.SPORT, *[rf"\m{w}\M" for w in words])).fetchall()


# ── writing ─────────────────────────────────────────────────────────────────

_INSERT = """
insert into bets (sport, slate_date, placed_at, platform, card_id, player_id, stat, side, line_or_strike,
                  price_cents, payout_mult, stake, p_at_placement, source, placed, note)
values (%(sport)s, %(slate_date)s, %(placed_at)s, %(platform)s, %(card_id)s, %(player_id)s, %(stat)s, %(side)s,
        %(line)s, %(price_cents)s, %(payout_mult)s, %(stake)s, %(p)s, %(source)s, %(placed)s, %(note)s)
{conflict}
returning id"""


def add_bet(conn, *, slate_date: date, player_id: int, stat: str, side: str, line: float, source: str,
            platform: str | None = None, price_cents: float | None = None, payout_mult: float | None = None,
            stake: float | None = None, p: float | None = None, placed: bool = False, card_id: str | None = None,
            placed_at: datetime | None = None, note: str | None = None) -> int | None:
    """Insert one bet; returns its id, or None when a card/claude row for the same pick already exists."""
    if stat not in STATS:
        raise ValueError(f"stat must be one of {STATS}")
    side = SIDES[side.lower()]
    if platform == "kalshi" and side not in ("yes", "no"):
        raise ValueError("Kalshi bets are yes/no")
    if platform == "sleeper" and side not in ("over", "under"):
        raise ValueError("Sleeper bets are over/under")
    conflict = "on conflict do nothing" if source in ("card", "claude") else ""
    row = conn.execute(_INSERT.format(conflict=conflict), {
        "sport": config.SPORT, "slate_date": slate_date, "placed_at": placed_at or (utcnow() if placed else None),
        "platform": platform, "card_id": card_id, "player_id": player_id, "stat": stat, "side": side,
        "line": line, "price_cents": price_cents, "payout_mult": payout_mult, "stake": stake, "p": p,
        "source": source, "placed": placed, "note": note}).fetchone()
    return row[0] if row else None


def log_card(conn, d: date, rows) -> tuple[int, int]:
    """Log every printed card leg (card.Row) as source=card, placed=false. Returns (new, already logged)."""
    new = 0
    for r in rows:
        side = r.side.lower()
        bid = add_bet(conn, slate_date=d, player_id=r.player_id, stat=r.stat, side=side, line=r.line,
                      source="card", platform=r.platform,
                      price_cents=round(r.price * 100, 1) if r.price is not None else None,
                      p=round(r.p, 4), note=", ".join(r.flags) or None)
        new += bid is not None
    conn.commit()
    return new, len(rows) - new


def set_placed(conn, bet_id: int, stake: float | None) -> int:
    n = conn.execute("""update bets set placed = true, placed_at = coalesce(placed_at, now()),
                               stake = coalesce(%s, stake) where id = %s and sport = %s""",
                     (stake, bet_id, config.SPORT)).rowcount
    conn.commit()
    return n


def set_void(conn, bet_id: int) -> int:
    n = conn.execute("""update bets set result = 'void', graded_at = now(),
                               note = concat_ws('; ', note, 'voided by hand') where id = %s and sport = %s""",
                     (bet_id, config.SPORT)).rowcount
    conn.commit()
    return n


def group_card(conn, ids: list[int], stake: float | None, mult: float | None) -> str:
    rows = conn.execute("select id, platform, slate_date from bets where id = any(%s)", (ids,)).fetchall()
    if len(rows) != len(set(ids)):
        raise ValueError(f"unknown bet id(s): {sorted(set(ids) - {r[0] for r in rows})}")
    if any(r[1] not in ("sleeper", None) for r in rows):
        raise ValueError("only Sleeper legs go on a card")
    card_id = f"slp-{min(r[2] for r in rows):%Y%m%d}-{min(ids)}"
    conn.execute("""update bets set card_id = %s, platform = 'sleeper', placed = true,
                           placed_at = coalesce(placed_at, now()), stake = %s, payout_mult = %s
                    where id = any(%s)""", (card_id, stake, mult, ids))
    conn.commit()
    return card_id


# ── grading ─────────────────────────────────────────────────────────────────

def outcome(value: float, side: str, line: float) -> str:
    if value == line:
        return "push"
    up = value > line
    return "win" if up == (side in ("over", "yes")) else "loss"


def _value(stat: str, pts, reb, ast, fg3m) -> float | None:
    parts = {"pts": (pts,), "reb": (reb,), "ast": (ast,), "fg3m": (fg3m,), "pra": (pts, reb, ast),
             "ra": (reb, ast)}[stat]
    return None if any(v is None for v in parts) else float(sum(parts))


def grade_pending(conn, through: date, now: datetime | None = None) -> dict:
    """Grade every ungraded bet with slate_date <= through. Returns counts."""
    now = now or utcnow()
    counts = defaultdict(int)
    pending = conn.execute("""select b.id, b.slate_date, b.player_id, b.stat, b.side, b.line_or_strike, p.team_abbr
                              from bets b join players p on p.player_id = b.player_id
                              where b.sport = %s and b.result is null and b.slate_date <= %s""",
                           (config.SPORT, through)).fetchall()
    for bid, d, pid, stat, side, line, team in pending:
        log = conn.execute(
            """select gl.did_not_play, gl.min, gl.pts, gl.reb, gl.ast, gl.fg3m, g.status
               from game_logs gl join games g on g.game_id = gl.game_id
               where gl.sport = %s and gl.player_id = %s and g.game_date = %s""",
            (config.SPORT, pid, d)).fetchone()
        result = value = None
        if log:
            dnp, mins, pts, reb, ast, fg3m, status = log
            if dnp or not mins:
                result = "void"
            elif status == "final":
                value = _value(stat, pts, reb, ast, fg3m)
                result = outcome(value, side, float(line)) if value is not None else None
        else:
            # absent from the box score of a final game his team played that night -> DNP
            absent = conn.execute(
                """select 1 from games g where g.sport = %s and g.game_date = %s and g.status = 'final'
                     and %s in (g.home_team, g.away_team)
                     and exists (select 1 from game_logs x where x.game_id = g.game_id)""",
                (config.SPORT, d, team)).fetchone()
            if absent:
                result = "void"
        if result is None:
            counts["pending"] += 1
            continue
        conn.execute("update bets set result = %s, actual_value = %s, graded_at = %s where id = %s",
                     (result, value, now, bid))
        counts[result] += 1
    return dict(counts)


# ── reporting ───────────────────────────────────────────────────────────────

@dataclass
class Graded:
    id: int
    slate_date: date
    platform: str | None
    card_id: str | None
    name: str
    stat: str
    side: str
    line: float
    price_cents: float | None
    payout_mult: float | None
    stake: float | None
    p: float | None
    source: str
    placed: bool
    result: str | None
    actual: float | None


def fetch(conn, since: date, until: date | None = None) -> list[Graded]:
    rows = conn.execute(
        """select b.id, b.slate_date, b.platform, b.card_id, p.full_name, b.stat, b.side, b.line_or_strike,
                  b.price_cents, b.payout_mult, b.stake, b.p_at_placement, b.source, b.placed, b.result,
                  b.actual_value
           from bets b join players p on p.player_id = b.player_id
           where b.sport = %s and b.slate_date >= %s and b.slate_date <= coalesce(%s, b.slate_date)
           order by b.slate_date, b.id""", (config.SPORT, since, until)).fetchall()
    f = lambda v: float(v) if v is not None else None       # noqa: E731
    return [Graded(r[0], r[1], r[2], r[3], r[4], r[5], r[6], float(r[7]), f(r[8]), f(r[9]), f(r[10]), f(r[11]),
                   r[12], r[13], r[14], f(r[15])) for r in rows]


def unit_return(b: Graded) -> float | None:
    """Profit of a 1-unit single bet, or None when the payout (or, if placed, the stake) is unknown."""
    if b.result not in ("win", "loss") or b.card_id:
        return None
    if b.placed and b.stake is None:
        return None
    if b.platform == "kalshi" and b.price_cents:
        return (100 - b.price_cents) / b.price_cents if b.result == "win" else -1.0
    if b.platform == "sleeper" and b.payout_mult:
        return b.payout_mult - 1 if b.result == "win" else -1.0
    return None


def card_results(bets: list[Graded]) -> list[dict]:
    """One entry per Sleeper card: result from its legs, unit return if the multiplier is known."""
    legs = defaultdict(list)
    for b in bets:
        if b.card_id:
            legs[b.card_id].append(b)
    out = []
    for cid, ls in sorted(legs.items()):
        res = [b.result for b in ls]
        if any(r is None for r in res) and "loss" not in res:
            result = None
        elif "loss" in res:
            result = "loss"
        elif all(r in ("void", "push") for r in res):
            result = "void"
        else:
            result = "win"
        mult, stake = ls[0].payout_mult, ls[0].stake
        ur = None
        if result in ("win", "loss") and mult and stake is not None:
            ur = mult - 1 if result == "win" else -1.0
        out.append({"card_id": cid, "legs": len(ls), "result": result, "unit_return": ur, "stake": stake,
                    "slate_date": ls[0].slate_date})
    return out


def p_bucket(p: float | None) -> str:
    if p is None:
        return "no p"
    return next(label for lo, label in P_BUCKETS if p >= lo - 1e-9)


def _summ(bets: list[Graded]) -> dict:
    w = sum(b.result == "win" for b in bets)
    lo = sum(b.result == "loss" for b in bets)
    rets = [r for r in (unit_return(b) for b in bets) if r is not None]
    return {"n": len(bets), "w": w, "l": lo, "other": sum(b.result in ("push", "void") for b in bets),
            "pending": sum(b.result is None for b in bets),
            "hit": w / (w + lo) if w + lo else None,
            "roi_n": len(rets), "roi": sum(rets) / len(rets) if rets else None}


def report(bets: list[Graded], days: int, today: date) -> str:
    groups = [("platform", lambda b: b.platform or "unknown"), ("stat", lambda b: b.stat),
              ("side", lambda b: b.side), ("source", lambda b: b.source), ("p bucket", lambda b: p_bucket(b.p)),
              ("placed", lambda b: "placed" if b.placed else "recommended only")]
    head = f"  {'':<18} {'bets':>4} {'W-L':>7} {'hit':>5} {'push/void':>9} {'open':>4} {'ROI (n)':>12}"
    out = [f"BETS REPORT last {days} day(s) through {today}  (research-only; ROI = flat 1u where payout known)",
           head]

    def line(label, s):
        hit = "  -  " if s["hit"] is None else f"{s['hit']:.0%}"
        roi = "-" if s["roi"] is None else f"{s['roi']:+.1%} ({s['roi_n']})"
        return f"  {label[:18]:<18} {s['n']:>4} {s['w']:>3}-{s['l']:<3} {hit:>5} {s['other']:>9} {s['pending']:>4} {roi:>12}"

    out.append(line("ALL", _summ(bets)))
    for title, key in groups:
        out.append(f" by {title}")
        by = defaultdict(list)
        for b in bets:
            by[key(b)].append(b)
        for k in sorted(by, key=str):
            out.append(line(str(k), _summ(by[k])))
    cards = card_results(bets)
    if cards:
        cw = sum(c["result"] == "win" for c in cards)
        cl = sum(c["result"] == "loss" for c in cards)
        rets = [c["unit_return"] for c in cards if c["unit_return"] is not None]
        out.append(f" Sleeper cards: {len(cards)}  {cw}-{cl}" +
                   (f"  ROI {sum(rets) / len(rets):+.1%} ({len(rets)})" if rets else "  ROI -"))
    cal = [b for b in bets if b.p is not None and b.result in ("win", "loss")]
    if cal:
        mean_p = sum(b.p for b in cal) / len(cal)
        actual = sum(b.result == "win" for b in cal) / len(cal)
        out.append(f" calibration: mean p {mean_p:.3f} vs actual {actual:.3f} over {len(cal)} graded bets with a p "
                   f"({actual - mean_p:+.3f})")
    else:
        out.append(" calibration: no graded bets with a p yet")
    return "\n".join(out)


def render_list(bets: list[Graded]) -> str:
    head = f"{'id':>5} {'date':<10} {'plat':<7} {'player':<22} {'stat':<4} {'side':<5} {'line':>5} {'price':>5} " \
           f"{'p':>4} {'src':<6} {'placed':<6} {'stake':>6} {'result':<6} {'actual':>6}"
    out = [head, "-" * len(head)]
    for b in bets:
        price = f"{b.price_cents:g}c" if b.price_cents else (f"x{b.payout_mult:g}" if b.payout_mult else "")
        out.append(f"{b.id:>5} {b.slate_date!s:<10} {b.platform or '?':<7} {b.name[:22]:<22} {b.stat:<4} "
                   f"{b.side:<5} {show_line(b.line, b.side):>5} {price:>5} {'' if b.p is None else f'{b.p:.2f}':>4} "
                   f"{b.source:<6} {'yes' if b.placed else 'no':<6} {'' if b.stake is None else f'{b.stake:g}':>6} "
                   f"{b.result or '':<6} {'' if b.actual is None else f'{b.actual:g}':>6}"
                   + (f"  card {b.card_id}" if b.card_id else ""))
    return "\n".join(out)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None, conn=None, inp=input) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--player", required=True)
    a.add_argument("--player-id", type=int)
    a.add_argument("--stat", required=True, choices=STATS)
    a.add_argument("--side", required=True, choices=sorted(SIDES))
    a.add_argument("--line", required=True, help="3.5, or a Kalshi strike like 4+")
    a.add_argument("--platform", choices=("sleeper", "kalshi"))
    a.add_argument("--date", type=date.fromisoformat)
    a.add_argument("--price", type=float, help="Kalshi price in cents")
    a.add_argument("--mult", type=float, help="Sleeper payout multiplier")
    a.add_argument("--stake", type=float)
    a.add_argument("--p", type=float)
    a.add_argument("--source", default="own", choices=("card", "claude", "discord", "own"))
    a.add_argument("--placed", action="store_true")
    a.add_argument("--card-id")
    a.add_argument("--note")
    a.add_argument("--yes", action="store_true", help="don't ask to confirm the player")
    ls = sub.add_parser("list")
    ls.add_argument("--date", type=date.fromisoformat)
    ls.add_argument("--days", type=int)
    v = sub.add_parser("void")
    v.add_argument("id", type=int)
    for name in ("placed", "set-placed"):
        pl = sub.add_parser(name)
        pl.add_argument("id", type=int)
        pl.add_argument("stake", type=float, nargs="?")
    c = sub.add_parser("card")
    c.add_argument("ids", type=int, nargs="+")
    c.add_argument("--stake", type=float)
    c.add_argument("--mult", type=float)
    g = sub.add_parser("grade")
    g.add_argument("--through", type=date.fromisoformat)
    r = sub.add_parser("report")
    r.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    today = slate_date(utcnow())

    def go(cx) -> int:
        if args.cmd == "add":
            if args.player_id:
                cands = [r for r in resolve_player(cx, args.player) if r[0] == args.player_id] or \
                        cx.execute("select player_id, full_name, team_abbr from players where player_id = %s",
                                   (args.player_id,)).fetchall()
            else:
                cands = resolve_player(cx, args.player)
            if len(cands) != 1:
                print(f"{len(cands)} players match {args.player!r}: " +
                      "; ".join(f"{i} {n} ({t})" for i, n, t in cands[:10]) + " -> use --player-id", file=sys.stderr)
                return 2
            pid, name, team = cands[0]
            if not args.yes and inp(f"{name} ({team}), id {pid}? [y/N] ").strip().lower() != "y":
                print("not added")
                return 1
            bid = add_bet(cx, slate_date=args.date or today, player_id=pid, stat=args.stat, side=args.side,
                          line=parse_line(args.line), source=args.source, platform=args.platform,
                          price_cents=args.price, payout_mult=args.mult, stake=args.stake, p=args.p,
                          placed=args.placed, card_id=args.card_id, note=args.note)
            cx.commit()
            print(f"added bet {bid}" if bid else "already logged")
        elif args.cmd == "list":
            since = args.date or (today - timedelta(days=args.days) if args.days else today)
            print(render_list(fetch(cx, since, args.date)))
        elif args.cmd == "void":
            print("voided" if set_void(cx, args.id) else f"no bet {args.id}")
        elif args.cmd in ("placed", "set-placed"):
            print("marked placed" if set_placed(cx, args.id, args.stake) else f"no bet {args.id}")
        elif args.cmd == "card":
            print(f"card {group_card(cx, args.ids, args.stake, args.mult)}: {len(args.ids)} legs")
        elif args.cmd == "grade":
            counts = grade_pending(cx, args.through or today - timedelta(days=1))
            cx.commit()
            print(f"graded: {counts}")
        elif args.cmd == "report":
            print(report(fetch(cx, today - timedelta(days=args.days)), args.days, today))
        return 0

    try:
        if conn is not None:
            return go(conn)
        with connect() as cx:
            return go(cx)
    except Exception as exc:  # noqa: BLE001
        print(redact(f"bets failed: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
