"""slate: every player prop for a date, with lines, Kalshi strikes, hit rates and injury status. Read-only.

Run (Mac, against Supabase):
  python -m prop_worker.slate [--date 2026-11-12] [--team NYK] [--stat pts] [--min-hit 60]
                              [--side over|under] [--include-injured] [--limit 50] [--json]

It only reads: the connection is read-only (every transaction is BEGIN READ ONLY), so it can't change
a table even by mistake. It doesn't touch migrations, jobs or workflows, and records no job_runs row.

Per player and stat:
  consensus   the line most of DraftKings / FanDuel / BetMGM / Pinnacle offer now (tie -> Pinnacle,
              else the lower line), how many offer it, and the no-vig P(over) at that line
  sleeper     Sleeper's current main line
  kalshi      the 3 open strikes nearest the reference line: YES ask / NO ask in cents, plus the
              L10 hit rate at that strike (you pick Kalshi thresholds yourself)
  hit rates   L5 / L10 / L15 / season / head-to-head (this season and last) vs the reference line,
              from game_logs (rules in prop_worker/hitrates.py)
  injury      the latest official NBA injury report status for that date; if the player isn't on
              the current official report, ESPN's current status (marked "espn")
Reference line = the consensus line; if no consensus book has one, Sleeper's (ref "slp"); if neither,
the Kalshi strike closest to 50c (ref "ksh").
Players listed Out / Doubtful / Questionable / Day-To-Day (GTD) are left out unless --include-injured.
Sorted by L10 hit rate (then L15, season); --min-hit filters on L10 (60 or 0.6).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import config
from .db import connect
from .hitrates import CONSENSUS_BOOKS, STATS, GameLine, consensus_line, hit_rates, no_vig_over
from .masking import redact
from .names import season_start
from .util import slate_date, utcnow

SLEEPER = "sleeper"
EXCLUDED = {"OUT", "D", "Q", "GTD"}
STATUS_CODE = {"out": "OUT", "doubtful": "D", "questionable": "Q", "day-to-day": "GTD", "probable": "P",
               "available": "AVL"}
CURRENT_TOLERANCE = timedelta(minutes=5)   # rows refreshed by the newest report run are "current"
KALSHI_NEAR = 3


@dataclass
class Prop:
    player_id: int
    name: str
    team: str | None
    opp: str | None
    game_id: int
    tip: datetime | None
    stat: str
    books: dict = field(default_factory=dict)        # book -> {"line", "over", "under", "seen"}
    kalshi: list = field(default_factory=list)       # [{"line", "yes_ask", "no_ask", "ticker", "seen"}]
    consensus: float | None = None
    consensus_n: int = 0
    no_vig_over: float | None = None
    ref_line: float | None = None
    ref: str = ""
    rates: dict = field(default_factory=dict)
    injury: dict | None = None


# ── reading ─────────────────────────────────────────────────────────────────

def read_only(conn):
    """Every later transaction on conn is BEGIN READ ONLY: a write raises instead of changing a table."""
    from psycopg.pq import TransactionStatus

    if conn.info.transaction_status != TransactionStatus.IDLE:
        conn.rollback()                  # nothing of ours to keep: slate never writes
    conn.read_only = True
    return conn


def games_on(conn, d: date) -> dict[int, dict]:
    rows = conn.execute("""select game_id, home_team, away_team, start_time, status from games
                           where sport = %s and game_date = %s""", (config.SPORT, d)).fetchall()
    return {r[0]: {"home": r[1], "away": r[2], "tip": r[3], "status": r[4]} for r in rows}


def book_lines(conn, game_ids: list[int]) -> list[tuple]:
    """Latest main-line quote per (book, game, player, stat, side) for the consensus books + Sleeper."""
    return conn.execute(
        """select distinct on (q.book, q.game_id, q.player_id, q.stat, q.side)
                  q.book, q.game_id, q.player_id, q.stat, q.side, q.line, q.price_american, q.last_seen_at
           from book_quotes q
           where q.sport = %s and q.game_id = any(%s) and q.player_id is not null and q.book = any(%s)
             and coalesce(q.line_type, 'main') = 'main'
           order by q.book, q.game_id, q.player_id, q.stat, q.side, q.last_seen_at desc, q.fetched_at desc, q.id desc""",
        (config.SPORT, game_ids, [*CONSENSUS_BOOKS, SLEEPER])).fetchall()


def kalshi_strikes(conn, game_ids: list[int]) -> list[tuple]:
    return conn.execute(
        """select m.game_id, m.player_id, m.stat, m.line, m.ticker, m.status, q.yes_ask, q.no_ask, q.last_seen_at
           from kalshi_markets m
           left join lateral (select yes_ask, no_ask, last_seen_at from kalshi_quotes k
                              where k.sport = m.sport and k.ticker = m.ticker
                              order by k.fetched_at desc, k.id desc limit 1) q on true
           where m.sport = %s and m.market_kind = 'player_prop' and m.game_id = any(%s)
             and m.player_id is not null and m.line is not null""",
        (config.SPORT, game_ids)).fetchall()


def players(conn, ids: list[int]) -> dict[int, tuple[str, str | None]]:
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "select player_id, full_name, team_abbr from players where player_id = any(%s)", (ids,))}


def game_logs(conn, ids: list[int], since: date, before: date) -> dict[int, list[tuple[GameLine, str]]]:
    out = defaultdict(list)
    for pid, gd, team, opp, mins, dnp, pts, reb, ast, fg3m in conn.execute(
            """select gl.player_id, g.game_date, gl.team_abbr, gl.opp_abbr, gl.min, gl.did_not_play,
                      gl.pts, gl.reb, gl.ast, gl.fg3m
               from game_logs gl join games g on g.game_id = gl.game_id
               where gl.sport = %s and gl.player_id = any(%s) and g.game_date >= %s and g.game_date < %s""",
            (config.SPORT, ids, since, before)):
        out[pid].append((GameLine(gd, opp, float(mins) if mins is not None else None, dnp, pts, reb, ast, fg3m), team))
    return out


def injury_status(conn, ids: list[int], d: date) -> dict[int, dict]:
    """Official report for date d where the player is on the newest report; else ESPN's current row."""
    out: dict[int, dict] = {}
    for source, date_cond in (("nba_pdf", "game_date = %(d)s"), ("espn", "true")):
        newest = conn.execute(f"""select max(last_seen_at) from injuries
                                  where sport = %(s)s and source = %(src)s and {date_cond}""",
                              {"s": config.SPORT, "src": source, "d": d}).fetchone()[0]
        if newest is None:
            continue
        rows = conn.execute(
            f"""select distinct on (player_id) player_id, status, reason, last_seen_at
                from injuries where sport = %(s)s and source = %(src)s and {date_cond}
                  and player_id = any(%(ids)s)
                order by player_id, fetched_at desc, id desc""",
            {"s": config.SPORT, "src": source, "d": d, "ids": ids}).fetchall()
        for pid, status, reason, seen in rows:
            if pid in out or seen < newest - CURRENT_TOLERANCE:
                continue                      # already have official, or dropped off the newest report
            out[pid] = {"status": status, "code": STATUS_CODE.get((status or "").lower(), status),
                        "reason": reason, "source": "official" if source == "nba_pdf" else "espn",
                        "as_of": seen.isoformat()}
    return out


