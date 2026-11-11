"""validate: 10 random players' ESPN game_logs rows from the last game night vs a second source.

Run:  python -m prop_worker.jobs.validate [--date YYYY-MM-DD] [--against propline|nba_edge_db]
                                           [--sqlite data/nba_edge.db] [--players 10] [--no-alert]

Second sources:
  propline     PropLine's free box-score stats (/events/{id}/stats). Runs anywhere; the nightly
               workflow uses it. One request per game in the sample.
  nba_edge_db  data/nba_edge.db (NBA.com via nba_api, Mac only). The old pipeline must already have
               ingested that night, or the check fails saying so.

Date: LAST NIGHT only (the slate before today's, rolling over at 6am ET), or --date. It never falls
back to an older date. If last night had no games the run is green and not expected; if any of last
night's finals has no game_logs rows the run is red and names the games.
Rows: whatever game_logs source holds that night (ESPN, or nba_edge_db where the backfill superseded
ESPN), played rows only, chosen with a seed derived from the date so a re-run checks the same players.
Against nba_edge_db only ESPN rows are sampled (comparing nba_edge.db with itself proves nothing).
Compared: pts reb ast fg3m fg3a fgm fga ftm fta oreb dreb stl blk tov pf exactly; minutes within 1
(ESPN reports whole minutes); plus/minus too against nba_edge_db.
Red on ANY mismatch, including a sampled player missing from the second source.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

from .. import config
from ..names import Candidate, PlayerIndex, normalize_name
from ..runner import Context, JobResult, run_all
from ..util import et_date, parse_ts

EXACT = ("pts", "reb", "ast", "fg3m", "fg3a", "fgm", "fga", "ftm", "fta", "oreb", "dreb", "stl", "blk", "tov", "pf")
PROPLINE_STATS = {"points": "pts", "rebounds": "reb", "assists": "ast", "threes": "fg3m",
                  "threes_attempted": "fg3a", "field_goals_made": "fgm", "field_goals_attempted": "fga",
                  "free_throws_made": "ftm", "free_throws_attempted": "fta", "offensive_rebounds": "oreb",
                  "defensive_rebounds": "dreb", "steals": "stl", "blocks": "blk", "turnovers": "tov",
                  "personal_fouls": "pf", "minutes": "min"}
MINUTES_TOLERANCE = 1.0
DEFAULT_SQLITE = Path(__file__).resolve().parents[3] / "data" / "nba_edge.db"


def target_date(ctx: Context):
    """Last night: the slate before the current one. Never an older fallback."""
    return ctx.options.get("date") or (ctx.slate - timedelta(days=1))


def missing_box_scores(ctx: Context, d) -> list[str]:
    rows = ctx.conn.execute(
        """select g.away_team || '@' || g.home_team from games g
           where g.sport = %s and g.game_date = %s and g.status = 'final'
             and not exists (select 1 from game_logs l where l.game_id = g.game_id)
           order by g.start_time""", (config.SPORT, d)).fetchall()
    return [r[0] for r in rows]


def sample(ctx: Context, d, n: int, sources=("espn", "nba_edge_db")) -> list[dict]:
    rows = ctx.conn.execute(
        f"""select l.player_id, p.full_name, l.team_abbr, g.game_id, g.espn_event_id, g.propline_event_id,
                   g.home_team, g.away_team, g.game_date, x.external_id as nba_id, l.source, l.min,
                   {", ".join("l." + c for c in EXACT)}, l.plus_minus
            from game_logs l join games g using (game_id) join players p using (player_id)
            left join player_xref x on x.player_id = l.player_id and x.source = 'nba'
            where l.sport = %s and l.source = any(%s) and not l.did_not_play and g.game_date = %s
            order by l.player_id, g.game_id""", (config.SPORT, list(sources), d)).fetchall()
    cols = ["player_id", "name", "team", "game_id", "espn_event_id", "propline_event_id", "home", "away",
            "game_date", "nba_id", "source", "min", *EXACT, "plus_minus"]
    rows = [dict(zip(cols, r)) for r in rows]
    return random.Random(f"validate:{d.isoformat()}").sample(rows, min(n, len(rows)))


def compare(espn: dict, other: dict | None, label: str, with_plus_minus: bool = False) -> list[str]:
    who = f"{espn['name']} ({espn['team']})"
    if other is None:
        return [f"{who}: not found in {label}"]
    out = []
    for c in EXACT + (("plus_minus",) if with_plus_minus else ()):
        a, b = espn.get(c), other.get(c)
        if b is None:
            out.append(f"{who}: {c} missing in {label}")
        elif a is None or int(a) != int(round(float(b))):
            out.append(f"{who}: {c} ESPN {a} vs {label} {b}")
    m_e, m_o = espn.get("min"), other.get("min")
    if m_o is None or m_e is None:
        out.append(f"{who}: minutes ESPN {m_e} vs {label} {m_o}")
    elif abs(float(m_e) - float(m_o)) > MINUTES_TOLERANCE:
        out.append(f"{who}: minutes ESPN {m_e} vs {label} {m_o} (> {MINUTES_TOLERANCE:g})")
    return out


# ── second source: PropLine ──────────────────────────────────────────────────

def parse_propline_stats(payload: dict) -> dict[tuple[str, str], dict]:
    """{(team_abbr, display name): {our stat: value}} from /events/{id}/stats."""
    players: dict[tuple[str, str], dict] = {}
    for r in payload.get("stats") or []:
        stat = PROPLINE_STATS.get(r.get("stat_type"))
        if r.get("player_name") and stat:
            players.setdefault((r.get("team_abbr"), r["player_name"]), {})[stat] = r.get("stat_value")
    return players


def find_in(players: dict[tuple[str, str], dict], name: str, team: str) -> dict | None:
    """Same team, then the usual name tiers (accents, Jr., Nic/Nicolas) — never across teams."""
    keyed = list(players.items())
    index = PlayerIndex([Candidate(i, n, t, normalize_name(n)) for i, ((t, n), _) in enumerate(keyed)])
    hit = index.match(name, team)
    return keyed[hit.player_id][1] if hit.player_id is not None else None


def propline_event_id(ctx: Context, game: dict, key: str) -> str | None:
    """The PropLine event for this game: same ET date AND same home team. Matched on games.game_date,
    which is always set (backfilled games have no start_time)."""
    if game["propline_event_id"]:
        return game["propline_event_id"]
    days_back = max(3, (ctx.now_et.date() - game["game_date"]).days + 2)
    r = ctx.session.get(f"{config.PROPLINE_API}/sports/{config.PROPLINE_SPORT}/scores",
                        params={"days_from": days_back}, headers={"X-API-Key": key})
    r.raise_for_status()
    by_pl = ctx.teams["by_propline_id"]
    for e in r.json() or []:
        start = parse_ts(e.get("commence_time"))
        if start and et_date(start) == game["game_date"] and by_pl.get(e.get("home_team_id")) == game["home"]:
            return str(e["id"])
    return None


def second_source_propline(ctx: Context, picks: list[dict]) -> tuple[dict, list[str]]:
    key = ctx.options.get("propline_key") or config.PROPLINE_API_KEY
    if not key:
        raise RuntimeError("PROPLINE_API_KEY not set")
    found, problems, cache = {}, [], {}
    for p in picks:
        if p["game_id"] not in cache:
            eid = propline_event_id(ctx, p, key)
            if eid is None:
                cache[p["game_id"]] = None
                problems.append(f"{p['away']}@{p['home']}: no PropLine event found")
            else:
                r = ctx.session.get(f"{config.PROPLINE_API}/sports/{config.PROPLINE_SPORT}/events/{eid}/stats",
                                    headers={"X-API-Key": key})
                r.raise_for_status()
                cache[p["game_id"]] = parse_propline_stats(r.json())
        stats = cache[p["game_id"]]
        found[p["player_id"]] = find_in(stats, p["name"], p["team"]) if stats is not None else None
    return found, problems


# ── second source: nba_edge.db ───────────────────────────────────────────────

def second_source_sqlite(path: str, d, picks: list[dict]) -> tuple[dict, list[str]]:
    if not Path(path).exists():
        raise RuntimeError(f"{path} not found (the nba_edge_db check runs on the Mac)")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        n = src.execute("select count(*) from game_logs where game_date = ?", (d.isoformat(),)).fetchone()[0]
        if n == 0:
            return {}, [f"nba_edge.db has no game_logs for {d}: run the old pipeline's ingestion for that night first"]
        found = {}
        for p in picks:
            row = src.execute(
                f"select min, plus_minus, {', '.join(EXACT)} from game_logs where player_id = ? and game_date = ?",
                (int(p["nba_id"]), d.isoformat())).fetchone() if p["nba_id"] else None
            found[p["player_id"]] = dict(zip(("min", "plus_minus", *EXACT), row)) if row else None
        return found, []
    finally:
        src.close()


def run(ctx: Context) -> JobResult:
    d = target_date(ctx)
    against = ctx.options.get("against") or "propline"
    n = ctx.options.get("players") or 10
    scheduled, finals = ctx.conn.execute(
        """select count(*) filter (where status not in ('postponed', 'canceled')),
                  count(*) filter (where status = 'final')
           from games where sport = %s and game_date = %s""", (config.SPORT, d)).fetchone()
    meta = {"date": d.isoformat(), "against": against, "games": scheduled, "finals": finals}
    if not scheduled:
        return JobResult(0, 0, expected=False, meta={**meta, "skipped": f"no games on {d}"})
    red, warnings = [], []
    if finals < scheduled:
        warnings.append(f"{scheduled - finals} of {scheduled} game(s) on {d} not final yet")
    missing = missing_box_scores(ctx, d)
    if missing:
        red.append(f"last night's box scores are missing for {len(missing)} of {finals} final(s) on {d}: "
                   + ", ".join(missing))
    sources = ("espn",) if against == "nba_edge_db" else ("espn", "nba_edge_db")
    picks = sample(ctx, d, n, sources)
    if not picks:
        if finals and not missing:
            red.append(f"no ESPN rows on {d} to compare with nba_edge.db (that night's rows came from nba_edge.db itself)"
                       if against == "nba_edge_db" else f"no played game_logs rows on {d}")
        return JobResult(0, 0, expected=bool(finals), red_reasons=red, warnings=warnings, meta=meta)

    if against == "propline":
        found, problems = second_source_propline(ctx, picks)
        label = "PropLine"
    elif against == "nba_edge_db":
        found, problems = second_source_sqlite(ctx.options.get("sqlite") or str(DEFAULT_SQLITE), d, picks)
        label = "nba_edge.db"
    else:
        raise ValueError(f"unknown --against {against!r}")

    mismatches = list(problems)
    checked = []
    for p in picks:
        if p["player_id"] not in found:          # second source unavailable for this night: already a problem
            continue
        diffs = compare(p, found[p["player_id"]], label, with_plus_minus=against == "nba_edge_db")
        mismatches += diffs
        checked.append({"player": p["name"], "team": p["team"], "source": p["source"], "ok": not diffs})
    matched = sum(c["ok"] for c in checked)
    if mismatches:
        red.append(f"{len(mismatches)} mismatch(es) vs {label} on {d}: " + "; ".join(mismatches[:8]))
    return JobResult(rows_in=len(picks), rows_out=matched, expected=True, units=None,
                     red_reasons=red, warnings=warnings,
                     meta={**meta, "players": checked, "mismatches": mismatches,
                           "row_sources": sorted({p["source"] for p in picks})})


def main(argv=None) -> int:
    from datetime import date

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", type=date.fromisoformat)
    ap.add_argument("--against", choices=("propline", "nba_edge_db"), default="propline")
    ap.add_argument("--sqlite", default=str(DEFAULT_SQLITE))
    ap.add_argument("--players", type=int, default=10)
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args(argv)
    code, outs = run_all([("validate", run)], alert=not args.no_alert,
                         options={"date": args.date, "against": args.against, "sqlite": args.sqlite,
                                  "players": args.players})
    for c in (outs[0].meta or {}).get("players", []):
        print(f"  {'OK      ' if c['ok'] else 'MISMATCH'} {c['player']} ({c['team']}, row from {c['source']})")
    for w in outs[0].warnings:
        print(f"  warning: {w}")
    for r in outs[0].red_reasons:
        print(f"  RED: {r}")
    for m in (outs[0].meta or {}).get("mismatches", []):
        print(f"  - {m}")
    return code


if __name__ == "__main__":
    sys.exit(main())
