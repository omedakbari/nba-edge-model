"""Source healthcheck: can this machine reach every source the worker depends on?

Run:  python -m prop_worker.healthcheck [--json out.json] [--require-db]

Exit code 1 if any *required* probe fails. Results also go to $GITHUB_STEP_SUMMARY in Actions.
Informational probes (never fail the run):
  - cdn.nba.com: 403 from the dev Mac and from GitHub runners (run 42912209921). Kept only to
    notice if that ever changes; the worker uses ESPN instead and never tries to get around the block.
  - PropLine (two probes): does our tier return NBA player props, and free box-score stats?
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from . import config
from .http import make_session

PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"


@dataclass
class Probe:
    name: str
    status: str
    required: bool
    latency_ms: int
    detail: str


def _timed(name, required, fn, *args):
    t0 = time.monotonic()
    try:
        status, detail = fn(*args)
    except Exception as exc:  # any failure is a FAIL with the reason, never a crash
        status, detail = FAIL, f"{type(exc).__name__}: {exc}"[:300]
    if not required and status == FAIL:
        status = INFO
    return Probe(name, status, required, int((time.monotonic() - t0) * 1000), detail)


# ── individual probes ────────────────────────────────────────────────────────

def probe_postgres(db_url: str, require: bool):
    if not db_url:
        return (FAIL if require else SKIP), "SUPABASE_DB_URL not set"
    import psycopg

    with psycopg.connect(db_url, connect_timeout=10) as conn:
        version = conn.execute("select version()").fetchone()[0]
    return PASS, version.split(",")[0]


def probe_espn_scoreboard(session, now_et: datetime):
    d = now_et.strftime("%Y%m%d")
    r = session.get(f"{config.ESPN_BASE}/scoreboard", params={"dates": d})
    r.raise_for_status()
    events = r.json().get("events")
    if not isinstance(events, list):
        return FAIL, "no events array"
    return PASS, f"{len(events)} games on {d}"


def probe_espn_boxscore(session, now_et: datetime, lookback_days: int = 7):
    for back in range(1, lookback_days + 1):
        d = (now_et - timedelta(days=back)).strftime("%Y%m%d")
        r = session.get(f"{config.ESPN_BASE}/scoreboard", params={"dates": d})
        r.raise_for_status()
        finals = [e for e in r.json().get("events", [])
                  if e.get("status", {}).get("type", {}).get("completed")]
        if not finals:
            continue
        event_id = finals[0]["id"]
        s = session.get(f"{config.ESPN_BASE}/summary", params={"event": event_id})
        s.raise_for_status()
        teams = s.json().get("boxscore", {}).get("players", [])
        if len(teams) != 2:
            return FAIL, f"event {event_id}: expected 2 teams in boxscore, got {len(teams)}"
        stats = teams[0]["statistics"][0]
        missing = {"MIN", "PTS", "REB", "AST", "3PT"} - set(stats.get("labels", []))
        if missing:
            return FAIL, f"event {event_id}: box score missing {sorted(missing)}"
        n = sum(len(t["statistics"][0].get("athletes", [])) for t in teams)
        return PASS, f"event {event_id} ({d}): {n} player rows"
    return FAIL, f"no completed games in the last {lookback_days} days"


def probe_espn_injuries(session):
    r = session.get(f"{config.ESPN_BASE}/injuries")
    r.raise_for_status()
    teams = r.json().get("injuries")
    if not isinstance(teams, list):
        return FAIL, "no injuries array"
    n = sum(len(t.get("injuries", [])) for t in teams)
    return PASS, f"{len(teams)} teams, {n} listed players"


def injury_pdf_url(slot: datetime) -> str:
    hh12 = slot.hour % 12 or 12
    return config.NBA_INJURY_PDF.format(date=slot.strftime("%Y-%m-%d"), hh=f"{hh12:02d}",
                                        mm=f"{slot.minute:02d}", ampm="AM" if slot.hour < 12 else "PM")


def recent_slots(now_et: datetime, hours: int = 36):
    slot = now_et.replace(minute=now_et.minute - now_et.minute % 15, second=0, microsecond=0)
    for _ in range(hours * 4):
        yield slot
        slot -= timedelta(minutes=15)


def probe_injury_pdf(session, now_et: datetime):
    # The bucket answers 403 (not 404) for slots that don't exist yet, so walk back to the newest one.
    for slot in recent_slots(now_et):
        url = injury_pdf_url(slot)
        h = session.head(url)
        if h.status_code != 200:
            continue
        r = session.get(url)
        r.raise_for_status()
        if not r.content.startswith(b"%PDF"):
            return FAIL, f"{url} is not a PDF"
        header = _pdf_first_line(r.content)
        if not header.startswith("Injury Report:"):
            return FAIL, f"unexpected header {header!r}"
        age_min = int((now_et - slot).total_seconds() // 60)
        return PASS, f"{header} ({age_min} min old, {len(r.content)//1024} KB)"
    return FAIL, "no injury report PDF in the last 36h (403 for every slot: missing or blocked)"


def _pdf_first_line(content: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        text = pdf.pages[0].extract_text() or ""
    return text.splitlines()[0].strip() if text else ""


def probe_kalshi(session):
    counts = {}
    for series in config.KALSHI_SERIES:
        r = session.get(f"{config.KALSHI_API}/markets",
                        params={"series_ticker": series, "status": "open", "limit": 1000})
        r.raise_for_status()
        markets = r.json().get("markets")
        if not isinstance(markets, list):
            return FAIL, f"{series}: no markets array"
        counts[series] = len(markets)
    return PASS, ", ".join(f"{k}={v}" for k, v in counts.items()) + " open markets"


def probe_cdn_nba(session):
    r = session.get(config.CDN_NBA_SCOREBOARD)
    if r.status_code != 200:
        return FAIL, f"HTTP {r.status_code} (blocked for this IP; worker uses ESPN instead)"
    games = r.json().get("scoreboard", {}).get("games", [])
    return PASS, f"reachable, {len(games)} games today"


# ── PropLine (informational) ─────────────────────────────────────────────────

_TIER_CODES = {401: "key rejected", 402: "not on this tier", 403: "not on this tier"}


def _propline_get(session, api_key: str, path: str, params=None):
    """GET a PropLine path. Returns (response, error string or None). Never puts the key in a message."""
    r = session.get(f"{config.PROPLINE_API}/{path}", params=params or {},
                    headers={"X-API-Key": api_key})
    if r.status_code in _TIER_CODES:
        return r, f"/{path.split('/')[-1]}: HTTP {r.status_code} ({_TIER_CODES[r.status_code]})"
    r.raise_for_status()
    return r, None


def _quota(r) -> str:
    h = getattr(r, "headers", {}) or {}
    rem, lim = h.get("x-daily-remaining"), h.get("x-daily-limit")
    return f"; quota {rem}/{lim} left today" if rem and lim else ""


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def count_prop_markets(odds: dict) -> tuple[dict[str, int], int]:
    """Player-prop markets per market key, and how many books carried at least one."""
    counts: dict[str, int] = {}
    books = 0
    for b in odds.get("bookmakers") or []:
        n0 = sum(counts.values())
        for m in b.get("markets") or []:
            if m.get("key") in config.PROPLINE_MARKETS:
                counts[m["key"]] = counts.get(m["key"], 0) + 1
        books += sum(counts.values()) > n0
    return counts, books


def probe_propline_props(session, api_key: str, now_et: datetime, max_events: int = 3):
    if not api_key:
        return SKIP, "PROPLINE_API_KEY not set"
    sport = config.PROPLINE_SPORT
    r, err = _propline_get(session, api_key, f"sports/{sport}/events")
    if err:
        return FAIL, err
    now_utc = now_et.astimezone(timezone.utc)
    upcoming = sorted((e for e in r.json() if _parse_ts(e["commence_time"]) > now_utc),
                      key=lambda e: e["commence_time"])
    if not upcoming:
        return FAIL, "no upcoming NBA events listed"
    tried = []
    for ev in upcoming[:max_events]:
        r, err = _propline_get(session, api_key, f"sports/{sport}/events/{ev['id']}/odds",
                               {"markets": ",".join(config.PROPLINE_MARKETS)})
        if err:
            return FAIL, f"player props NOT on our tier: {err}"
        counts, books = count_prop_markets(r.json())
        label = f"{ev['away_team']} @ {ev['home_team']}"
        if counts:
            missing = sorted(set(config.PROPLINE_MARKETS) - set(counts))
            per = ", ".join(f"{config.PROPLINE_MARKETS[k]}={v}" for k, v in sorted(counts.items()))
            gap = f"; missing {missing}" if missing else ""
            return PASS, f"player props on our tier: {label}: {books} books, {per}{gap}{_quota(r)}"
        tried.append(label)
    return FAIL, f"0 player-prop markets for the next {len(tried)} events ({'; '.join(tried)})"


def probe_propline_boxscore(session, api_key: str):
    if not api_key:
        return SKIP, "PROPLINE_API_KEY not set"
    sport = config.PROPLINE_SPORT
    r, err = _propline_get(session, api_key, f"sports/{sport}/scores", {"days_from": 3})
    if err:
        return FAIL, err
    finals = [e for e in r.json() if e.get("status") == "final"]
    if not finals:
        return FAIL, "no final NBA games in the last 3 days"
    ev = finals[0]
    r, err = _propline_get(session, api_key, f"sports/{sport}/events/{ev['id']}/stats")
    if err:
        return FAIL, f"box-score stats NOT on our tier: {err}"
    rows = [s for s in r.json().get("stats") or [] if s.get("player_name")]
    players = {s["player_name"] for s in rows}
    types = {s["stat_type"] for s in rows}
    missing = sorted({"minutes", "points", "rebounds", "assists", "threes"} - types)
    if not players or missing:
        return FAIL, f"event {ev['id']}: {len(players)} players, missing stats {missing}"
    return PASS, (f"free box-score stats: event {ev['id']} ({ev['away_team']} @ {ev['home_team']}): "
                  f"{len(players)} players, {len(types)} stat types{_quota(r)}")


# ── runner ───────────────────────────────────────────────────────────────────

def run(session=None, now_et: datetime | None = None, db_url: str | None = None,
        require_db: bool = False, propline_key: str | None = None) -> list[Probe]:
    session = session or make_session()
    now_et = now_et or datetime.now(config.ET)
    db_url = config.SUPABASE_DB_URL if db_url is None else db_url
    propline_key = config.PROPLINE_API_KEY if propline_key is None else propline_key
    return [
        _timed("postgres", True, probe_postgres, db_url, require_db),
        _timed("espn_scoreboard", True, probe_espn_scoreboard, session, now_et),
        _timed("espn_boxscore", True, probe_espn_boxscore, session, now_et),
        _timed("espn_injuries", True, probe_espn_injuries, session),
        _timed("nba_injury_pdf", True, probe_injury_pdf, session, now_et),
        _timed("kalshi", True, probe_kalshi, session),
        _timed("propline_props (info)", False, probe_propline_props, session, propline_key, now_et),
        _timed("propline_boxscore (info)", False, probe_propline_boxscore, session, propline_key),
        _timed("cdn_nba (info)", False, probe_cdn_nba, session),
    ]


def overall_ok(results: list[Probe]) -> bool:
    return not any(p.required and p.status == FAIL for p in results)


def render(results: list[Probe]) -> str:
    lines = ["| probe | status | ms | detail |", "|---|---|---|---|"]
    lines += [f"| {p.name} | {p.status} | {p.latency_ms} | {p.detail.replace('|', '/')} |" for p in results]
    lines.append("")
    lines.append(f"**Overall: {'PASS' if overall_ok(results) else 'FAIL'}**")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", help="also write results to this path")
    ap.add_argument("--require-db", action="store_true",
                    help="fail if SUPABASE_DB_URL is missing (default in CI)")
    args = ap.parse_args(argv)

    results = run(require_db=args.require_db or config.in_ci())
    table = render(results)
    print(table)
    if args.json:
        with open(args.json, "w") as f:
            json.dump([asdict(p) for p in results], f, indent=2)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("## Source healthcheck\n\n" + table + "\n")
    return 0 if overall_ok(results) else 1


if __name__ == "__main__":
    sys.exit(main())