# ── assembling ──────────────────────────────────────────────────────────────

def build(conn, d: date, *, side: str = "over") -> tuple[list[Prop], dict]:
    games = games_on(conn, d)
    gids = list(games)
    props: dict[tuple[int, int, str], Prop] = {}

    def prop(gid, pid, stat):
        key = (gid, pid, stat)
        if key not in props:
            props[key] = Prop(pid, "", None, None, gid, games[gid]["tip"], stat)
        return props[key]

    for book, gid, pid, stat, s, line, price, seen in book_lines(conn, gids):
        b = prop(gid, pid, stat).books.setdefault(book, {"line": None, "over": None, "under": None, "seen": None})
        if s == "over" or b["line"] is None:
            b["line"] = float(line)
        b[s] = (float(line), price)
        b["seen"] = max(filter(None, [b["seen"], seen]))
    for gid, pid, stat, line, ticker, status, yes_ask, no_ask, seen in kalshi_strikes(conn, gids):
        prop(gid, pid, stat).kalshi.append({"line": float(line), "ticker": ticker,
                                            "yes_ask": float(yes_ask) if yes_ask is not None else None,
                                            "no_ask": float(no_ask) if no_ask is not None else None,
                                            "seen": seen})

    ids = sorted({p.player_id for p in props.values()})
    who = players(conn, ids) if ids else {}
    start = season_start(d)
    h2h_since = date(start.year - 1, start.month, start.day)
    logs = game_logs(conn, ids, h2h_since, d) if ids else {}
    injuries = injury_status(conn, ids, d) if ids else {}

    for p in props.values():
        name, team = who.get(p.player_id, (str(p.player_id), None))
        recent = sorted(logs.get(p.player_id, []), key=lambda t: t[0].game_date, reverse=True)
        if recent:
            team = recent[0][1] if not team else team
        g = games[p.game_id]
        if team not in (g["home"], g["away"]) and recent and recent[0][1] in (g["home"], g["away"]):
            team = recent[0][1]
        p.name, p.team = name, team
        p.opp = g["away"] if team == g["home"] else g["home"] if team == g["away"] else None
        for b in p.books.values():               # under price only counts at the over's line
            over, under = b.pop("over"), b.pop("under")
            b["over"] = over[1] if over and over[0] == b["line"] else None
            b["under"] = under[1] if under and under[0] == b["line"] else None
        p.consensus, p.consensus_n = consensus_line({bk: v["line"] for bk, v in p.books.items()})
        if p.consensus is not None:
            p.no_vig_over = no_vig_over([(v["over"], v["under"]) for bk, v in p.books.items()
                                         if bk in CONSENSUS_BOOKS and v["line"] == p.consensus])
            p.ref_line, p.ref = p.consensus, "cons"
        elif SLEEPER in p.books:
            p.ref_line, p.ref = p.books[SLEEPER]["line"], "slp"
        elif p.kalshi:
            priced = [k for k in p.kalshi if k["yes_ask"] is not None]
            if priced:
                p.ref_line, p.ref = min(priced, key=lambda k: abs(k["yes_ask"] - 0.5))["line"], "ksh"
        games_played = [t[0] for t in recent]
        if p.ref_line is not None:
            p.rates = hit_rates(games_played, p.stat, p.ref_line, before=d, season_start=start, opp=p.opp,
                                h2h_since=h2h_since, side=side)
        if p.kalshi and p.ref_line is not None:
            near = sorted(p.kalshi, key=lambda k: (abs(k["line"] - p.ref_line), k["line"]))[:KALSHI_NEAR]
            for k in near:
                k["l10"] = hit_rates(games_played, p.stat, k["line"], before=d, season_start=start, opp=p.opp,
                                     h2h_since=h2h_since, side=side)["l10"]
            p.kalshi = sorted(near, key=lambda k: k["line"])
        p.injury = injuries.get(p.player_id)
    info = {"date": d.isoformat(), "games": len(games), "side": side,
            "props_total": len(props), "season_start": start.isoformat(), "h2h_since": h2h_since.isoformat()}
    return list(props.values()), info


