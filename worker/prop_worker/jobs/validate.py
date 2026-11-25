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
(ESPN reports whole minutes); plus/minus too against nba_edge_db. Minutes missing in one source and under
1.0 in the other count as a DNP match (dnp_match), not a mismatch.
Severity (per date):
  YELLOW  every mismatch is blk, stl or tov off by exactly 1, and there are at most 2 of them.
          Logged as a warning; the run stays green and the job exits 0.
  RED     any mismatch on pts / reb / ast / fg3m (or any other column), any |diff| >= 2, minutes off
          by more than 1, a sampled player missing from the second source, or more than 2 mismatches.

Rechecks: every stat mismatch on an ESPN row is stored in validate_rechecks. The next nightly run
(the first one validating a later date) re-fetches that game's ESPN box score first. If ESPN's
value changed, game_logs is updated and graded bets on that player / stat / date are regraded
(bets.regrade). Rows from nba_edge_db are never overwritten, so they are not queued.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

from .. import config
from ..bets import regrade
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
DNP_MINUTES = 1.0         # None in one source vs < 1.0 in the other = DNP in both
YELLOW_STATS = ("blk", "stl", "tov")     # a single one of these off by 1 is scorer noise, not a broken feed
MAX_MISMATCHES_YELLOW = 2                # more than this on one date is red whatever the stats
RECHECK_COLUMNS = frozenset(EXACT) | {"min"}
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


def diffs(espn: dict, other: dict | None, label: str, with_plus_minus: bool = False) -> list[dict]:
    """Mismatches as {"text", "stat", "espn", "other", "diff"}; stat/diff are None when there is no
    number to compare (player or stat missing in the second source)."""
    who = f"{espn['name']} ({espn['team']})"
    if other is None:
        return [_mm(f"{who}: not found in {label}")]
    out = []
    for c in EXACT + (("plus_minus",) if with_plus_minus else ()):
        a, b = espn.get(c), other.get(c)
        if b is None:
            out.append(_mm(f"{who}: {c} missing in {label}", c, a))
        elif a is None:
            out.append(_mm(f"{who}: {c} ESPN {a} vs {label} {b}", c, a, b))
        elif int(a) != int(round(float(b))):
            out.append(_mm(f"{who}: {c} ESPN {a} vs {label} {b}", c, a, b, abs(int(a) - int(round(float(b))))))
    m_e, m_o = espn.get("min"), other.get("min")
    if dnp_match(m_e, m_o):
        return out
    if m_o is None or m_e is None:
        out.append(_mm(f"{who}: minutes ESPN {m_e} vs {label} {m_o}", "min", m_e, m_o))
    elif abs(float(m_e) - float(m_o)) > MINUTES_TOLERANCE:
        out.append(_mm(f"{who}: minutes ESPN {m_e} vs {label} {m_o} (> {MINUTES_TOLERANCE:g})", "min", m_e, m_o,
                       abs(float(m_e) - float(m_o))))
    return out


def dnp_match(m_espn, m_other) -> bool:
    """One source has no minutes (DNP) and the other has under DNP_MINUTES (e.g. ESPN None vs PropLine 0.15,
    AJ Green 2026-11-23): both say the player didn't really play. Not a mismatch."""
    if (m_espn is None) == (m_other is None):
        return False
    m = m_other if m_espn is None else m_espn
    return 0 <= float(m) < DNP_MINUTES


def _mm(text, stat=None, espn=None, other=None, diff=None) -> dict:
    return {"text": text, "stat": stat, "espn": espn, "other": other, "diff": diff}


def compare(espn: dict, other: dict | None, label: str, with_plus_minus: bool = False) -> list[str]:
    return [m["text"] for m in diffs(espn, other, label, with_plus_minus)]


def mismatch_severity(m: dict) -> str:
    return "yellow" if m.get("stat") in YELLOW_STATS and m.get("diff") == 1 else "red"


def date_severity(mismatches: list[dict]) -> str:
    """green (none) | yellow (1-2 mismatches, each blk/stl/tov off by 1) | red (anything else)."""
    if not mismatches:
        return "green"
    if len(mismatches) > MAX_MISMATCHES_YELLOW or any(mismatch_severity(m) == "red" for m in mismatches):
        return "red"
    return "yellow"


# ── rechecks: re-fetch ESPN for last run's mismatches ─────────────────────────

_QUEUE = """
insert into validate_rechecks (sport, game_id, player_id, stat, stored_value, other_value, other_label,
                               severity, found_on, found_at)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (sport, game_id, player_id, stat) do nothing"""


def queue_rechecks(ctx: Context, d, items: list[tuple[dict, dict]], label: str) -> int:
    """Store (pick, mismatch) pairs that have a stat to re-fetch, for ESPN rows only."""
    n = 0
    for p, m in items:
        if p["source"] != "espn" or m["stat"] not in RECHECK_COLUMNS:
            continue
        other = m["other"]
        n += ctx.conn.execute(_QUEUE, (config.SPORT, p["game_id"], p["player_id"], m["stat"], m["espn"],
                                       float(other) if other is not None else None, label,
                                       mismatch_severity(m), d, ctx.now)).rowcount
    return n


