"""kalshi: open NBA markets (5 player-prop series + game winner / spread / total) -> kalshi_markets, kalshi_quotes.

Run:  python -m prop_worker.jobs.kalshi [--no-alert]

Public API, no auth. Every open market is upserted into kalshi_markets (metadata, latest volume) and
its prices offered to kalshi_quotes, which keeps a row only when a price changes (otherwise the
previous row's last_seen_at moves). Prop markets resolve their player from the ticker fragment
(team + name, via player_xref); an unresolved player keeps player_id null and is listed in
xref_unmatched. Markets attach to games by Kalshi's event code ('26NOV10MIAUTA').

Provider timestamps (updated_time) are stored as Kalshi sent them; ones in the future are counted
in meta so the freshness gates can treat them as red later.
"""

from __future__ import annotations

import re

from .. import config
from ..names import kalshi_player_name, parse_kalshi_ticker
from ..runner import Context, JobResult
from ..util import dec, is_future, parse_ts
from ..xref import External, Linker
from . import main_for

_N_PLUS = re.compile(r"(\d+)\+")


def fetch_series(session, series: str) -> list[dict]:
    markets, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = session.get(f"{config.KALSHI_API}/markets", params=params)
        r.raise_for_status()
        body = r.json()
        if not isinstance(body.get("markets"), list):
            raise ValueError(f"{series}: response has no markets array")
        markets += body["markets"]
        cursor = body.get("cursor")
        if not cursor:
            return markets


def prop_line(m: dict) -> float | None:
    """'25+ points' with floor_strike 24.5 and strike_type greater -> 24.5; inconsistent -> None."""
    strike = dec(m.get("floor_strike"))
    match = _N_PLUS.search(m.get("yes_sub_title") or m.get("title") or "")
    if strike is None or m.get("strike_type") != "greater" or not match:
        return None
    return float(strike) if int(match.group(1)) == int(strike) + 1 else None


def parse_market(m: dict, series: str) -> dict | None:
    t = parse_kalshi_ticker(m.get("ticker", ""))
    if t is None:
        return None
    kind = "player_prop" if series in config.KALSHI_SERIES else config.KALSHI_GAME_SERIES[series]
    row = {
        "ticker": m["ticker"], "event_ticker": m.get("event_ticker") or "", "series_ticker": series,
        "market_kind": kind, "stat": config.KALSHI_SERIES.get(series), "kalshi_fragment": t.fragment,
        "kalshi_event": f"{t.date_code}{t.away}{t.home}", "team_abbr": None, "player_name": None,
        "title": m.get("title"), "yes_sub_title": m.get("yes_sub_title"),
        "floor_strike": dec(m.get("floor_strike")), "strike_type": m.get("strike_type"), "line": None,
        "status": m.get("status"), "result": m.get("result") or None,
        "open_time": parse_ts(m.get("open_time")), "close_time": parse_ts(m.get("close_time")),
        "expected_expiration_time": parse_ts(m.get("expected_expiration_time")),
        "rules_primary": m.get("rules_primary"),
        "volume": dec(m.get("volume_fp")), "open_interest": dec(m.get("open_interest_fp")),
        "provider_ts": parse_ts(m.get("updated_time")),
        "yes_bid": dec(m.get("yes_bid_dollars")), "yes_ask": dec(m.get("yes_ask_dollars")),
        "no_bid": dec(m.get("no_bid_dollars")), "no_ask": dec(m.get("no_ask_dollars")),
        "last_price": dec(m.get("last_price_dollars")),
        "yes_bid_size": dec(m.get("yes_bid_size_fp")), "yes_ask_size": dec(m.get("yes_ask_size_fp")),
    }
    if kind == "player_prop":
        row["team_abbr"], row["player_name"], row["line"] = t.team, kalshi_player_name(m), prop_line(m)
    elif kind == "game_winner":
        row["team_abbr"] = t.tail
    elif kind == "spread":
        row["team_abbr"] = re.sub(r"\d+$", "", t.tail or "") or None
        row["line"] = float(row["floor_strike"]) if row["floor_strike"] is not None else None
    elif kind == "total":
        row["line"] = float(row["floor_strike"]) if row["floor_strike"] is not None else None
    return row