def select(props: list[Prop], *, team: str | None = None, stat: str | None = None, min_hit: float | None = None,
           include_injured: bool = False) -> tuple[list[Prop], dict]:
    dropped = {"injured": 0, "no_line": 0}
    out = []
    for p in props:
        if team and team.upper() not in (p.team, p.opp):
            continue
        if stat and p.stat != stat:
            continue
        if p.ref_line is None:
            dropped["no_line"] += 1
            continue
        if not include_injured and p.injury and p.injury["code"] in EXCLUDED:
            dropped["injured"] += 1
            continue
        l10 = p.rates["l10"].pct
        if min_hit is not None and (l10 is None or l10 < min_hit):
            continue
        out.append(p)

    def key(p):
        r = p.rates
        return tuple(-(r[k].pct if r[k].pct is not None else -1) for k in ("l10", "l15", "season")) + (p.name,)
    return sorted(out, key=key), dropped


# ── output ──────────────────────────────────────────────────────────────────

def _pct(rate) -> str:
    return "  -  " if rate is None or not rate.n else f"{rate.hits:>2}/{rate.n:<2}"


def _line(v) -> str:
    return "-" if v is None else f"{v:g}"


def _price(v) -> str:
    return "" if v is None else f"{int(v):+d}"


def _cents(v) -> str:
    return "--" if v is None else f"{round(v * 100):d}"