def run_rechecks(ctx: Context, d) -> tuple[list[dict], list[str]]:
    """Recheck every open mismatch found on a date before d. Returns (results, warnings)."""
    from .boxscores import parse_summary

    open_ = ctx.conn.execute(
        """select r.id, r.game_id, r.player_id, r.stat, r.stored_value, r.found_on, g.espn_event_id,
                  p.full_name, x.external_id
           from validate_rechecks r join games g using (game_id) join players p on p.player_id = r.player_id
           left join player_xref x on x.player_id = r.player_id and x.source = 'espn'
           where r.sport = %s and r.checked_at is null and r.found_on < %s
           order by r.found_on, r.game_id, r.id""", (config.SPORT, d)).fetchall()
    results, warnings, summaries = [], [], {}
    for rid, game_id, pid, stat, stored, found_on, event_id, name, espn_id in open_:
        if game_id not in summaries:
            try:
                r = ctx.session.get(f"{config.ESPN_BASE}/summary", params={"event": event_id})
                r.raise_for_status()
                summaries[game_id] = parse_summary(r.json(), ctx.teams["by_espn_abbr"])
            except Exception as exc:  # noqa: BLE001 — left open, retried next run
                summaries[game_id] = None
                warnings.append(f"recheck: ESPN {event_id} fetch failed ({type(exc).__name__}); retried next run")
        if summaries[game_id] is None:
            continue
        rows, problems = summaries[game_id]
        row = next((x for x in rows if espn_id and x["espn_athlete_id"] == str(espn_id)), None)
        new = row.get(stat) if row else None
        changed = new is not None and (stored is None or float(new) != float(stored))
        if problems:
            outcome = "espn_problem"
        elif row is None or new is None:
            outcome = "not_in_espn"
        else:
            outcome = "updated" if changed else "unchanged"
        regraded = []
        if outcome == "updated":
            ctx.conn.execute(f"""update game_logs set {stat} = %s, fetched_at = %s
                                 where sport = %s and game_id = %s and player_id = %s and source = 'espn'""",
                             (new, ctx.now, config.SPORT, game_id, pid))
            regraded = regrade(ctx.conn, pid, found_on, stat,
                               f"regraded {ctx.now_et:%Y-%m-%d}: ESPN corrected {stat} {_num(stored)} -> {new}",
                               now=ctx.now)
            flips = [f"bet {b} {o}->{n}" for b, o, n in regraded if o != n]
            warnings.append(f"recheck: {name} {stat} on {found_on} ESPN corrected {_num(stored)} -> {new}; "
                            f"{len(regraded)} bet(s) regraded" + (f" ({', '.join(flips)})" if flips else ""))
        elif outcome in ("espn_problem", "not_in_espn"):
            warnings.append(f"recheck: {name} {stat} on {found_on}: {outcome.replace('_', ' ')}")
        ctx.conn.execute("""update validate_rechecks set checked_at = %s, outcome = %s, espn_value = %s,
                                   bets_regraded = %s where id = %s""",
                         (ctx.now, outcome, new, len(regraded), rid))
        results.append({"player": name, "stat": stat, "date": found_on.isoformat(), "stored": _num(stored),
                        "espn_now": new, "outcome": outcome,
                        "regraded": [{"bet": b, "was": o, "now": n} for b, o, n in regraded]})
    return results, warnings


def _num(v):
    return None if v is None else (int(v) if float(v).is_integer() else float(v))


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
    rechecks, warnings = run_rechecks(ctx, d)
    meta = {"date": d.isoformat(), "against": against, "games": scheduled, "finals": finals, "rechecks": rechecks}
    if not scheduled:
        return JobResult(0, 0, expected=False, warnings=warnings, meta={**meta, "skipped": f"no games on {d}"})
    red = []
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

    found_mm = [_mm(t) for t in problems]           # no PropLine event etc.: always red
    pairs = []
    checked = []
    for p in picks:
        if p["player_id"] not in found:          # second source unavailable for this night: already a problem
            continue
        ds = diffs(p, found[p["player_id"]], label, with_plus_minus=against == "nba_edge_db")
        found_mm += ds
        pairs += [(p, m) for m in ds]
        checked.append({"player": p["name"], "team": p["team"], "source": p["source"], "ok": not ds})
    matched = sum(c["ok"] for c in checked)
    mismatches = [m["text"] for m in found_mm]
    severity = date_severity(found_mm)
    summary = f"{len(mismatches)} mismatch(es) vs {label} on {d}: " + "; ".join(mismatches[:8])
    if severity == "red":
        red.append(summary)
    elif severity == "yellow":
        warnings.append(f"YELLOW: {summary}")
    queued = queue_rechecks(ctx, d, pairs, label)
    return JobResult(rows_in=len(picks), rows_out=matched, expected=True, units=None,
                     red_reasons=red, warnings=warnings,
                     meta={**meta, "players": checked, "mismatches": mismatches, "mismatch_severity": severity,
                           "rechecks_queued": queued, "row_sources": sorted({p["source"] for p in picks})})


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
        print(f"  {w}" if w.startswith("YELLOW") else f"  warning: {w}")
    for r in outs[0].red_reasons:
        print(f"  RED: {r}")
    for m in (outs[0].meta or {}).get("mismatches", []):
        print(f"  - {m}")
    return code


if __name__ == "__main__":
    sys.exit(main())
