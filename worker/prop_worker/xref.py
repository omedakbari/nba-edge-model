"""player_xref: one internal player_id per player, mapped to every source's id.

Run:
  python -m prop_worker.xref seed --sqlite ../data/nba_edge.db   (Mac only) players + NBA.com ids
  python -m prop_worker.xref link [--skip espn,kalshi,propline]  ESPN / Kalshi / PropLine ids
  python -m prop_worker.xref report [--csv unmatched.csv] [--fail-on-unmatched]

How each source is matched:
  nba       seeded from nba_edge.db; defines the internal players
  propline  direct join: PropLine's player_id is 'nba:<NBA.com id>'
  espn      team + normalized name (ESPN roster gives the current team)
  kalshi    team + normalized name (team from the ticker fragment, name from the title)
  fallback  name unique league-wide and our team for that player is unknown or from before this
            season (offseason moves, rookies without game logs) -> name_unique_stale_team
  any       worker/xref_overrides.csv (source, external_id, nba_id) wins over matching
  manual    worker/manual_players.csv (nba_id, full_name, team_abbr, note) adds players that aren't in
            nba_edge.db yet (just drafted / signed). nba_edge.db wins once it has them.

Nothing is dropped: every id that can't be matched is written to xref_unmatched with the reason
and candidate players, and `report` prints it. Once matched (or overridden), the row gets resolved_at.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import config
from .db import connect
from .http import make_session
from .names import (Candidate, PlayerIndex, kalshi_player_name, normalize_name, parse_kalshi_ticker,
                    season_start)

OVERRIDES = Path(__file__).resolve().parent.parent / "xref_overrides.csv"
MANUAL_PLAYERS = Path(__file__).resolve().parent.parent / "manual_players.csv"
ONE_PER_PLAYER = {"nba", "espn", "propline"}  # kalshi may hold several fragments (trades, jersey changes)


@dataclass(frozen=True)
class External:
    source: str
    external_id: str
    name: str
    team_abbr: str | None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── parsers (pure; tested against saved responses) ───────────────────────────

def parse_espn_roster(payload: dict, espn_to_abbr: dict[str, str]) -> list[External]:
    team = (payload.get("team") or {}).get("abbreviation")
    abbr = espn_to_abbr.get(team or "")
    out = []
    for a in payload.get("athletes") or []:
        if a.get("id") and (a.get("fullName") or a.get("displayName")):
            out.append(External("espn", str(a["id"]), a.get("fullName") or a["displayName"], abbr))
    return out


def parse_kalshi_props(markets: list[dict]) -> list[External]:
    seen: dict[str, External] = {}
    for m in markets:
        t = parse_kalshi_ticker(m.get("ticker", ""))
        if not t or not t.fragment:
            continue
        seen.setdefault(t.fragment, External("kalshi", t.fragment, kalshi_player_name(m), t.team))
    return list(seen.values())


def parse_propline_players(events: list[dict]) -> list[External]:
    seen: dict[str, External] = {}
    for e in events or []:
        for b in e.get("bookmakers") or []:
            for m in b.get("markets") or []:
                for o in m.get("outcomes") or []:
                    pid, name = o.get("player_id"), o.get("description")
                    if pid and name:
                        seen.setdefault(pid, External("propline", pid, name, None))
    return list(seen.values())


def load_overrides(path: Path = OVERRIDES) -> dict[tuple[str, str], str]:
    if not path.exists():
        return {}
    with path.open() as f:
        return {(r["source"].strip(), r["external_id"].strip()): r["nba_id"].strip()
                for r in csv.DictReader(f) if r.get("source") and not r["source"].startswith("#")}


# ── Linker: resolve external ids, batch the writes ───────────────────────────

class Linker:
    """Resolve (source, external_id, name, team) to a player_id. Ingest jobs use the same class.

    resolve() only touches memory; flush() writes links, unmatched rows and team updates, then commits.
    """

    def __init__(self, conn, sport: str = config.SPORT, overrides: dict | None = None, now=None):
        self.conn, self.sport = conn, sport
        self.now = now or utcnow()
        self.today = self.now.astimezone(config.ET).date()
        self.stale_before = season_start(self.today)
        self.overrides = load_overrides() if overrides is None else overrides
        rows = conn.execute(
            "select player_id, full_name, team_abbr, name_norm, team_as_of from players where sport = %s",
            (sport,)).fetchall()
        self.index = PlayerIndex([Candidate(*r) for r in rows])
        self.xref: dict[tuple[str, str], int] = {
            (s, e): p for s, e, p in conn.execute(
                "select source, external_id, player_id from player_xref where sport = %s", (sport,))}
        self.linked_sources = {(s, p) for (s, _), p in self.xref.items() if s in ONE_PER_PLAYER}
        self.new_links: list[tuple] = []
        self.touched: list[tuple] = []
        self.unmatched: dict[tuple[str, str], tuple] = {}
        self.team_updates: dict[int, str] = {}
        self.resolved: set[tuple[str, str]] = set()      # name-only matches: clear their xref_unmatched row
        self.stats: dict[str, dict[str, int]] = {}

    def _count(self, source, key):
        self.stats.setdefault(source, {"known": 0, "linked": 0, "unmatched": 0})[key] += 1

    def resolve(self, ext: External) -> int | None:
        key = (ext.source, ext.external_id)
        if key in self.xref:
            pid = self.xref[key]
            self.touched.append((ext.name, ext.team_abbr, self.now, self.sport, ext.source, ext.external_id))
            self._count(ext.source, "known")
            self._maybe_update_team(ext, pid)
            return pid

        override = self.overrides.get(key)
        if override:
            pid = self.xref.get(("nba", override))
            if pid is None:
                return self._miss(ext, "override_nba_id_not_in_players", [{"nba_id": override}])
            return self._link(ext, pid, "override")

        if ext.source == "propline":
            nba_id = ext.external_id.removeprefix("nba:")
            pid = self.xref.get(("nba", nba_id)) if ext.external_id.startswith("nba:") else None
            if pid is None:
                return self._miss(ext, "nba_id_not_in_players", [])
            return self._link(ext, pid, "direct_id")

        result = self.index.match(ext.name, ext.team_abbr, stale_before=self.stale_before)
        if result.player_id is None:
            return self._miss(ext, result.reason, result.candidates)
        return self._link(ext, result.player_id, result.method)

    def match_only(self, ext: External) -> int | None:
        """Team + name match for a source with no ids of its own (the injury PDF): no player_xref row
        is created, but a miss still goes to xref_unmatched (external_id = the source's player key)."""
        result = self.index.match(ext.name, ext.team_abbr, stale_before=self.stale_before)
        if result.player_id is None:
            return self._miss(ext, result.reason, result.candidates)
        self._count(ext.source, "linked")
        return result.player_id

    def match_in_teams(self, source: str, external_id: str, name: str, teams: list[str | None]) -> int | None:
        """Name-only record known to belong to one of `teams` (e.g. a PropLine outcome without an id,
        whose event gives home and away). Overrides first; otherwise the normal team+name tiers for
        each team, and only a single distinct hit counts. Misses go to xref_unmatched."""
        override = self.overrides.get((source, external_id))
        if override:
            pid = self.xref.get(("nba", override))
            if pid is None:
                return self._miss(External(source, external_id, name, None),
                                  "override_nba_id_not_in_players", [{"nba_id": override}])
            self._count(source, "linked")
            self.resolved.add((source, external_id))
            return pid
        teams = [t for t in teams if t]
        results = [self.index.match(name, t, stale_before=self.stale_before) for t in teams]
        hits = {r.player_id for r in results if r.player_id is not None}
        if len(hits) == 1:
            self._count(source, "linked")
            self.resolved.add((source, external_id))
            return hits.pop()
        reason = "ambiguous_between_teams" if hits else (results[0].reason if results else "no_team")
        candidates = [c for r in results for c in r.candidates] or [{"player_id": p} for p in sorted(hits)]
        return self._miss(External(source, external_id, name, "/".join(teams) or None), reason, candidates)

    def _link(self, ext: External, pid: int, method: str) -> int | None:
        if ext.source in ONE_PER_PLAYER and (ext.source, pid) in self.linked_sources:
            other = next(e for (s, e), p in self.xref.items() if s == ext.source and p == pid)
            p = self.index.by_id[pid]
            return self._miss(ext, "player_already_linked",
                              [{"player_id": pid, "name": p.full_name, "team": p.team_abbr,
                                f"{ext.source}_id": other}])
        self.xref[(ext.source, ext.external_id)] = pid
        if ext.source in ONE_PER_PLAYER:
            self.linked_sources.add((ext.source, pid))
        self.new_links.append((self.sport, ext.source, ext.external_id, pid, ext.name, ext.team_abbr,
                               method, self.now, self.now))
        self.unmatched.pop((ext.source, ext.external_id), None)
        self._count(ext.source, "linked")
        self._maybe_update_team(ext, pid)
        return pid

    def _maybe_update_team(self, ext: External, pid: int):
        # ESPN rosters are the current-team source of truth (trades), so later Kalshi matches use it.
        if ext.source != "espn" or not ext.team_abbr:
            return
        p = self.index.by_id[pid]
        if p.team_abbr != ext.team_abbr or p.team_as_of != self.today:
            self.index.move(pid, ext.team_abbr, self.today)
            self.team_updates[pid] = ext.team_abbr   # also refreshes team_as_of when ESPN re-confirms

    def _miss(self, ext: External, reason: str, candidates: list) -> None:
        self.unmatched[(ext.source, ext.external_id)] = (
            self.sport, ext.source, ext.external_id, ext.name, ext.team_abbr, reason,
            json.dumps(candidates), self.now, self.now)
        self._count(ext.source, "unmatched")
        return None

    def flush(self, commit: bool = True) -> None:
        """Write batched changes. Jobs pass commit=False so their rows and links commit together."""
        c = self.conn
        with c.transaction():
            if self.team_updates:
                c.cursor().executemany(
                    """update players set team_abbr = %s, team_as_of = %s, updated_at = %s
                       where player_id = %s""",
                    [(t, self.today, self.now, pid) for pid, t in self.team_updates.items()])
            if self.new_links:
                c.cursor().executemany(
                    """insert into player_xref (sport, source, external_id, player_id, external_name,
                                                team_abbr, match_method, first_seen, last_seen)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       on conflict (sport, source, external_id) do nothing""", self.new_links)
                c.cursor().executemany(
                    """update xref_unmatched set resolved_at = %s
                       where sport = %s and source = %s and external_id = %s and resolved_at is null""",
                    [(self.now, s, src, e) for s, src, e, *_ in self.new_links])
            # An override also closes an open row whose source no longer sends that id (e.g. a book
            # we stopped storing), as long as the override's NBA.com id is a known player.
            for src, ext_id in c.execute("""select source, external_id from xref_unmatched
                                            where sport = %s and resolved_at is null""", (self.sport,)).fetchall():
                nba = self.overrides.get((src, ext_id))
                if nba and ("nba", nba) in self.xref:
                    self.resolved.add((src, ext_id))
            resolved = self.resolved - set(self.unmatched)
            if resolved:
                c.cursor().executemany(
                    """update xref_unmatched set resolved_at = %s
                       where sport = %s and source = %s and external_id = %s and resolved_at is null""",
                    [(self.now, self.sport, src, e) for src, e in sorted(resolved)])
            if self.touched:
                c.cursor().executemany(
                    """update player_xref set external_name = %s, team_abbr = coalesce(%s, team_abbr),
                              last_seen = %s
                       where sport = %s and source = %s and external_id = %s""", self.touched)
            if self.unmatched:
                c.cursor().executemany(
                    """insert into xref_unmatched (sport, source, external_id, external_name, team_abbr,
                                                   reason, candidates, first_seen, last_seen)
                       values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                       on conflict (sport, source, external_id) do update set
                           external_name = excluded.external_name, team_abbr = excluded.team_abbr,
                           reason = excluded.reason, candidates = excluded.candidates,
                           last_seen = excluded.last_seen, seen_count = xref_unmatched.seen_count + 1,
                           resolved_at = null""", list(self.unmatched.values()))
        if commit:
            c.commit()
        self.new_links, self.touched, self.unmatched, self.team_updates = [], [], {}, {}
        self.resolved = set()


# ── seed (Mac only: reads nba_edge.db) ───────────────────────────────────────

def read_sqlite_players(path: str) -> list[tuple[str, str, str | None, date | None, bool]]:
    """[(nba_id, full_name, team of latest game log, date of that game, is_active)]."""
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = src.execute("""
            select p.player_id, p.full_name, latest.team_abbr, latest.game_date, coalesce(p.is_active, 1)
            from players p
            left join (
                select player_id, team_abbr, game_date,
                       row_number() over (partition by player_id order by game_date desc, id desc) as rn
                from game_logs
            ) latest on latest.player_id = p.player_id and latest.rn = 1
            order by p.player_id""").fetchall()
    finally:
        src.close()
    return [(str(pid), name, team, date.fromisoformat(gd) if gd else None, bool(active))
            for pid, name, team, gd, active in rows]


def read_manual_players(path: Path = MANUAL_PLAYERS, exclude: set[str] = frozenset()) -> list[tuple]:
    """Players not in nba_edge.db yet, from worker/manual_players.csv. Same tuple shape as the sqlite reader."""
    if not path.exists():
        return []
    with path.open() as f:
        rows = [r for r in csv.DictReader(f) if r.get("nba_id") and not r["nba_id"].startswith("#")]
    return [(r["nba_id"].strip(), r["full_name"].strip(), (r.get("team_abbr") or "").strip() or None, None, True)
            for r in rows if r["nba_id"].strip() not in exclude]


def seed(conn, players: list[tuple], sport: str = config.SPORT) -> dict:
    """Upsert players + their 'nba' xref rows. Idempotent: re-running updates names and teams.

    A re-seed never moves a player back to an older team than the one already stored
    (an ESPN roster may have set a newer one).
    """
    now = utcnow()
    with conn.transaction():
        conn.execute("drop table if exists pg_temp.stage_players")
        conn.execute("""create temp table stage_players (
                            nba_id text primary key, full_name text, name_norm text,
                            team_abbr text, team_as_of date, is_active boolean)""")
        with conn.cursor().copy("copy stage_players from stdin") as cp:
            for nba_id, name, team, as_of, active in players:
                cp.write_row((nba_id, name, normalize_name(name), team, as_of, active))
        bad_teams = conn.execute(
            """select distinct s.team_abbr from stage_players s
               left join teams t on t.sport = %s and t.abbr = s.team_abbr
               where s.team_abbr is not null and t.abbr is null""", (sport,)).fetchall()
        if bad_teams:
            raise SystemExit(f"unknown team abbreviations in nba_edge.db: {[r[0] for r in bad_teams]}")
        updated = conn.execute(
            """update players p set full_name = s.full_name, name_norm = s.name_norm, is_active = s.is_active,
                      team_abbr = case when p.team_as_of is null or s.team_as_of >= p.team_as_of
                                       then coalesce(s.team_abbr, p.team_abbr) else p.team_abbr end,
                      team_as_of = greatest(p.team_as_of, s.team_as_of),
                      updated_at = %s
               from stage_players s
               join player_xref x on x.sport = %s and x.source = 'nba' and x.external_id = s.nba_id
               where p.player_id = x.player_id""", (now, sport)).rowcount
        inserted = conn.execute(
            """with new as materialized (
                   select nextval(pg_get_serial_sequence('players', 'player_id')) as pid, s.*
                   from stage_players s
                   left join player_xref x on x.sport = %(sport)s and x.source = 'nba' and x.external_id = s.nba_id
                   where x.player_id is null),
               ins as (
                   insert into players (player_id, sport, full_name, name_norm, team_abbr, team_as_of,
                                        is_active, created_at, updated_at)
                   select pid, %(sport)s, full_name, name_norm, team_abbr, team_as_of, is_active,
                          %(now)s, %(now)s from new
                   returning player_id)
               insert into player_xref (sport, source, external_id, player_id, external_name, team_abbr,
                                        match_method, first_seen, last_seen)
               select %(sport)s, 'nba', nba_id, pid, full_name, team_abbr, 'seed', %(now)s, %(now)s from new""",
            {"sport": sport, "now": now}).rowcount
        conn.execute("drop table pg_temp.stage_players")
    conn.commit()
    total = conn.execute("select count(*) from players where sport = %s", (sport,)).fetchone()[0]
    return {"source_rows": len(players), "inserted": inserted, "updated": updated, "players_total": total}


# ── link (anywhere: ESPN, Kalshi, PropLine) ──────────────────────────────────

def fetch_espn(session, espn_to_abbr: dict[str, str], espn_team_ids: list[str]) -> list[External]:
    out = []
    for tid in espn_team_ids:
        r = session.get(f"{config.ESPN_BASE}/teams/{tid}/roster")
        r.raise_for_status()
        out += parse_espn_roster(r.json(), espn_to_abbr)
    return out


def fetch_kalshi(session) -> list[External]:
    markets = []
    for series in config.KALSHI_SERIES:
        cursor = None
        while True:
            params = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = session.get(f"{config.KALSHI_API}/markets", params=params)
            r.raise_for_status()
            body = r.json()
            markets += body.get("markets") or []
            cursor = body.get("cursor")
            if not cursor:
                break
    return parse_kalshi_props(markets)


def fetch_propline(session, api_key: str) -> list[External]:
    r = session.get(f"{config.PROPLINE_API}/sports/{config.PROPLINE_SPORT}/odds",
                    params={"markets": "player_points"}, headers={"X-API-Key": api_key})
    r.raise_for_status()
    return parse_propline_players(r.json())


def link(conn, session=None, skip: set[str] = frozenset(), propline_key: str | None = None,
         now: datetime | None = None) -> dict:
    session = session or make_session()
    propline_key = config.PROPLINE_API_KEY if propline_key is None else propline_key
    teams = conn.execute("select espn_abbr, abbr, espn_team_id from teams where sport = %s",
                         (config.SPORT,)).fetchall()
    espn_to_abbr = {e: a for e, a, _ in teams}
    linker = Linker(conn, now=now)
    fetched: dict[str, int] = {}
    # ESPN first: it refreshes current teams, which the Kalshi team+name match depends on.
    if "espn" not in skip:
        items = fetch_espn(session, espn_to_abbr, [t for _, _, t in teams])
        fetched["espn"] = len(items)
        for ext in items:
            linker.resolve(ext)
    if "propline" not in skip:
        if propline_key:
            items = fetch_propline(session, propline_key)
            fetched["propline"] = len(items)
            for ext in items:
                linker.resolve(ext)
        else:
            fetched["propline"] = -1
    if "kalshi" not in skip:
        items = fetch_kalshi(session)
        fetched["kalshi"] = len(items)
        for ext in items:
            linker.resolve(ext)
    stats = linker.stats
    linker.flush()
    return {"fetched": fetched, "stats": stats}


# ── report ───────────────────────────────────────────────────────────────────

def unmatched_rows(conn, sport: str = config.SPORT):
    return conn.execute(
        """select source, external_id, external_name, team_abbr, reason, candidates, seen_count,
                  first_seen, last_seen
           from xref_unmatched where sport = %s and resolved_at is null
           order by source, team_abbr nulls last, external_name""", (sport,)).fetchall()


def coverage(conn, sport: str = config.SPORT):
    return conn.execute(
        """select count(*) filter (where is_active),
                  count(*) filter (where is_active and espn_id is not null),
                  count(*) filter (where is_active and propline_id is not null),
                  count(*) filter (where is_active and cardinality(kalshi_fragments) > 0)
           from player_xref_wide where sport = %s""", (sport,)).fetchone()


def methods(conn, sport: str = config.SPORT):
    return conn.execute(
        """select source, match_method, count(*) from player_xref where sport = %s
           group by 1, 2 order by 1, 2""", (sport,)).fetchall()


def render_report(rows, cov, method_counts=()) -> str:
    active, espn, propline, kalshi = cov
    lines = [f"active players: {active}  with ESPN id: {espn}  with PropLine id: {propline}  "
             f"with Kalshi fragment: {kalshi}"]
    if method_counts:
        lines.append("links by method: " + ", ".join(f"{s}/{m}={n}" for s, m, n in method_counts))
    lines.append(f"UNMATCHED (unresolved): {len(rows)}")
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r[0]] = by_source.get(r[0], 0) + 1
    if by_source:
        lines.append("  by source: " + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))
    for source, ext_id, name, team, reason, cands, seen, *_ in rows:
        cand = "; ".join(f"{c.get('name')} ({c.get('team')} as of {c.get('team_as_of')}, id {c.get('player_id')})"
                         for c in (cands or []) if c.get("name"))
        lines.append(f"  {source:8} {ext_id:24} {name or '':28} {team or '-':4} {reason:30} "
                     f"seen {seen}x" + (f"  candidates: {cand}" if cand else ""))
    if rows:
        lines.append("Fix: add 'source,external_id,nba_id,note' lines to worker/xref_overrides.csv, "
                     "then run `python -m prop_worker.xref link`.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="(Mac) seed players + NBA.com ids from nba_edge.db")
    s.add_argument("--sqlite", default=str(Path(__file__).resolve().parents[2] / "data" / "nba_edge.db"))
    l = sub.add_parser("link", help="link ESPN / PropLine / Kalshi ids")
    l.add_argument("--skip", default="", help="comma list of sources to skip")
    r = sub.add_parser("report", help="print the unmatched report")
    r.add_argument("--csv", help="also write the unmatched rows to this CSV")
    r.add_argument("--fail-on-unmatched", action="store_true")
    args = ap.parse_args(argv)

    with connect() as conn:
        if args.cmd == "seed":
            if not Path(args.sqlite).exists():
                raise SystemExit(f"{args.sqlite} not found (seed runs on the Mac)")
            players = read_sqlite_players(args.sqlite)
            manual = read_manual_players(exclude={p[0] for p in players})
            out = seed(conn, players + manual)
            out["from_nba_edge_db"], out["from_manual_players_csv"] = len(players), len(manual)
            print(json.dumps(out, indent=2))
            return 0
        if args.cmd == "link":
            if not conn.execute("select count(*) from players").fetchone()[0]:
                raise SystemExit("players is empty: run `python -m prop_worker.xref seed` on the Mac first")
            out = link(conn, skip={x for x in args.skip.split(",") if x})
            if out["fetched"].get("propline") == -1:
                print("propline: skipped (PROPLINE_API_KEY not set)")
            for src, st in out["stats"].items():
                print(f"{src:8} fetched {out['fetched'][src]:5}  already linked {st['known']:5}  "
                      f"newly linked {st['linked']:5}  unmatched {st['unmatched']:4}")
        rows = unmatched_rows(conn)
        print(render_report(rows, coverage(conn), methods(conn)))
        if getattr(args, "csv", None):
            with open(args.csv, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["source", "external_id", "external_name", "team_abbr", "reason",
                            "candidates", "seen_count", "first_seen", "last_seen"])
                for row in rows:
                    w.writerow([*row[:5], json.dumps(row[5]), *row[6:]])
            print(f"wrote {len(rows)} rows to {args.csv}")
        if getattr(args, "fail_on_unmatched", False) and rows:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
