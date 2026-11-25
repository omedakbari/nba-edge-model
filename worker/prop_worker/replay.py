"""replay: what the card's Kalshi rule would have computed on past slates, with NO lookahead. Read-only.

Run (Mac, against Supabase):
  python -m prop_worker.replay [--since 2026-11-01] [--until 2026-11-23] [--out ../data/calibration/kalshi_replay.csv]

Per slate date D (ET):
  pick time   the slate's first tip minus PICK_LEAD_MIN (the card runs before the first game)
  quote       the newest kalshi_quotes row fetched in [pick time - STALE_MIN, pick time]   (kind 'intraday')
              or, for games whose intraday rows were compacted by prune, the OPENING quote from
              quote_history when it was first seen at or before pick time                    (kind 'opening')
              Closing prices and anything seen after pick time are never used.
  history     game_logs strictly before D (the same L10 hit rate at the strike the card uses)
  pk          card.p_kalshi(h10, n10, mid): the card's raw P(YES)
  outcome     the player's stat in that game (DNP / no box score -> row skipped)
Each row is one strike: p_raw = max(pk, 1 - pk) on the favoured side with that side's outcome, plus what
the card would have shown with its raw rules (selected, side, p, price, outcome) so the calibration report
can look at the selected subset separately.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config
from .card import STALE_MIN, kalshi_pick, p_kalshi, yes_mid
from .db import connect
from .hitrates import GameLine, hit_rates, played, stat_value
from .masking import redact
from .names import season_start
from .slate import read_only

PICK_LEAD_MIN = 60
STATS = tuple(config.KALSHI_SERIES.values())          # pts reb ast fg3m pra
FIELDS = ["date", "game_id", "player_id", "stat", "line", "strike", "quote_kind", "quote_age_min", "yes_ask",
          "no_ask", "mid", "l10_hits", "l10_n", "pk", "side", "p_raw", "price", "outcome", "actual",
          "selected", "sel_side", "sel_p", "sel_price", "sel_outcome"]


def slates(conn, since: date, until: date) -> dict[date, dict]:
    """{D: {'pick': datetime, 'games': {game_id: tip}}} for slates with every game final."""
    out: dict[date, dict] = {}
    for gid, d, tip, status in conn.execute(
            """select game_id, game_date, start_time, status from games
               where sport = %s and game_date between %s and %s and start_time is not null""",
            (config.SPORT, since, until)):
        s = out.setdefault(d, {"games": {}, "final": True})
        s["games"][gid] = tip
        s["final"] &= (status == "final")
    for d, s in list(out.items()):
        if not s["final"]:
            del out[d]
            continue
        s["pick"] = min(s["games"].values()) - timedelta(minutes=PICK_LEAD_MIN)
    return out


def intraday_quotes(conn, gids: list[int], pick: datetime) -> list[tuple]:
    """(game_id, player_id, stat, line, yes_ask, no_ask, fetched_at): newest quote per strike before pick."""
    return conn.execute(
        """select m.game_id, m.player_id, m.stat, m.line, q.yes_ask, q.no_ask, q.fetched_at
           from kalshi_markets m
           join lateral (select yes_ask, no_ask, fetched_at from kalshi_quotes k
                         where k.sport = m.sport and k.ticker = m.ticker
                           and k.fetched_at <= %(pick)s and k.fetched_at >= %(from)s
                         order by k.fetched_at desc, k.id desc limit 1) q on true
           where m.sport = %(s)s and m.market_kind = 'player_prop' and m.game_id = any(%(g)s)
             and m.player_id is not null and m.line is not null""",
        {"s": config.SPORT, "g": gids, "pick": pick, "from": pick - timedelta(minutes=STALE_MIN)}).fetchall()


def opening_quotes(conn, gids: list[int], pick: datetime) -> list[tuple]:
    """Same shape from quote_history: the opening quote, only when it was first seen at/before pick."""
    return conn.execute(
        """select game_id, player_id, stat, line, open_over, open_under, opened_at from quote_history
           where sport = %s and venue = 'kalshi' and game_id = any(%s) and player_id is not null
             and line is not null and stat = any(%s) and opened_at <= %s""",
        (config.SPORT, gids, list(STATS), pick)).fetchall()


def logs(conn, pids: list[int], since: date, until: date) -> dict[int, dict[int, GameLine]]:
    """{player_id: {game_id: GameLine}} for since <= game_date <= until."""
    out: dict[int, dict[int, GameLine]] = defaultdict(dict)
    for pid, gid, gd, opp, mins, dnp, pts, reb, ast, fg3m in conn.execute(
            """select gl.player_id, gl.game_id, g.game_date, gl.opp_abbr, gl.min, gl.did_not_play,
                      gl.pts, gl.reb, gl.ast, gl.fg3m
               from game_logs gl join games g on g.game_id = gl.game_id
               where gl.sport = %s and gl.player_id = any(%s) and g.game_date between %s and %s""",
            (config.SPORT, pids, since, until)):
        out[pid][gid] = GameLine(gd, opp, float(mins) if mins is not None else None, dnp, pts, reb, ast, fg3m)
    return out


def _f(v):
    return float(v) if v is not None else None


def rows_for_slate(d: date, s: dict, quotes: list[tuple], kind: str, history: dict) -> list[dict]:
    out = []
    start = season_start(d)
    for gid, pid, stat, line, yes, no, seen in quotes:
        if stat not in STATS:
            continue
        line, yes, no = float(line), _f(yes), _f(no)
        mid = yes_mid(yes, no)
        if mid is None:
            continue
        games = history.get(pid, {})
        g = games.get(gid)
        if g is None or not played(g) or stat_value(g, stat) is None:
            continue                                            # DNP / no box score: void
        past = [x for x in games.values() if x.game_date < d]
        l10 = hit_rates(past, stat, line, before=d, season_start=start, opp=None,
                        h2h_since=start, side="over")["l10"]
        if not l10.n:
            continue
        pk = p_kalshi(l10.hits, l10.n, mid)
        actual = stat_value(g, stat)
        yes_won = int(actual > line)
        side = "YES" if pk >= 0.5 else "NO"
        pick = kalshi_pick(pk, yes, no)
        row = {"date": d.isoformat(), "game_id": gid, "player_id": pid, "stat": stat, "line": line,
               "strike": int(line + 0.5), "quote_kind": kind,
               "quote_age_min": round((s["pick"] - seen).total_seconds() / 60, 1), "yes_ask": yes, "no_ask": no,
               "mid": round(mid, 4), "l10_hits": l10.hits, "l10_n": l10.n, "pk": round(pk, 5), "side": side,
               "p_raw": round(max(pk, 1 - pk), 5), "price": yes if side == "YES" else no,
               "outcome": yes_won if side == "YES" else 1 - yes_won, "actual": actual,
               "selected": int(pick is not None), "sel_side": "", "sel_p": "", "sel_price": "", "sel_outcome": ""}
        if pick:
            ps, pp, price = pick
            row.update(sel_side=ps, sel_p=round(pp, 5), sel_price=price,
                       sel_outcome=yes_won if ps == "YES" else 1 - yes_won)
        out.append(row)
    return out


def build(conn, since: date, until: date) -> list[dict]:
    rows: list[dict] = []
    for d, s in sorted(slates(conn, since, until).items()):
        gids = list(s["games"])
        intra = intraday_quotes(conn, gids, s["pick"])
        have = {q[0] for q in intra}
        opening = [q for q in opening_quotes(conn, gids, s["pick"]) if q[0] not in have]
        pids = sorted({q[1] for q in intra + opening})
        if not pids:
            continue
        hist = logs(conn, pids, date(season_start(d).year - 1, 10, 1), d)
        rows += rows_for_slate(d, s, intra, "intraday", hist)
        rows += rows_for_slate(d, s, opening, "opening", hist)
        print(f"{d}: {len(intra)} intraday + {len(opening)} opening quotes", file=sys.stderr)
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def main(argv=None, conn=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", type=date.fromisoformat, default=date(2026, 10, 1))
    ap.add_argument("--until", type=date.fromisoformat, default=None, help="default: yesterday (ET)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[2] / "data/calibration/kalshi_replay.csv")
    args = ap.parse_args(argv)
    from .util import slate_date, utcnow
    until = args.until or slate_date(utcnow()) - timedelta(days=1)
    try:
        if conn is not None:
            rows = build(read_only(conn), args.since, until)
        else:
            with connect() as c:
                rows = build(read_only(c), args.since, until)
    except Exception as exc:  # noqa: BLE001
        print(redact(f"replay failed: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1
    write_csv(rows, args.out)
    print(f"{len(rows)} replayed strikes -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
