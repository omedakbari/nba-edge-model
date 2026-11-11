"""Backfill: copy game logs from data/nba_edge.db (SQLite, Mac only) into Postgres through player_xref.

Run (repo root):  PYTHONPATH=worker ./venv/bin/python -m prop_worker.backfill [--dry-run]
                  [--sqlite data/nba_edge.db]

What it does, in ONE transaction:
  1. stages every nba_edge.db game_logs row (COPY into a temp table),
  2. creates a games row for every NBA game id (or attaches nba_game_id to the row the ESPN
     schedule job already made for that date + teams),
  3. upserts game_logs (source 'nba_edge_db') for every row whose player is in player_xref.
     On overlap with an ESPN row for the same game + player, the NBA.com row from nba_edge.db wins
     (decimal minutes, official stats); the ingest never overwrites 'nba_edge_db' rows afterwards,
  4. holds rows for players not in player_xref in pending_rows (never dropped),
     Unchanged rows are not rewritten, so a re-run writes nothing,
  5. checks: Postgres 'nba_edge_db' rows == SQLite rows with a known player, and
     pending rows from this source == SQLite rows without one. Per-season counts are printed.
If a check fails the transaction is rolled back and the exit code is 1. --dry-run always rolls back.
Afterwards (either way) it VACUUMs the touched tables so dead rows don't count against the cap.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .db import connect

DEFAULT_SQLITE = Path(__file__).resolve().parents[2] / "data" / "nba_edge.db"
STATS = ("min", "pts", "reb", "ast", "stl", "blk", "tov", "pf", "fgm", "fga", "fg3m", "fg3a",
         "ftm", "fta", "oreb", "dreb", "plus_minus")
STAGE_COLS = ("nba_player_id", "nba_game_id", "game_date", "season", "season_type", "team_abbr",
              "opp_abbr", "is_home", "inserted_at") + STATS


class BackfillMismatch(RuntimeError):
    pass


def read_sqlite(path: str) -> list[tuple]:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = src.execute(f"""
            select cast(player_id as text), game_id, game_date, season, season_type, team_abbr, opp_abbr,
                   is_home, inserted_at, {", ".join(STATS)}
            from game_logs order by game_date, game_id, player_id""").fetchall()
    finally:
        src.close()
    out = []
    for r in rows:
        r = list(r)
        r[7] = bool(r[7])
        r[8] = _sqlite_ts(r[8])
        r[-1] = None if r[-1] is None else int(round(r[-1]))      # plus_minus REAL -> int
        out.append(tuple(r))
    return out


def _sqlite_ts(value):
    """SQLite datetime('now') is UTC 'YYYY-MM-DD HH:MM:SS'."""
    if not value:
        return None
    return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)


def counts(conn) -> dict:
    row = conn.execute(
        """select (select count(*) from game_logs where sport = %(s)s),
                  (select count(*) from game_logs where sport = %(s)s and source = 'nba_edge_db'),
                  (select count(*) from game_logs where sport = %(s)s and source = 'espn'),
                  (select count(*) from games where sport = %(s)s),
                  (select count(*) from pending_rows where sport = %(s)s and source = 'nba_edge_db'
                       and resolved_at is null)""", {"s": config.SPORT}).fetchone()
    return dict(zip(("game_logs", "nba_edge_db", "espn", "games", "pending"), row))


def backfill(conn, rows: list[tuple]) -> dict:
    """Stage, upsert and verify inside the caller's open transaction. Raises BackfillMismatch."""
    s = config.SPORT
    conn.execute("set local statement_timeout = '15min'")
    conn.execute("drop table if exists pg_temp.stage_logs")
    conn.execute(f"""create temp table stage_logs (
        nba_player_id text, nba_game_id text, game_date date, season text, season_type text,
        team_abbr text, opp_abbr text, is_home boolean, inserted_at timestamptz,
        min numeric, pts int, reb int, ast int, stl int, blk int, tov int, pf int, fgm int, fga int,
        fg3m int, fg3a int, ftm int, fta int, oreb int, dreb int, plus_minus int)""")
    with conn.cursor().copy(f"copy stage_logs ({', '.join(STAGE_COLS)}) from stdin") as cp:
        for r in rows:
            cp.write_row(r)

    bad_teams = conn.execute(
        """select distinct x from (select team_abbr x from stage_logs union select opp_abbr from stage_logs) t
           where not exists (select 1 from teams where sport = %s and abbr = t.x)""", (s,)).fetchall()
    if bad_teams:
        raise BackfillMismatch(f"unknown team abbreviations: {[b[0] for b in bad_teams]}")

    # games: one per NBA game id; home/away from any row (is_home says which side the player was on)
    games_before = conn.execute("select count(*) from games where sport = %s", (s,)).fetchone()[0]
    attached = conn.execute(
        """with g as (
               select distinct on (nba_game_id) nba_game_id, game_date, season, season_type,
                      case when is_home then team_abbr else opp_abbr end as home_team,
                      case when is_home then opp_abbr else team_abbr end as away_team
               from stage_logs order by nba_game_id)
           insert into games (sport, game_date, season, season_type, home_team, away_team, status,
                              nba_game_id, source, fetched_at)
           select %s, game_date, season, season_type, home_team, away_team, 'final', nba_game_id,
                  'nba_edge_db', now() from g
           on conflict (sport, game_date, home_team, away_team) do update set
               nba_game_id = coalesce(games.nba_game_id, excluded.nba_game_id),
               season = coalesce(games.season, excluded.season),
               season_type = coalesce(games.season_type, excluded.season_type)
           where games.nba_game_id is null or games.season is null or games.season_type is null""",
        (s,)).rowcount
    games_created = conn.execute("select count(*) from games where sport = %s", (s,)).fetchone()[0] - games_before
    orphan_games = conn.execute(
        """select count(distinct st.nba_game_id) from stage_logs st
           where not exists (select 1 from games g where g.sport = %s and g.nba_game_id = st.nba_game_id)""",
        (s,)).fetchone()[0]
    if orphan_games:
        raise BackfillMismatch(f"{orphan_games} NBA game id(s) couldn't be attached to a games row "
                               "(a different nba_game_id already holds that date + teams)")

    loaded = conn.execute(
        f"""insert into game_logs (sport, game_id, player_id, team_abbr, opp_abbr, is_home, starter,
                                   did_not_play, {", ".join(STATS)}, source, provider_ts, fetched_at)
            select %s, g.game_id, x.player_id, st.team_abbr, st.opp_abbr, st.is_home, null, false,
                   {", ".join("st." + c for c in STATS)}, 'nba_edge_db', null, coalesce(st.inserted_at, now())
            from stage_logs st
            join games g on g.sport = %s and g.nba_game_id = st.nba_game_id
            join player_xref x on x.sport = %s and x.source = 'nba' and x.external_id = st.nba_player_id
            on conflict (sport, game_id, player_id) do update set
                team_abbr = excluded.team_abbr, opp_abbr = excluded.opp_abbr, is_home = excluded.is_home,
                did_not_play = false, dnp_reason = null,
                {", ".join(f"{c} = excluded.{c}" for c in STATS)},
                source = 'nba_edge_db', provider_ts = null, fetched_at = excluded.fetched_at
            where game_logs.source <> 'nba_edge_db'
               or (game_logs.team_abbr, game_logs.opp_abbr, game_logs.is_home, {", ".join("game_logs." + c for c in STATS)})
                  is distinct from
                  (excluded.team_abbr, excluded.opp_abbr, excluded.is_home, {", ".join("excluded." + c for c in STATS)})""",
        (s, s, s)).rowcount

    held = conn.execute(
        """insert into pending_rows (sport, target, source, dedupe_key, external_id, team_abbr, payload, reason)
           select %s, 'game_logs', 'nba_edge_db', 'nba:' || st.nba_game_id || ':' || st.nba_player_id,
                  st.nba_player_id, st.team_abbr, to_jsonb(st), 'player not in player_xref'
           from stage_logs st
           where not exists (select 1 from player_xref x where x.sport = %s and x.source = 'nba'
                             and x.external_id = st.nba_player_id)
           on conflict (sport, target, source, dedupe_key) do update set payload = excluded.payload
           where pending_rows.payload is distinct from excluded.payload""",
        (s, s)).rowcount

    matched, unmatched_rows, unmatched_players = conn.execute(
        """select count(*) filter (where x.player_id is not null),
                  count(*) filter (where x.player_id is null),
                  count(distinct st.nba_player_id) filter (where x.player_id is null)
           from stage_logs st
           left join player_xref x on x.sport = %s and x.source = 'nba' and x.external_id = st.nba_player_id""",
        (s,)).fetchone()
    per_season = conn.execute(
        """select st.season, count(*) as sqlite_rows,
                  (select count(*) from game_logs l join games g using (game_id)
                   where l.sport = %s and l.source = 'nba_edge_db' and g.season = st.season) as pg_rows
           from stage_logs st group by st.season order by st.season""", (s,)).fetchall()
    after = counts(conn)
    result = {"sqlite_rows": len(rows), "matched_rows": matched, "unmatched_rows": unmatched_rows,
              "unmatched_players": unmatched_players, "upserted": loaded, "held": held,
              "games_created": games_created, "games_touched": attached, "per_season": per_season,
              "after": after}
    problems = []
    if after["nba_edge_db"] != matched:
        problems.append(f"Postgres has {after['nba_edge_db']} nba_edge_db game_logs rows, expected {matched}")
    if after["pending"] < unmatched_rows:
        problems.append(f"pending_rows holds {after['pending']} rows, expected at least {unmatched_rows}")
    if matched + unmatched_rows != len(rows):
        problems.append(f"matched {matched} + unmatched {unmatched_rows} != {len(rows)} SQLite rows")
    for season, n_sqlite, n_pg in per_season:
        if n_pg + _unmatched_in(conn, season) != n_sqlite:
            problems.append(f"season {season}: SQLite {n_sqlite}, Postgres {n_pg}")
    result["problems"] = problems
    if problems:
        raise BackfillMismatch("; ".join(problems))
    return result


