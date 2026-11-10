"""boxscores: ESPN summary -> game_logs, finals only.

Run:  python -m prop_worker.jobs.boxscores [--date YYYY-MM-DD] [--no-alert]

Loads every final from the last BOXSCORE_LOOKBACK_DAYS days that has no game_logs yet (or every
final on --date). Before writing a game it checks each team's player points add up to the final
score; a game that doesn't is not loaded and the run goes red (it is retried next run).

Players resolve through player_xref (ESPN athlete id, else team + name). A player who can't be
resolved is never dropped: the row goes to pending_rows and the id to xref_unmatched, and every run
first re-applies pending rows whose player has since been linked (override, manual player, roster).

Rows already loaded from nba_edge.db (source 'nba_edge_db') are never overwritten.
DNPs are stored with did_not_play = true and null stats.
"""

from __future__ import annotations

import json
from datetime import timedelta

from .. import config
from ..runner import Context, JobResult
from ..util import to_int
from ..xref import External, Linker
from . import main_for

STAT_KEYS = {"points": "pts", "rebounds": "reb", "assists": "ast", "steals": "stl", "blocks": "blk",
             "turnovers": "tov", "fouls": "pf", "offensiveRebounds": "oreb", "defensiveRebounds": "dreb",
             "plusMinus": "plus_minus"}
SPLIT_KEYS = {"fieldGoalsMade-fieldGoalsAttempted": ("fgm", "fga"),
              "threePointFieldGoalsMade-threePointFieldGoalsAttempted": ("fg3m", "fg3a"),
              "freeThrowsMade-freeThrowsAttempted": ("ftm", "fta")}
COLUMNS = ("min", "pts", "reb", "ast", "stl", "blk", "tov", "pf", "fgm", "fga", "fg3m", "fg3a",
           "ftm", "fta", "oreb", "dreb", "plus_minus")


