"""Phase 1 check: can this machine reach every source the worker depends on?

Run:  python -m prop_worker.healthcheck [--json out.json] [--require-db]

Exit code 1 if any *required* probe fails. Results also go to $GITHUB_STEP_SUMMARY in Actions.
cdn.nba.com is informational only: it returns 403 to plain requests from the dev Mac, and we
want to know whether cloud IPs fare any differently. We never try to get around the block.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

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


# ── runner ───────────────────────────────────────────────────────────────────

def run(session=None, now_et: datetime | None = None, db_url: str | None = None,
        require_db: bool = False) -> list[Probe]:
    session = session or make_session()
    now_et = now_et or datetime.now(config.ET)
    db_url = config.SUPABASE_DB_URL if db_url is None else db_url
    return [
        _timed("postgres", True, probe_postgres, db_url, require_db),
        _timed("espn_scoreboard", True, probe_espn_scoreboard, session, now_et),
        _timed("espn_boxscore", True, probe_espn_boxscore, session, now_et),
        _timed("espn_injuries", True, probe_espn_injuries, session),
        _timed("nba_injury_pdf", True, probe_injury_pdf, session, now_et),
        _timed("kalshi", True, probe_kalshi, session),
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
