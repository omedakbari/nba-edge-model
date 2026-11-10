"""schedule: ESPN scoreboard -> games (yesterday through 3 days ahead).

Run:  python -m prop_worker.jobs.schedule [--date YYYY-MM-DD] [--no-alert]

Games are keyed by (ET date, home, away); the ESPN event id is attached to that row, so games the
nba_edge.db backfill created get their ESPN id instead of a duplicate. A rescheduled ESPN event
moves its existing row to the new date. Events whose teams aren't NBA teams (exhibitions) are
skipped and listed in meta, never silently.
"""

from __future__ import annotations

from datetime import timedelta

from .. import config
from ..runner import Context, JobResult
from ..util import et_date, kalshi_event_code, parse_ts, to_int
from . import main_for

SEASON_TYPES = {1: "Preseason", 2: "Regular Season", 3: "Playoffs", 5: "PlayIn"}


def game_status(status: dict) -> str:
    t = (status or {}).get("type") or {}
    name, state = t.get("name", ""), t.get("state", "")
    if "POSTPONED" in name:
        return "postponed"
    if "CANCELED" in name or "CANCELLED" in name:
        return "canceled"
    if t.get("completed"):
        return "final"
    if state == "in":
        return "in_progress"
    return "scheduled"


def season_label(year: int | None) -> str | None:
    """ESPN season year 2027 -> '2026-27'."""
    return f"{year - 1}-{str(year)[2:]}" if year else None


def parse_scoreboard(payload: dict, by_espn_abbr: dict[str, str]) -> tuple[list[dict], list[str]]:
    games, skipped = [], []
    for e in payload.get("events") or []:
        comp = (e.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        home_c, away_c = sides.get("home"), sides.get("away")
        if not home_c or not away_c:
            skipped.append(f"{e.get('id')}: no home/away competitors")
            continue
        home = by_espn_abbr.get(home_c["team"].get("abbreviation"))
        away = by_espn_abbr.get(away_c["team"].get("abbreviation"))
        if not home or not away:
            skipped.append(f"{e.get('id')}: non-NBA teams {away_c['team'].get('abbreviation')}"
                           f"@{home_c['team'].get('abbreviation')}")
            continue
        start = parse_ts(e.get("date") or comp.get("date"))
        status = game_status(e.get("status") or comp.get("status"))
        played = status in ("in_progress", "final")
        gdate = et_date(start)
        season = e.get("season") or {}
        games.append({
            "espn_event_id": str(e["id"]), "start_time": start, "game_date": gdate,
            "season": season_label(season.get("year")), "season_type": SEASON_TYPES.get(season.get("type")),
            "home_team": home, "away_team": away, "status": status,
            "home_score": to_int(home_c.get("score")) if played else None,
            "away_score": to_int(away_c.get("score")) if played else None,
            "kalshi_event": kalshi_event_code(gdate, away, home),
        })
    return games, skipped


_MOVE = """
update games set game_date = %(game_date)s, start_time = %(start_time)s, season = %(season)s,
       season_type = %(season_type)s, home_team = %(home_team)s, away_team = %(away_team)s,
       status = %(status)s, home_score = %(home_score)s, away_score = %(away_score)s,
       kalshi_event = %(kalshi_event)s, source = 'espn', fetched_at = %(fetched_at)s
where sport = %(sport)s and espn_event_id = %(espn_event_id)s"""

_UPSERT = """
insert into games (sport, game_date, season, season_type, start_time, home_team, away_team, status,
                   home_score, away_score, espn_event_id, kalshi_event, source, fetched_at)
values (%(sport)s, %(game_date)s, %(season)s, %(season_type)s, %(start_time)s, %(home_team)s,
        %(away_team)s, %(status)s, %(home_score)s, %(away_score)s, %(espn_event_id)s, %(kalshi_event)s,
        'espn', %(fetched_at)s)
on conflict (sport, game_date, home_team, away_team) do update set
    season = coalesce(excluded.season, games.season),
    season_type = coalesce(excluded.season_type, games.season_type),
    start_time = excluded.start_time, status = excluded.status,
    home_score = excluded.home_score, away_score = excluded.away_score,
    espn_event_id = excluded.espn_event_id, kalshi_event = excluded.kalshi_event,
    source = 'espn', fetched_at = excluded.fetched_at"""


def upsert_games(conn, games: list[dict], fetched_at) -> int:
    n = 0
    for g in games:
        row = dict(g, sport=config.SPORT, fetched_at=fetched_at)
        if conn.execute(_MOVE, row).rowcount == 0:
            conn.execute(_UPSERT, row)
        n += 1
    return n


def run(ctx: Context) -> JobResult:
    days = [ctx.options["date"]] if ctx.options.get("date") else \
        [ctx.slate + timedelta(days=k) for k in config.SCHEDULE_DAYS]
    games, skipped = [], []
    for d in days:
        r = ctx.session.get(f"{config.ESPN_BASE}/scoreboard", params={"dates": d.strftime("%Y%m%d")})
        r.raise_for_status()
        g, s = parse_scoreboard(r.json(), ctx.teams["by_espn_abbr"])
        games += g
        skipped += s
    n = upsert_games(ctx.conn, games, ctx.now)
    by_status: dict[str, int] = {}
    for g in games:
        by_status[g["status"]] = by_status.get(g["status"], 0) + 1
    return JobResult(rows_in=len(games) + len(skipped), rows_out=n, expected=True, units=None,
                     warnings=[f"skipped {s}" for s in skipped],
                     meta={"dates": [d.isoformat() for d in days], "by_status": by_status,
                           "slate_games": sum(g["game_date"] == ctx.slate for g in games)})


main_for(__name__, "schedule", run, __doc__, date_option=True)