_MARKET = """
insert into kalshi_markets (sport, ticker, event_ticker, series_ticker, market_kind, stat, kalshi_fragment,
    player_id, game_id, team_abbr, title, yes_sub_title, floor_strike, strike_type, line, status, result,
    open_time, close_time, expected_expiration_time, rules_primary, volume, open_interest, source,
    provider_ts, fetched_at)
values (%(sport)s, %(ticker)s, %(event_ticker)s, %(series_ticker)s, %(market_kind)s, %(stat)s,
    %(kalshi_fragment)s, %(player_id)s, %(game_id)s, %(team_abbr)s, %(title)s, %(yes_sub_title)s,
    %(floor_strike)s, %(strike_type)s, %(line)s, %(status)s, %(result)s, %(open_time)s, %(close_time)s,
    %(expected_expiration_time)s, %(rules_primary)s, %(volume)s, %(open_interest)s, 'kalshi',
    %(provider_ts)s, %(fetched_at)s)
on conflict (sport, ticker) do update set
    player_id = coalesce(excluded.player_id, kalshi_markets.player_id),
    game_id = coalesce(excluded.game_id, kalshi_markets.game_id),
    team_abbr = excluded.team_abbr, title = excluded.title, yes_sub_title = excluded.yes_sub_title,
    floor_strike = excluded.floor_strike, strike_type = excluded.strike_type, line = excluded.line,
    status = excluded.status, result = excluded.result, close_time = excluded.close_time,
    expected_expiration_time = excluded.expected_expiration_time, rules_primary = excluded.rules_primary,
    volume = excluded.volume, open_interest = excluded.open_interest,
    provider_ts = excluded.provider_ts, fetched_at = excluded.fetched_at"""

_QUOTE = """
insert into kalshi_quotes (sport, ticker, yes_bid, yes_ask, no_bid, no_ask, last_price, yes_bid_size,
                           yes_ask_size, status, source, provider_ts, fetched_at)
values (%(sport)s, %(ticker)s, %(yes_bid)s, %(yes_ask)s, %(no_bid)s, %(no_ask)s, %(last_price)s,
        %(yes_bid_size)s, %(yes_ask_size)s, %(status)s, 'kalshi', %(provider_ts)s, %(fetched_at)s)"""


def run(ctx: Context) -> JobResult:
    linker = Linker(ctx.conn, now=ctx.now)
    games = dict(ctx.conn.execute(
        "select kalshi_event, game_id from games where sport = %s and kalshi_event is not null",
        (config.SPORT,)).fetchall())
    rows, unparsed, per_series = [], [], {}
    for series in [*config.KALSHI_SERIES, *config.KALSHI_GAME_SERIES]:
        markets = fetch_series(ctx.session, series)
        per_series[series] = len(markets)
        for m in markets:
            row = parse_market(m, series)
            if row is None:
                unparsed.append(m.get("ticker"))
                continue
            rows.append(row)

    no_line = no_game = future = unresolved = 0
    for row in rows:
        row.update(sport=config.SPORT, fetched_at=ctx.now, game_id=games.get(row["kalshi_event"]), player_id=None)
        if row["game_id"] is None:
            no_game += 1
        if row["market_kind"] == "player_prop":
            if row["line"] is None:
                no_line += 1
            if row["kalshi_fragment"]:
                row["player_id"] = linker.resolve(
                    External("kalshi", row["kalshi_fragment"], row["player_name"], row["team_abbr"]))
            unresolved += row["player_id"] is None
        future += is_future(row["provider_ts"], ctx.now)

    if rows:
        ctx.conn.cursor().executemany(_MARKET, rows)
        cur = ctx.conn.cursor()
        cur.executemany(_QUOTE, rows)
        new_quotes = max(cur.rowcount or 0, 0)
    else:
        new_quotes = 0
    linker.flush(commit=False)

    warnings = []
    if unparsed:
        warnings.append(f"{len(unparsed)} ticker(s) not parsed, e.g. {unparsed[:3]}")
    if no_line:
        warnings.append(f"{no_line} prop market(s) whose 'N+' title doesn't match floor_strike (line left null)")
    if unresolved:
        warnings.append(f"{unresolved} prop market(s) without a resolved player (see xref report)")
    if future:
        warnings.append(f"{future} market(s) with updated_time in the future")
    events = len({r["kalshi_event"] for r in rows})
    return JobResult(rows_in=len(rows) + len(unparsed), rows_out=len(rows), expected=True,
                     units=events or None, warnings=warnings,
                     meta={"per_series": per_series, "quotes_new": new_quotes, "events": events,
                           "no_game": no_game, "no_line": no_line, "unresolved_players": unresolved,
                           "future_provider_ts": future})


main_for(__name__, "kalshi", run, __doc__)