def parse_summary(payload: dict, by_espn_abbr: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Player rows from an ESPN summary, plus problems that should stop the game from loading."""
    problems: list[str] = []
    header = ((payload.get("header") or {}).get("competitions") or [{}])[0]
    score, side = {}, {}
    for c in header.get("competitors") or []:
        abbr = by_espn_abbr.get((c.get("team") or {}).get("abbreviation"))
        score[abbr], side[abbr] = to_int(c.get("score")), c.get("homeAway")
    teams = list(score)
    if len(teams) != 2 or None in teams:
        return [], [f"header has teams {teams}, expected two NBA teams"]

    rows: list[dict] = []
    for block in (payload.get("boxscore") or {}).get("players") or []:
        team = by_espn_abbr.get((block.get("team") or {}).get("abbreviation"))
        if team not in score:
            problems.append(f"box score team {block.get('team', {}).get('abbreviation')} not in header")
            continue
        opp = next(t for t in teams if t != team)
        stats = (block.get("statistics") or [{}])[0]
        keys = stats.get("keys") or []
        for a in stats.get("athletes") or []:
            athlete = a.get("athlete") or {}
            values = a.get("stats") or []
            dnp = bool(a.get("didNotPlay")) or not values
            row = {"espn_athlete_id": str(athlete.get("id")), "name": athlete.get("displayName"),
                   "team_abbr": team, "opp_abbr": opp, "is_home": side[team] == "home",
                   "starter": bool(a.get("starter")), "did_not_play": dnp,
                   "dnp_reason": a.get("reason") if dnp else None, **{c: None for c in COLUMNS}}
            if not dnp:
                if len(values) != len(keys):
                    problems.append(f"{row['name']}: {len(values)} values for {len(keys)} keys")
                    continue
                for k, v in zip(keys, values):
                    if k == "minutes":
                        row["min"] = to_int(v)
                    elif k in STAT_KEYS:
                        row[STAT_KEYS[k]] = to_int(v)
                    elif k in SPLIT_KEYS and "-" in str(v):
                        made, att = str(v).split("-", 1)
                        row[SPLIT_KEYS[k][0]], row[SPLIT_KEYS[k][1]] = to_int(made), to_int(att)
            rows.append(row)

    for team in teams:
        pts = sum(r["pts"] or 0 for r in rows if r["team_abbr"] == team)
        if not any(r["team_abbr"] == team for r in rows):
            problems.append(f"{team}: no player rows")
        elif score[team] is not None and pts != score[team]:
            problems.append(f"{team}: player points add up to {pts}, final score is {score[team]}")
    return rows, problems


_INSERT = f"""
insert into game_logs (sport, game_id, player_id, team_abbr, opp_abbr, is_home, starter, did_not_play,
                       dnp_reason, {", ".join(COLUMNS)}, source, provider_ts, fetched_at)
values (%(sport)s, %(game_id)s, %(player_id)s, %(team_abbr)s, %(opp_abbr)s, %(is_home)s, %(starter)s,
        %(did_not_play)s, %(dnp_reason)s, {", ".join(f"%({c})s" for c in COLUMNS)}, 'espn', null, %(fetched_at)s)
on conflict (sport, game_id, player_id) do update set
    {", ".join(f"{c} = excluded.{c}" for c in ("team_abbr", "opp_abbr", "is_home", "starter",
                                                "did_not_play", "dnp_reason") + COLUMNS)},
    fetched_at = excluded.fetched_at
where game_logs.source = 'espn'"""

_PENDING = """
insert into pending_rows (sport, target, source, dedupe_key, external_id, external_name, team_abbr,
                          payload, reason)
values (%s, 'game_logs', 'espn', %s, %s, %s, %s, %s::jsonb, 'player not in player_xref')
on conflict (sport, target, source, dedupe_key) do update set payload = excluded.payload"""


def finals_to_load(ctx: Context) -> list[tuple]:
    if ctx.options.get("date"):
        where, params = "g.game_date = %s", [ctx.options["date"]]
    else:
        where = """g.game_date between %s and %s
                   and not exists (select 1 from game_logs l where l.game_id = g.game_id)"""
        params = [ctx.slate - timedelta(days=config.BOXSCORE_LOOKBACK_DAYS), ctx.slate]
    return ctx.conn.execute(
        f"""select g.game_id, g.espn_event_id, g.game_date, g.away_team, g.home_team from games g
            where g.sport = %s and g.status = 'final' and g.espn_event_id is not null and {where}
            order by g.game_date, g.game_id""", [config.SPORT, *params]).fetchall()


def write_rows(conn, linker: Linker, game_id: int, event_id: str, rows: list[dict], fetched_at) -> tuple[int, int]:
    loaded, pending = [], []
    for r in rows:
        pid = linker.resolve(External("espn", r["espn_athlete_id"], r["name"], r["team_abbr"]))
        if pid is None:
            payload = dict(r, game_id=game_id, espn_event_id=event_id)
            pending.append((config.SPORT, f"espn:{event_id}:{r['espn_athlete_id']}", r["espn_athlete_id"],
                            r["name"], r["team_abbr"], json.dumps(payload)))
        else:
            loaded.append(dict(r, sport=config.SPORT, game_id=game_id, player_id=pid, fetched_at=fetched_at))
    if loaded:
        conn.cursor().executemany(_INSERT, loaded)
    if pending:
        conn.cursor().executemany(_PENDING, pending)
    return len(loaded), len(pending)


def apply_pending(conn, linker: Linker, fetched_at) -> int:
    """Re-apply held game_logs rows whose ESPN athlete now resolves (linked since, or newly matchable
    because the player was added via nba_edge.db / manual_players.csv / an override)."""
    held = conn.execute(
        """select id, external_id, external_name, team_abbr, payload from pending_rows
           where sport = %s and target = 'game_logs' and source = 'espn' and resolved_at is null""",
        (config.SPORT,)).fetchall()
    done = 0
    for row_id, ext_id, name, team, payload in held:
        pid = linker.resolve(External("espn", ext_id, name, team))
        if pid is None:
            continue
        conn.execute(_INSERT, dict(payload, sport=config.SPORT, player_id=pid, fetched_at=fetched_at))
        conn.execute("update pending_rows set resolved_at = %s where id = %s", (fetched_at, row_id))
        done += 1
    return done


def run(ctx: Context) -> JobResult:
    linker = Linker(ctx.conn, now=ctx.now)
    reapplied = apply_pending(ctx.conn, linker, ctx.now)
    finals = finals_to_load(ctx)
    rows_in = rows_out = held = 0
    red, loaded_games = [], 0
    for game_id, event_id, gdate, away, home in finals:
        r = ctx.session.get(f"{config.ESPN_BASE}/summary", params={"event": event_id})
        r.raise_for_status()
        rows, problems = parse_summary(r.json(), ctx.teams["by_espn_abbr"])
        rows_in += len(rows)
        if problems:
            red.append(f"{gdate} {away}@{home} (ESPN {event_id}) not loaded: " + "; ".join(problems))
            continue
        n_loaded, n_pending = write_rows(ctx.conn, linker, game_id, event_id, rows, ctx.now)
        rows_out += n_loaded + n_pending
        held += n_pending
        loaded_games += 1
    linker.flush(commit=False)
    warnings = [f"{held} row(s) held in pending_rows: player not in player_xref "
                f"(see `python -m prop_worker.xref report`)"] if held else []
    return JobResult(rows_in=rows_in, rows_out=rows_out, expected=bool(finals), units=loaded_games or None,
                     red_reasons=red, warnings=warnings,
                     meta={"finals": len(finals), "games_loaded": loaded_games, "pending_new": held,
                           "pending_reapplied": reapplied})


main_for(__name__, "boxscores", run, __doc__, date_option=True)
