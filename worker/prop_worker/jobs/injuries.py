"""injuries: official NBA injury report PDF (primary) + ESPN injuries (secondary) -> injuries, injury_flags.

Run:  python -m prop_worker.jobs.injuries [--no-alert]

Both sources are stored (change-only: a row is added only when a player's status or reason
changes; otherwise last_seen_at moves). Rows are keyed '<TEAM>:<normalized name>' so the two
sources line up. Players resolve through player_xref; unresolved players are still stored with
player_id null and listed in xref_unmatched.

Disagreements for tonight's teams go to injury_flags (one open row per player; resolved when the
sources agree again):
  status_mismatch   official and ESPN statuses aren't compatible (ESPN 'Day-To-Day' is compatible
                    with Questionable / Doubtful / Probable)
  missing_in_espn   official Out/Doubtful (not a G League assignment) that ESPN doesn't list
  missing_in_nba    ESPN Out for a team whose official report is filed, not on the official report

Red: no official report in the last 2 h, the PDF doesn't parse, or the official report has rows
but none could be stored. An ESPN failure is red too, but the official rows are still stored.
"""

from __future__ import annotations

import re
from datetime import timedelta, timezone

from .. import config
from ..healthcheck import injury_pdf_url
from ..injury_pdf import parse_report
from ..names import normalize_name
from ..runner import Context, JobResult, _squash
from ..util import is_future, parse_ts
from ..xref import External, Linker
from . import main_for

ESPN_ID = re.compile(r"/id/(\d+)")
DTD_COMPATIBLE = {"Questionable", "Doubtful", "Probable"}


def player_key(team: str, name: str) -> str:
    return f"{team}:{normalize_name(name)}"


def find_latest_pdf(session, now_et):
    slot = now_et.replace(minute=now_et.minute - now_et.minute % 15, second=0, microsecond=0)
    for _ in range(config.INJURY_PDF_MAX_AGE_SLOTS + 1):
        url = injury_pdf_url(slot)
        if session.head(url).status_code == 200:
            return url, slot
        slot -= timedelta(minutes=15)
    return None, None


def parse_espn_injuries(payload: dict, by_espn_id: dict[str, str]) -> tuple[list[dict], list[str]]:
    rows, skipped = [], []
    for team_block in payload.get("injuries") or []:
        team = by_espn_id.get(str(team_block.get("id")))
        if not team:
            skipped.append(f"ESPN team {team_block.get('id')} {team_block.get('displayName')}")
            continue
        for i in team_block.get("injuries") or []:
            a = i.get("athlete") or {}
            href = next((l.get("href", "") for l in a.get("links") or [] if "playercard" in (l.get("rel") or [])), "")
            m = ESPN_ID.search(href)
            details = i.get("details") or {}
            reason = " ".join(x for x in (details.get("side"), details.get("type")) if x and x != "Not Specified")
            rows.append({"espn_athlete_id": m.group(1) if m else None, "name": a.get("displayName"),
                         "team_abbr": team, "status": i.get("status"),
                         "reason": reason or i.get("shortComment"), "provider_ts": parse_ts(i.get("date"))})
    return rows, skipped


def compatible(nba_status: str, espn_status: str) -> bool:
    if nba_status == espn_status:
        return True
    return espn_status == "Day-To-Day" and nba_status in DTD_COMPATIBLE


def compare(nba_rows: dict[str, dict], espn_rows: dict[str, dict], filed_teams: set[str]) -> list[dict]:
    """nba_rows / espn_rows: {player_key: row}. Returns flag dicts (kind, statuses)."""
    flags = []
    for key, n in nba_rows.items():
        e = espn_rows.get(key)
        if e is None:
            if n["status"] in ("Out", "Doubtful") and "G League" not in (n.get("reason") or ""):
                flags.append({"player_key": key, "kind": "missing_in_espn", "nba": n, "espn": None})
        elif not compatible(n["status"], e["status"]):
            flags.append({"player_key": key, "kind": "status_mismatch", "nba": n, "espn": e})
    for key, e in espn_rows.items():
        if key not in nba_rows and e["status"] == "Out" and key.split(":", 1)[0] in filed_teams:
            flags.append({"player_key": key, "kind": "missing_in_nba", "nba": None, "espn": e})
    return flags


_INSERT = """
insert into injuries (sport, source, player_key, player_id, external_name, team_abbr, game_date, status,
                      reason, source_url, provider_ts, fetched_at)
values (%(sport)s, %(source)s, %(player_key)s, %(player_id)s, %(external_name)s, %(team_abbr)s, %(game_date)s,
        %(status)s, %(reason)s, %(source_url)s, %(provider_ts)s, %(fetched_at)s)"""


