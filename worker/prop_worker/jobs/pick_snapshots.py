"""pick_snapshots: archive each slate's pick-time Kalshi prices before prune compacts them (DECISIONS.md D15).

Runs in the nightly BEFORE prune. For every slate in the last LOOKBACK_DAYS whose pick time (first tip - 60
min, worker/prop_worker/replay.py) has passed, copies the price in effect at pick time for every player-prop
strike (replay.pick_time_rows: newest row stored at/before pick, still seen within 45 min) into
kalshi_pick_snapshots. Idempotent: (sport, slate_date, ticker) is the key; an archived slate is skipped.

OFF by default: needs sql/011_kalshi_pick_snapshots.sql applied and config.PICK_SNAPSHOTS_ENABLED. When off it
records a green, not-expected run. When on but the table is missing it is red, naming the migration.
"""
from __future__ import annotations

from datetime import timedelta

from .. import config
from ..replay import PICK_LEAD_MIN, pick_time_rows
from ..runner import Context, JobResult

LOOKBACK_DAYS = 3          # prune keeps full intraday history until a game is 2 days old


def slates_to_archive(conn, today, lookback: int = LOOKBACK_DAYS) -> dict:
    """{slate_date: (pick_at, [game_id])} for recent slates not archived yet."""
    out = {}
    for d, gids, first_tip in conn.execute(
            """select game_date, array_agg(game_id), min(start_time) from games
               where sport = %s and game_date between %s and %s and start_time is not null
               group by game_date""", (config.SPORT, today - timedelta(days=lookback), today)):
        done = conn.execute("select 1 from kalshi_pick_snapshots where sport = %s and slate_date = %s limit 1",
                            (config.SPORT, d)).fetchone()
        if not done:
            out[d] = (first_tip - timedelta(minutes=PICK_LEAD_MIN), list(gids))
    return out


def run(ctx: Context) -> JobResult:
    if not config.PICK_SNAPSHOTS_ENABLED:
        return JobResult(0, 0, expected=False,
                         meta={"disabled": "set PICK_SNAPSHOTS_ENABLED after applying sql/011_kalshi_pick_snapshots.sql"})
    if ctx.conn.execute("select to_regclass('public.kalshi_pick_snapshots')").fetchone()[0] is None:
        return JobResult(0, 0, red_reasons=["kalshi_pick_snapshots table missing: apply sql/011_kalshi_pick_snapshots.sql"])
    rows_in = rows_out = 0
    archived = {}
    for d, (pick_at, gids) in sorted(slates_to_archive(ctx.conn, ctx.slate).items()):
        if pick_at > ctx.now:
            continue                                   # pick time not reached yet
        rows = pick_time_rows(ctx.conn, gids, pick_at)
        rows_in += len(rows)
        for ticker, gid, pid, stat, line, yes, no, fetched, seen in rows:
            rows_out += ctx.conn.execute(
                """insert into kalshi_pick_snapshots (sport, slate_date, pick_at, ticker, game_id, player_id, stat,
                                                      line, yes_ask, no_ask, fetched_at, last_seen_at, archived_at)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (sport, slate_date, ticker) do nothing""",
                (config.SPORT, d, pick_at, ticker, gid, pid, stat, line, yes, no, fetched, seen, ctx.now)).rowcount
        archived[d.isoformat()] = len(rows)
    ctx.conn.commit()
    warnings = [f"slate {d}: 0 pick-time Kalshi quotes to archive" for d, n in archived.items() if n == 0]
    return JobResult(rows_in, rows_out, expected=bool(archived), warnings=warnings, meta={"archived": archived},
                     volume_rule="warn")