def render(props: list[Prop], info: dict, dropped: dict) -> str:
    head = (f"{'player':<24} {'tm':<3} {'vs':<3} {'stat':<4} {'ref':>5} {'src':<4} {'nv%':>4} {'slp':>5} "
            f"{'L5':>5} {'L10':>5} {'L15':>5} {'szn':>5} {'h2h':>5}  {'inj':<4} kalshi (line: yes/no c, L10)")
    lines = [f"slate {info['date']}: {info['games']} game(s), {len(props)} prop(s) shown "
             f"({info['side']} hit rates; {dropped['injured']} injured hidden, {dropped['no_line']} without a line)",
             head, "-" * len(head)]
    for p in props:
        slp = p.books.get(SLEEPER, {}).get("line")
        nv = "" if p.no_vig_over is None else f"{p.no_vig_over * 100:.0f}"
        inj = "" if not p.injury else p.injury["code"] + ("*" if p.injury["source"] == "espn" else "")
        ksh = "  ".join(f"{_line(k['line'])}: {_cents(k['yes_ask'])}/{_cents(k['no_ask'])}"
                        f" ({_pct(k.get('l10')).strip()})" for k in p.kalshi)
        src = {"cons": f"c{p.consensus_n}", "slp": "slp", "ksh": "ksh"}[p.ref]
        r = p.rates
        lines.append(f"{p.name[:24]:<24} {p.team or '?':<3} {p.opp or '?':<3} {p.stat:<4} {_line(p.ref_line):>5} "
                     f"{src:<4} {nv:>4} {_line(slp):>5} {_pct(r['l5']):>5} {_pct(r['l10']):>5} {_pct(r['l15']):>5} "
                     f"{_pct(r['season']):>5} {_pct(r['h2h']):>5}  {inj:<4} {ksh}")
    lines.append("src: c<n> = consensus of n books, slp = Sleeper line, ksh = Kalshi strike nearest 50c. "
                 "nv% = no-vig P(over) at the consensus line. inj: official report; * = ESPN (not on the "
                 "official report). Hit rates = hits/games.")
    return "\n".join(lines)


def to_json(props: list[Prop], info: dict, dropped: dict) -> str:
    def enc(p: Prop) -> dict:
        return {"player_id": p.player_id, "name": p.name, "team": p.team, "opp": p.opp, "game_id": p.game_id,
                "tip": p.tip, "stat": p.stat, "ref_line": p.ref_line, "ref": p.ref, "consensus": p.consensus,
                "consensus_books": p.consensus_n, "no_vig_over": p.no_vig_over,
                "books": p.books, "kalshi": [{**k, "l10": k["l10"].as_dict() if k.get("l10") else None}
                                             for k in p.kalshi],
                "rates": {k: v.as_dict() for k, v in p.rates.items()}, "injury": p.injury}
    return json.dumps({**info, "dropped": dropped, "props": [enc(p) for p in props]}, default=str, indent=1)


def parse_min_hit(v: str) -> float:
    x = float(v)
    if x > 1:
        x /= 100
    if not 0 <= x <= 1:
        raise argparse.ArgumentTypeError("--min-hit is a percentage (60) or fraction (0.6)")
    return x


def main(argv=None, conn=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", type=date.fromisoformat, help="ET game date (default: today's slate)")
    ap.add_argument("--team", help="team abbreviation, either side of the game (e.g. NYK)")
    ap.add_argument("--stat", choices=STATS)
    ap.add_argument("--min-hit", type=parse_min_hit, help="minimum L10 hit rate: 60 or 0.6")
    ap.add_argument("--side", choices=("over", "under"), default="over")
    ap.add_argument("--include-injured", action="store_true", help="show players listed Q/D/GTD/OUT")
    ap.add_argument("--limit", type=int, help="show only the top N")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the table")
    args = ap.parse_args(argv)
    d = args.date or slate_date(utcnow())

    def go(c) -> int:
        props, info = build(read_only(c), d, side=args.side)
        shown, dropped = select(props, team=args.team, stat=args.stat, min_hit=args.min_hit,
                                include_injured=args.include_injured)
        if args.limit:
            shown = shown[:args.limit]
        print(to_json(shown, info, dropped) if args.json else render(shown, info, dropped))
        if args.stat == "ra" and not any(p.stat == "ra" for p in props):
            print("note: no rebounds+assists lines are collected yet (PropLine's player_rebounds_assists "
                  "isn't in the ingest list, and Kalshi has no RA series); adding it is a job change for "
                  "after the Phase 2 gate.", file=sys.stderr)
        if not info["games"]:
            print(f"no games on {d}", file=sys.stderr)
        return 0

    try:
        if conn is not None:
            return go(conn)
        with connect() as c:
            return go(c)
    except Exception as exc:  # noqa: BLE001
        print(redact(f"slate failed: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