def store(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    cur = conn.cursor()
    cur.executemany(_INSERT, rows)
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def write_flags(conn, flags: list[dict], slate, now) -> tuple[int, int]:
    latest = {(s, k): i for i, s, k in conn.execute(
        """select distinct on (source, player_key) id, source, player_key from injuries
           where sport = %s and ((source = 'nba_pdf' and game_date = %s) or source = 'espn')
           order by source, player_key, fetched_at desc, id desc""", (config.SPORT, slate))}
    keys = []
    for f in flags:
        n, e = f["nba"], f["espn"]
        pid = (n or {}).get("player_id") or (e or {}).get("player_id")
        conn.execute(
            """insert into injury_flags (sport, game_date, player_key, player_id, nba_status, espn_status,
                                         nba_injury_id, espn_injury_id, kind, detected_at, last_seen_at)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (sport, game_date, player_key) where resolved_at is null do update set
                   player_id = coalesce(excluded.player_id, injury_flags.player_id),
                   nba_status = excluded.nba_status, espn_status = excluded.espn_status,
                   nba_injury_id = excluded.nba_injury_id, espn_injury_id = excluded.espn_injury_id,
                   kind = excluded.kind, last_seen_at = excluded.last_seen_at""",
            (config.SPORT, slate, f["player_key"], pid, (n or {}).get("status"), (e or {}).get("status"),
             latest.get(("nba_pdf", f["player_key"])), latest.get(("espn", f["player_key"])), f["kind"], now, now))
        keys.append(f["player_key"])
    resolved = conn.execute(
        """update injury_flags set resolved_at = %s
           where sport = %s and game_date = %s and resolved_at is null and not (player_key = any(%s))""",
        (now, config.SPORT, slate, keys)).rowcount
    return len(flags), resolved


def run(ctx: Context) -> JobResult:
    linker = Linker(ctx.conn, now=ctx.now)
    teams = ctx.teams
    red, warnings, meta = [], [], {}
    nba_rows: list[dict] = []
    espn_rows: list[dict] = []
    filed_teams_tonight: set[str] = set()
    pdf_ok = False
    tonight = ctx.conn.execute(
        "select home_team, away_team from games where sport = %s and game_date = %s",
        (config.SPORT, ctx.slate)).fetchall()
    playing = {t for g in tonight for t in g}

    # ── primary: official PDF ───────────────────────────────────────────────
    url, slot = find_latest_pdf(ctx.session, ctx.now_et)
    if url is None:
        red.append(f"no official injury report in the last {config.INJURY_PDF_MAX_AGE_SLOTS * 15} min")
    else:
        r = ctx.session.get(url)
        r.raise_for_status()
        report = parse_report(r.content)
        meta.update(pdf_url=url, pdf_rows=len(report.rows), not_submitted=len(report.not_submitted))
        if report.problems:
            red.append("official PDF parse problems: " + "; ".join(report.problems[:5]))
        report_ts = (report.report_ts or slot).astimezone(timezone.utc)
        if is_future(report_ts, ctx.now):
            warnings.append(f"official report time {report_ts.isoformat()} is in the future")
        not_filed = {(d, teams["by_name"].get(_squash(t))) for d, t in report.not_submitted}
        for row in report.rows:
            team = teams["by_name"].get(_squash(row.team_name))
            if team is None:
                red.append(f"official PDF team not recognised: {row.team_name!r}")
                continue
            key = player_key(team, row.player_name)
            pid = linker.match_only(External("nba_pdf", key, row.player_name, team))
            nba_rows.append({"sport": config.SPORT, "source": "nba_pdf", "player_key": key, "player_id": pid,
                             "external_name": row.player_name, "team_abbr": team, "game_date": row.game_date,
                             "status": row.status, "reason": row.reason, "source_url": url,
                             "provider_ts": report_ts, "fetched_at": ctx.now})
        filed_teams_tonight = {t for t in playing if (ctx.slate, t) not in not_filed}
        pdf_ok = True

    # ── secondary: ESPN ─────────────────────────────────────────────────────
    try:
        r = ctx.session.get(f"{config.ESPN_BASE}/injuries")
        r.raise_for_status()
        parsed, skipped = parse_espn_injuries(r.json(), teams["by_espn_id"])
        warnings += [f"ESPN injuries skipped {s}" for s in skipped]
        for row in parsed:
            key = player_key(row["team_abbr"], row["name"] or "")
            ext = External("espn", row["espn_athlete_id"], row["name"], row["team_abbr"]) \
                if row["espn_athlete_id"] else External("espn_injuries", key, row["name"], row["team_abbr"])
            pid = linker.resolve(ext) if row["espn_athlete_id"] else linker.match_only(ext)
            espn_rows.append({"sport": config.SPORT, "source": "espn", "player_key": key, "player_id": pid,
                              "external_name": row["name"], "team_abbr": row["team_abbr"], "game_date": None,
                              "status": row["status"], "reason": row["reason"],
                              "source_url": f"{config.ESPN_BASE}/injuries",
                              "provider_ts": row["provider_ts"], "fetched_at": ctx.now})
    except Exception as exc:  # noqa: BLE001 — keep the official rows even if ESPN is down
        red.append(f"ESPN injuries (secondary) failed: {type(exc).__name__}: {exc}")
        if not pdf_ok:
            raise

    new_nba = store(ctx.conn, nba_rows)
    new_espn = store(ctx.conn, espn_rows)
    linker.flush(commit=False)

    flagged = resolved = 0
    if pdf_ok and espn_rows:
        tonight_nba = {r["player_key"]: r for r in nba_rows if r["game_date"] == ctx.slate}
        espn_tonight = {r["player_key"]: r for r in espn_rows if r["team_abbr"] in playing}
        flags = compare(tonight_nba, espn_tonight, filed_teams_tonight)
        flagged, resolved = write_flags(ctx.conn, flags, ctx.slate, ctx.now)

    if pdf_ok and report.rows and not nba_rows:
        red.append("official report had rows but none could be stored")
    unresolved = sum(r["player_id"] is None for r in nba_rows + espn_rows)
    if unresolved:
        warnings.append(f"{unresolved} injury row(s) stored without a player_id (see xref report)")
    meta.update(espn_rows=len(espn_rows), new_nba=new_nba, new_espn=new_espn, flags_open=flagged,
                flags_resolved=resolved, unresolved_players=unresolved)
    return JobResult(rows_in=len(nba_rows) + len(espn_rows), rows_out=len(nba_rows) + len(espn_rows),
                     expected=True, units=ctx.games_on(ctx.slate) or None, red_reasons=red,
                     warnings=warnings, meta=meta)


main_for(__name__, "injuries", run, __doc__)