def _unmatched_in(conn, season: str) -> int:
    return conn.execute(
        """select count(*) from stage_logs st where st.season = %s and not exists (
               select 1 from player_xref x where x.sport = %s and x.source = 'nba'
               and x.external_id = st.nba_player_id)""", (season, config.SPORT)).fetchone()[0]


def render(before: dict, result: dict, dry_run: bool) -> str:
    a = result["after"]
    lines = [
        f"SQLite nba_edge.db game_logs rows:        {result['sqlite_rows']:>8}",
        f"  with a player in player_xref:          {result['matched_rows']:>8}",
        f"  UNMATCHED rows -> pending_rows:        {result['unmatched_rows']:>8}",
        f"UNMATCHED PLAYERS:                       {result['unmatched_players']:>8}",
        "",
        "Postgres                     before      after",
        f"  game_logs (all)          {before['game_logs']:>8}   {a['game_logs']:>8}",
        f"  game_logs nba_edge_db    {before['nba_edge_db']:>8}   {a['nba_edge_db']:>8}",
        f"  game_logs espn           {before['espn']:>8}   {a['espn']:>8}",
        f"  games                    {before['games']:>8}   {a['games']:>8}   "
        f"({result['games_created']} created)",
        f"  pending (nba_edge_db)    {before['pending']:>8}   {a['pending']:>8}",
        "",
        "season     SQLite   Postgres",
    ]
    lines += [f"  {season}  {n_sqlite:>7}   {n_pg:>8}" for season, n_sqlite, n_pg in result["per_season"]]
    lines.append("")
    lines.append(f"CHECK: Postgres nba_edge_db rows {a['nba_edge_db']} == SQLite rows with a known player "
                 f"{result['matched_rows']}  -> OK")
    lines.append("DRY RUN: rolled back, nothing written." if dry_run else "Committed.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sqlite", default=str(DEFAULT_SQLITE))
    ap.add_argument("--dry-run", action="store_true", help="do everything, print the checks, roll back")
    args = ap.parse_args(argv)
    if config.in_ci():
        raise SystemExit("backfill runs on the Mac only (it reads data/nba_edge.db)")
    if not Path(args.sqlite).exists():
        raise SystemExit(f"{args.sqlite} not found")

    rows = read_sqlite(args.sqlite)
    print(f"read {len(rows)} rows from {args.sqlite}")
    with connect() as conn:
        if not conn.execute("select count(*) from player_xref where source = 'nba'").fetchone()[0]:
            raise SystemExit("player_xref is empty: run `python -m prop_worker.xref seed` first")
        before = counts(conn)
        conn.commit()
        try:
            with conn.transaction(force_rollback=args.dry_run):
                result = backfill(conn, rows)
                print(render(before, result, args.dry_run))
        except BackfillMismatch as exc:
            print(f"FAILED, rolled back: {exc}")
            print(f"Postgres unchanged: {counts(conn)}")
            vacuum(conn)
            return 1
        vacuum(conn)
    return 0


def vacuum(conn) -> None:
    """Reclaim dead rows now (a rolled-back dry run leaves ~all of them) so the 500 MB cap isn't eaten."""
    import psycopg

    conn.commit()
    conn.autocommit = True
    try:
        # Never hang behind a concurrent ingest run holding these tables; autovacuum catches up later.
        conn.execute("set lock_timeout = '30s'")
        conn.execute("vacuum (analyze) game_logs, games, pending_rows")
        mb = conn.execute("select pg_database_size(current_database()) / 1e6").fetchone()[0]
        print(f"vacuumed game_logs, games, pending_rows; database now {mb:.0f} MB of 500 MB")
    except psycopg.errors.LockNotAvailable:
        print("vacuum skipped: tables busy (an ingest run?); autovacuum will reclaim the space")
    finally:
        conn.execute("reset lock_timeout")
        conn.autocommit = False


if __name__ == "__main__":
    sys.exit(main())
