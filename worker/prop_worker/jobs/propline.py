"""propline: book player-prop lines (PropLine free tier) -> book_quotes.

Run:  python -m prop_worker.jobs.propline [--no-alert]

One request per run (free tier = 1,000/day): the bulk odds endpoint for the five prop markets,
filtered server-side to config.BOOK_QUOTE_BOOKS. Only main lines of games that haven't started are
kept; book_quotes stores a row only when a price changes.

Players join directly on PropLine's 'nba:<NBA.com id>' through player_xref. An outcome without an
id (player_key 'name:<normalized name>') is matched by name within the event's two teams; if that
fails it is still stored with player_id null and listed in xref_unmatched. Events attach to games by ET
date + home team, and the game row gets propline_event_id.

provider_ts = the book's own update time when PropLine has it, else PropLine's last_change_at.
Timestamps in the future are stored as sent and counted in meta (the freshness gates hide them).
"""

from __future__ import annotations

from .. import config
from ..names import normalize_name
from ..runner import Context, JobResult
from ..util import dec, et_date, is_future, parse_ts
from ..xref import External, Linker
from . import main_for

_QUOTE = """
insert into book_quotes (sport, source, book, event_id, game_id, player_key, player_id, external_name, market,
                         stat, line_type, line, side, price_american, payout_multiplier, provider_ts, fetched_at)
values (%(sport)s, 'propline', %(book)s, %(event_id)s, %(game_id)s, %(player_key)s, %(player_id)s,
        %(external_name)s, %(market)s, %(stat)s, %(line_type)s, %(line)s, %(side)s, %(price_american)s,
        %(payout_multiplier)s, %(provider_ts)s, %(fetched_at)s)"""


def parse_odds(events: list[dict], now) -> tuple[list[dict], dict]:
    """Main-line Over/Under outcomes of pregame events for our books. Returns (quotes, counts)."""
    quotes, counts = [], {"events": 0, "events_started": 0, "no_line": 0, "no_player_id": 0, "future_ts": 0,
                          "other_line_types": 0, "other_books": 0}
    for e in events or []:
        start = parse_ts(e.get("commence_time"))
        if start is None or start <= now:
            counts["events_started"] += 1
            continue
        counts["events"] += 1
        for b in e.get("bookmakers") or []:
            if b.get("key") not in config.BOOK_QUOTE_BOOKS:
                counts["other_books"] += 1
                continue
            for m in b.get("markets") or []:
                if m.get("key") not in config.PROPLINE_MARKETS:
                    continue
                if m.get("line_type") != "main":
                    counts["other_line_types"] += 1
                    continue
                for o in m.get("outcomes") or []:
                    side = (o.get("name") or "").lower()
                    if side not in ("over", "under"):
                        continue
                    line = dec(o.get("point"))
                    if line is None:
                        counts["no_line"] += 1
                        continue
                    pid = o.get("player_id")
                    name = o.get("description") or ""
                    if not pid:
                        counts["no_player_id"] += 1
                    provider_ts = parse_ts(o.get("book_updated_at")) or parse_ts(o.get("last_change_at")) \
                        or parse_ts(m.get("last_update"))
                    counts["future_ts"] += is_future(provider_ts, now)
                    quotes.append({
                        "event_id": str(e["id"]), "home_team_id": e.get("home_team_id"),
                        "away_team_id": e.get("away_team_id"), "start": start,
                        "book": b["key"], "market": m["key"], "stat": config.PROPLINE_MARKETS[m["key"]],
                        "line_type": "main", "line": line, "side": side,
                        "price_american": int(o["price"]) if o.get("price") is not None else None,
                        "payout_multiplier": dec(o.get("payout_multiplier")),
                        "propline_player_id": pid, "external_name": name,
                        "player_key": pid or f"name:{normalize_name(name)}", "provider_ts": provider_ts,
                    })
    return quotes, counts


def run(ctx: Context) -> JobResult:
    if not config.PROPLINE_API_KEY and not ctx.options.get("propline_key"):
        raise RuntimeError("PROPLINE_API_KEY not set")
    key = ctx.options.get("propline_key") or config.PROPLINE_API_KEY
    upcoming = ctx.conn.execute(
        """select count(*) from games where sport = %s and game_date = %s and start_time > %s
           and status = 'scheduled'""", (config.SPORT, ctx.slate, ctx.now)).fetchone()[0]

    r = ctx.session.get(f"{config.PROPLINE_API}/sports/{config.PROPLINE_SPORT}/odds",
                        params={"markets": ",".join(config.PROPLINE_MARKETS),
                                "bookmakers": ",".join(config.BOOK_QUOTE_BOOKS)},
                        headers={"X-API-Key": key})
    r.raise_for_status()
    headers = getattr(r, "headers", {}) or {}
    remaining = headers.get("x-daily-remaining")
    events = r.json()
    if not isinstance(events, list):
        raise ValueError("PropLine odds response is not a list of events")
    quotes, counts = parse_odds(events, ctx.now)

    # events -> games (ET date + home team), and remember PropLine's id on the game
    game_for: dict[str, int | None] = {}
    for q in quotes:
        if q["event_id"] in game_for:
            continue
        home = ctx.teams["by_propline_id"].get(q["home_team_id"])
        row = ctx.conn.execute(
            "select game_id from games where sport = %s and game_date = %s and home_team = %s",
            (config.SPORT, et_date(q["start"]), home)).fetchone()
        game_for[q["event_id"]] = row[0] if row else None
        if row:
            ctx.conn.execute("update games set propline_event_id = %s where game_id = %s",
                             (q["event_id"], row[0]))

    linker = Linker(ctx.conn, now=ctx.now)
    unresolved = 0
    by_pl = ctx.teams["by_propline_id"]
    for q in quotes:
        if q["propline_player_id"]:
            pid = linker.resolve(External("propline", q["propline_player_id"], q["external_name"], None))
        else:   # e.g. Fanatics lists Ace Bailey as 'Airious Bailey' with no id: match within the two teams
            pid = linker.match_in_teams("propline", q["player_key"], q["external_name"],
                                        [by_pl.get(q["home_team_id"]), by_pl.get(q["away_team_id"])])
        unresolved += pid is None
        q.update(sport=config.SPORT, game_id=game_for.get(q["event_id"]), player_id=pid, fetched_at=ctx.now)

    new_quotes = 0
    if quotes:
        cur = ctx.conn.cursor()
        cur.executemany(_QUOTE, quotes)
        new_quotes = max(cur.rowcount or 0, 0)
    linker.flush(commit=False)

    warnings = []
    if remaining is not None and int(remaining) < config.PROPLINE_QUOTA_WARN:
        warnings.append(f"PropLine quota low: {remaining} requests left today")
    if counts["future_ts"]:
        warnings.append(f"{counts['future_ts']} quote(s) with a provider timestamp in the future")
    if unresolved:
        warnings.append(f"{unresolved} quote(s) without a resolved player (see xref report)")
    no_game = sum(1 for v in game_for.values() if v is None)
    if no_game:
        warnings.append(f"{no_game} PropLine event(s) not matched to a game")
    return JobResult(rows_in=len(quotes), rows_out=len(quotes), expected=upcoming > 0,
                     units=counts["events"] or None, warnings=warnings,
                     meta={**counts, "quotes_new": new_quotes, "quota_remaining": remaining,
                           "unresolved_players": unresolved, "events_unmatched": no_game,
                           "pregame_games_tonight": upcoming})


main_for(__name__, "propline", run, __doc__)
