"""Today page: one-click daily refresh and tonight's board.

The Refresh button launches scripts/run_daily.py as a detached process that
writes to data/runs/<stamp>.log. The page tails that log, so a run keeps going
(and the page re-attaches) across browser reloads, and two tabs can't start
two runs. Everything shown is read from files the pipeline already writes:

  team board   data/team_picks/<date>.json        (newest revision, every game)
  prop card    data/cards/<date>.json             (research-only shortlist)
  market +EV   data/market_picks/<date>.json      (newest revision)
  yesterday    data/team_picks/results_log.csv
  brief        data/cards/<date>_brief.txt
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.utils.slate import latest_revision, slate_today  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
RUNS_DIR = ROOT / "data" / "runs"
CURRENT = RUNS_DIR / "current.json"
EXIT_MARK = "__RUN_EXIT__"

# Mirrors scripts/generate_todays_team_picks.py (not imported: that module pulls in the model stack).
MIN_SPREAD_EDGE, STRONG_SPREAD_EDGE = 3.5, 5.5
MIN_OU_EDGE, STRONG_OU_EDGE = 4.0, 7.0
PICK_TIERS = ("STRONG", "LEAN")
TIER_RANK = {"STRONG": 0, "LEAN": 1, "MONITOR": 2, "NONE": 3, "PASS": 3}

# Section markers printed by run_daily.py / nba.py daily, in order.
STAGES = [
    ("Live evidence:", "Archiving live data"),
    ("Market props — ", "Grading props (catch-up)"),          # '— yesterday' in logs before 2026-11-25
    ("Team picks — ", "Grading team picks (catch-up)"),
    ("Player props — market prices and EV", "Pricing player props"),
    ("Team spread / ML picks", "Building team board"),
    ("Player + game prop stack screen", "Screening stacks"),
    ("── card (research-only) ──", "Building prop card"),
    ("Brief", "Writing brief"),
]
STEP_RE = re.compile(r"^\s*(OK|WARN|FAILED) \(exit (\d+)\) — (.+?)(?: :: (.+))?$")
WARN_EXIT = 3          # run_daily: every step ran, at least one warned (src/utils/step_status.py)

_PROCS: dict[int, subprocess.Popen] = {}   # reaped on poll so finished runs don't linger as zombies


# ── run control ───────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    proc = _PROCS.get(pid)
    if proc is not None:
        return proc.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_log(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except FileNotFoundError:
        return ""


def run_state() -> dict | None:
    """The newest run: {'log', 'started', 'pid', 'running', 'exit', 'text'} or None."""
    try:
        meta = json.loads(CURRENT.read_text())
    except (FileNotFoundError, ValueError):
        return None
    log = Path(meta["log"])
    text = _read_log(log)
    m = re.search(rf"^{EXIT_MARK} (-?\d+)$", text, re.M)
    exit_code = int(m.group(1)) if m else None
    running = exit_code is None and _pid_alive(int(meta["pid"]))
    if exit_code is None and not running:
        exit_code = -1                          # killed / crashed without reaching the exit marker
    text = re.sub(rf"^{EXIT_MARK} .*$\n?", "", text, flags=re.M)
    return {**meta, "log": log, "running": running, "exit": exit_code, "text": text}


def start_run(dry_run: bool = False) -> bool:
    """dry_run: scripts/run_daily.py --dry-run (no ledger/Supabase writes, today's files untouched)."""
    state = run_state()
    if state and state["running"]:
        return False
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = RUNS_DIR / f"{stamp}.log"
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    with open(log, "w") as fh:
        # The shell appends the exit code so a reloaded page can still read the result.
        proc = subprocess.Popen(
            ["/bin/sh", "-c", f'"$0" -u scripts/run_daily.py{" --dry-run" if dry_run else ""}; echo "{EXIT_MARK} $?"',
             sys.executable],
            cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, start_new_session=True,
        )
    _PROCS[proc.pid] = proc
    CURRENT.write_text(json.dumps({"pid": proc.pid, "log": str(log),
                                   "started": datetime.now(timezone.utc).isoformat()}))
    return True


def stage_of(text: str) -> int:
    """Index of the last stage marker seen (-1 before the first)."""
    idx = -1
    for i, (marker, _) in enumerate(STAGES):
        if marker in text:
            idx = i
    return idx


def step_results(text: str) -> list[tuple[str, int, str, str]]:
    """[(OK|WARN|FAILED, exit code, label, reasons)] in log order. Logs from before 2026-11-24 have no
    status line for the card / brief; those are inferred from the 'Card:' / 'Brief' lines."""
    out = [(m.group(1), int(m.group(2)), m.group(3), m.group(4) or "")
           for m in map(STEP_RE.match, text.splitlines()) if m]
    if any(label in ("Prop card", "Brief") for _, _, label, _ in out):
        return out
    for line in text.splitlines():
        if line.startswith("Card: "):
            out.append(("OK", 0, "Prop card", ""))
        elif line.startswith("Brief (attach"):
            out.append(("OK", 0, "Brief", ""))
        elif line.startswith("Card failed"):
            out.append(("FAILED", 1, "Prop card", ""))
        elif line.startswith("Brief failed"):
            out.append(("FAILED", 1, "Brief", ""))
    return out


# ── data loaders (cached by file mtime, so a finished run is picked up instantly) ──

def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


@st.cache_data(show_spinner=False)
def _load_json(path: str, mtime: float):
    return json.loads(Path(path).read_text()) if mtime else None


@st.cache_data(show_spinner=False)
def _load_csv(path: str, mtime: float):
    return pd.read_csv(path) if mtime else None


def load_json(path: Path):
    return _load_json(str(path), _mtime(path))


def team_board(day: str) -> tuple[list[dict], Path | None]:
    path = latest_revision(ROOT / "data" / "team_picks" / f"{day}.json")
    data = load_json(path)
    return (data or {}).get("picks", []), (path if data else None)


def market_picks(day: str) -> list[dict]:
    data = load_json(latest_revision(ROOT / "data" / "market_picks" / f"{day}.json"))
    return (data or {}).get("picks", [])


def card(day: str) -> dict | None:
    return load_json(ROOT / "data" / "cards" / f"{day}.json")


def feed_lines(day: str) -> list[str]:
    """'DK via ESPN: 0/506 passed (406 stale, 100 future-skew)' from the newest quote snapshot."""
    from src.data.quote_diagnostics import feed_summary
    data = load_json(latest_revision(ROOT / "data" / "quote_snapshots" / f"{day}.json"))
    cov = (data or {}).get("quote_coverage")
    if not cov or not cov.get("timestamp_check_applied"):
        return []
    return list(feed_summary(cov).values())


@st.cache_data(ttl=300, show_spinner=False)
def worker_jobs() -> dict:
    """Supabase job_runs, read-only, refreshed at most every 5 min (src/data/worker_health.py)."""
    from src.data.worker_health import job_status
    return job_status()


def yesterday_results(day: str) -> pd.DataFrame | None:
    path = ROOT / "data" / "team_picks" / "results_log.csv"
    df = _load_csv(str(path), _mtime(path))
    if df is None:
        return None
    y = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    return df[df["game_date"] == y]


# ── formatting helpers ────────────────────────────────────────────────────────

def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def _hcap(v) -> str:
    x = _f(v)
    if x is None:
        return "—"
    return "PK" if abs(x) < 0.05 else f"{x:+.1f}"


def _ml(v) -> str:
    x = _f(v)
    return "—" if x is None else f"{int(x):+d}"


def _tip(start: str | None) -> str:
    if not start:
        return "TBD"
    try:
        t = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(PT)
    except ValueError:
        return "TBD"
    return t.strftime("%-I:%M %p PT")


def _ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    mins = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
    when = t.astimezone(PT).strftime("%-I:%M %p PT")
    if mins < 1:
        return f"{when} · just now"
    if mins < 60:
        return f"{when} · {mins} min ago"
    return f"{when} · {mins // 60}h {mins % 60}m ago"


def _esc(s) -> str:
    return html.escape(str(s))


def game_view(a: dict) -> dict:
    """Everything a game card shows, derived from one saved analysis."""
    home, away = a["home"], a["away"]
    ib = a.get("injury_blind") or {}
    before = ib.get("before_guard") or {}
    sp_edge, ou_edge = _f(a.get("sp_edge")), _f(a.get("ou_edge"))
    tier, ou_tier = a.get("tier") or "NONE", a.get("ou_tier") or "NONE"

    # The spread side the model prefers and the market number on that side.
    market = _f(a.get("market_spread"))
    side = None
    if sp_edge is not None and market is not None and abs(sp_edge) >= 0.05:
        side = home if sp_edge > 0 else away
        side_line = market if sp_edge > 0 else -market
    spread_txt = f"{side} {_hcap(side_line)}" if side else "—"

    was = [f"{lab} {w}" for lab, w in (("spread", before.get("tier")), ("total", before.get("ou_tier")))
           if w in PICK_TIERS]
    best = min((tier, ou_tier), key=lambda t: TIER_RANK.get(t, 3))
    if best == "NONE" and not was:
        best = "PASS"
    cont = {t: c for t, c in (a.get("roster_continuity") or {}).items()
            if isinstance(c, dict) and c.get("flag") == "LOW_CONTINUITY"}
    return dict(home=home, away=away, tier=tier, ou_tier=ou_tier, best=best, was=was, side=side,
                spread_txt=spread_txt, sp_edge=sp_edge, ou_edge=ou_edge, injury=ib.get("teams") or {},
                low_cont=cont, sort=(TIER_RANK.get(best, 3) - (0.5 if was else 0), -abs(sp_edge or 0)))


BADGE = {"STRONG": "strong", "LEAN": "lean", "MONITOR": "monitor", "PASS": "pass", "NONE": "pass"}


def _meter(edge: float | None, lean: float, strong: float) -> str:
    """Edge bar with tick marks at the LEAN and STRONG thresholds."""
    if edge is None:
        return '<div class="meter"></div>'
    top = strong * 1.4
    w = min(abs(edge) / top, 1.0) * 100
    lvl = "strong" if abs(edge) >= strong else "lean" if abs(edge) >= lean else "low"
    return (f'<div class="meter"><div class="fill {lvl}" style="width:{w:.0f}%"></div>'
            f'<span class="tick" style="left:{lean / top * 100:.0f}%"></span>'
            f'<span class="tick" style="left:{strong / top * 100:.0f}%"></span></div>')


def game_card(a: dict, v: dict) -> str:
    home, away = v["home"], v["away"]
    badge = v["best"]
    sub = f'<span class="was">injury-capped · was {", ".join(v["was"])}</span>' if v["was"] else ""
    model_home = _f(a.get("fair_home_handicap"))
    rows = []

    # Spread
    rows.append(
        f'<div class="mkt"><div class="lab">Spread</div>'
        f'<div class="val">Model {home} {_hcap(model_home)} · Mkt {home} {_hcap(a.get("market_spread"))}</div>'
        f'<div class="edge {BADGE.get(v["tier"], "pass")}">{v["spread_txt"]}'
        f'{f" · {abs(v["sp_edge"]):.1f}" if v["sp_edge"] is not None else ""}</div></div>'
        + _meter(v["sp_edge"], MIN_SPREAD_EDGE, STRONG_SPREAD_EDGE))

    # Total
    mt, line = _f(a.get("model_total")), _f(a.get("over_under"))
    if mt is not None and line is not None:
        rows.append(
            f'<div class="mkt"><div class="lab">Total</div>'
            f'<div class="val">Model {mt:.1f} · Line {line:.1f}</div>'
            f'<div class="edge {BADGE.get(v["ou_tier"], "pass")}">{_esc(a.get("ou_direction") or "")} '
            f'{abs(v["ou_edge"] or 0):.1f}</div></div>'
            + _meter(v["ou_edge"], MIN_OU_EDGE, STRONG_OU_EDGE))

    # Moneyline (model vs paired no-vig, home side)
    wp, nv = _f(a.get("model_wp")), _f(a.get("home_no_vig"))
    if wp is not None:
        nv_txt = f" · mkt {nv:.0%}" if nv is not None else ""
        rows.append(
            f'<div class="mkt"><div class="lab">ML</div>'
            f'<div class="val">{home} model {wp:.0%}{nv_txt}</div>'
            f'<div class="edge pass">{_esc(away)} {_ml(a.get("away_ml"))} · {_esc(home)} {_ml(a.get("home_ml"))}'
            f'</div></div>')

    chips = []
    for team, f in v["injury"].items():
        names = ", ".join(f'{p["name"]} ({p["ppg"]:.0f}p)' for p in f.get("players", []))
        chips.append(f'<span class="chip warn" title="{_esc(f.get("reason", ""))}">'
                     f'⚠ {_esc(team)} out: {_esc(names)}</span>')
    for team, c in v["low_cont"].items():
        share = _f(c.get("returning_minutes_share"))
        chips.append(f'<span class="chip info">{_esc(team)} low continuity'
                     f'{f" {share:.0%}" if share is not None else ""}</span>')

    return f"""
<div class="gcard {BADGE.get(badge, 'pass')}">
  <div class="ghead"><span class="tip">{_tip(a.get("start_time"))}</span>
    <span class="badge {BADGE.get(badge, 'pass')}">{badge}</span>{sub}</div>
  <div class="team"><span class="abbr">{_esc(away)}</span><span class="name">{_esc(a.get("away_full", ""))}</span>
    <span class="rating">{_f(a.get("away_rating")) or 0:+.1f}</span></div>
  <div class="team"><span class="abbr">@ {_esc(home)}</span><span class="name">{_esc(a.get("home_full", ""))}</span>
    <span class="rating">{_f(a.get("home_rating")) or 0:+.1f}</span></div>
  <div class="rows">{''.join(rows)}</div>
  {f'<div class="chips">{"".join(chips)}</div>' if chips else ''}
</div>"""


CSS = """
<style>
.block-container {padding-top: 3.5rem; max-width: 1400px;}
.hero {display:flex; justify-content:space-between; align-items:flex-end; gap:1rem; flex-wrap:wrap;
       margin-bottom: .25rem;}
.hero h1 {margin:0; padding:0; font-size:1.9rem; line-height:1.15; letter-spacing:-.02em;}
.hero .sub {opacity:.65; font-size:.95rem;}
.ribbon {font-size:.8rem; padding:.45rem .75rem; border-radius:8px; margin:.5rem 0 1rem;
         background: rgba(245,158,11,.10); border:1px solid rgba(245,158,11,.35);}
.steps {display:flex; gap:.4rem; flex-wrap:wrap; margin:.25rem 0 1rem;}
.step {font-size:.75rem; padding:.2rem .55rem; border-radius:999px; border:1px solid rgba(128,128,128,.35);}
.step.ok {background:rgba(34,197,94,.12); border-color:rgba(34,197,94,.45);}
.step.fail {background:rgba(239,68,68,.14); border-color:rgba(239,68,68,.55);}
.step.warn {background:rgba(245,158,11,.16); border-color:rgba(245,158,11,.6);}
.hgrid {display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:.45rem; margin:.4rem 0 .6rem;}
.htile {border-radius:8px; padding:.4rem .55rem; border:1px solid rgba(128,128,128,.35); font-size:.78rem;}
.htile.ok {background:rgba(34,197,94,.10); border-color:rgba(34,197,94,.45);}
.htile.warn {background:rgba(245,158,11,.14); border-color:rgba(245,158,11,.55);}
.htile.fail {background:rgba(239,68,68,.14); border-color:rgba(239,68,68,.6);}
.htile .hname {font-weight:600;}
.htile .hlast {opacity:.7; font-size:.72rem;}
.healthnote {font-size:.78rem; opacity:.85; margin:-.6rem 0 .8rem; line-height:1.5;}
.grid {display:grid; grid-template-columns:repeat(auto-fill, minmax(330px, 1fr)); gap:14px;}
.gcard {border:1px solid rgba(128,128,128,.28); border-radius:12px; padding:14px 16px;
        background: rgba(128,128,128,.06); border-top:3px solid rgba(128,128,128,.4);}
.gcard.strong {border-top-color:#16a34a;} .gcard.lean {border-top-color:#2563eb;}
.gcard.monitor {border-top-color:#d97706;}
.ghead {display:flex; align-items:center; gap:.5rem; margin-bottom:.5rem;}
.ghead .tip {font-size:.8rem; opacity:.7; margin-right:auto;}
.badge {font-size:.68rem; font-weight:700; letter-spacing:.06em; padding:.15rem .5rem; border-radius:6px;}
.badge.strong {background:#16a34a; color:#fff;} .badge.lean {background:#2563eb; color:#fff;}
.badge.monitor {background:#d97706; color:#fff;} .badge.pass {background:rgba(128,128,128,.25);}
.was {font-size:.7rem; opacity:.75;}
.team {display:flex; align-items:baseline; gap:.5rem; line-height:1.5;}
.team .abbr {font-weight:700; font-size:1.05rem; min-width:3.4rem;}
.team .name {opacity:.7; font-size:.85rem; margin-right:auto; white-space:nowrap; overflow:hidden;
             text-overflow:ellipsis;}
.team .rating {font-variant-numeric:tabular-nums; font-size:.85rem; opacity:.8;}
.rows {margin-top:.6rem; border-top:1px solid rgba(128,128,128,.2); padding-top:.5rem;}
.mkt {display:grid; grid-template-columns:3.2rem 1fr auto; gap:.5rem; align-items:baseline;
      font-size:.82rem; margin-top:.35rem;}
.mkt .lab {opacity:.55; text-transform:uppercase; font-size:.68rem; letter-spacing:.05em;}
.mkt .val {font-variant-numeric:tabular-nums; opacity:.9;}
.mkt .edge {font-weight:600; font-variant-numeric:tabular-nums; white-space:nowrap;}
.edge.strong {color:#16a34a;} .edge.lean {color:#3b82f6;} .edge.monitor {color:#d97706;}
.meter {position:relative; height:4px; border-radius:2px; background:rgba(128,128,128,.18);
        margin:.25rem 0 .2rem 3.7rem;}
.meter .fill {height:100%; border-radius:2px;}
.fill.low {background:rgba(128,128,128,.55);} .fill.lean {background:#3b82f6;} .fill.strong {background:#16a34a;}
.meter .tick {position:absolute; top:-2px; width:1px; height:8px; background:rgba(128,128,128,.7);}
.chips {display:flex; flex-wrap:wrap; gap:.3rem; margin-top:.6rem;}
.chip {font-size:.7rem; padding:.15rem .45rem; border-radius:6px; line-height:1.35;}
.chip.warn {background:rgba(245,158,11,.14); border:1px solid rgba(245,158,11,.4);}
.chip.info {background:rgba(59,130,246,.10); border:1px solid rgba(59,130,246,.35);}
div[data-testid="stMetricValue"] {font-variant-numeric:tabular-nums;}
</style>
"""


# ── page ─────────────────────────────────────────────────────────────────────

STEP_ICON = {"OK": ("ok", "✓"), "WARN": ("warn", "!"), "FAILED": ("fail", "✕")}
LEVEL_ICON = {"green": ("ok", "✓"), "yellow": ("warn", "!"), "red": ("fail", "✕")}


def step_chips(steps: list[tuple[str, int, str, str]]) -> str:
    out = []
    for s, _, label, why in steps:
        cls, icon = STEP_ICON.get(s, ("fail", "?"))
        out.append(f'<span class="step {cls}" title="{_esc(why)}">{icon} {_esc(label)}</span>')
    return "".join(out)


def job_chips(health: dict) -> tuple[str, list[str]]:
    """(chips html, ['validate: AJ Green (MIL): minutes ...']) for the worker jobs."""
    chips, notes = [], []
    for j in health.get("jobs", []):
        cls, icon = LEVEL_ICON[j["level"]]
        why = "; ".join(j["reasons"])
        chips.append(f'<span class="step {cls}" title="{_esc(why)}">{icon} {_esc(j["job"])}</span>')
        if j["level"] != "green":
            notes.append(f'{j["job"]} ({j["level"]}): {why or j["status"]}')
    return "".join(chips), notes


def _local_max_game_date() -> str | None:
    import sqlite3
    db = ROOT / "data" / "nba_edge.db"
    if not db.exists():
        return None
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
        return c.execute("select max(game_date) from game_logs").fetchone()[0]


def health_tiles(day: str, state: dict | None) -> list[dict]:
    """Inputs for dashboard/health_view.sources(), each read defensively (a broken source is a tile, not a crash)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import health_view
    from src.utils import grading
    today = date.fromisoformat(day)
    lag, team_last = [], None
    try:
        team_graded = grading.team_graded_dates()
        team_last = max(team_graded, default=None)
        for kind, folder, graded in (("team", ROOT / "data" / "team_picks", team_graded),
                                     ("market props", ROOT / "data" / "market_picks", grading.market_graded_dates())):
            w = grading.lag_warning(kind, grading.pick_file_dates(folder), graded, day)
            if w:
                lag.append(w)
    except Exception as exc:  # noqa: BLE001
        lag.append(f"grading check failed: {type(exc).__name__}")
    try:
        from src.models.calibration import health as cal_health, ARTIFACT
        cal_warn = cal_health()
        fit = json.loads(ARTIFACT.read_text()).get("fit_date") if ARTIFACT.exists() else None
    except Exception as exc:  # noqa: BLE001
        cal_warn, fit = [f"calibration health check failed: {type(exc).__name__}"], None
    run = None
    if state is not None:
        run = {"exit": state.get("exit"), "started": state.get("started"), "dry": "DRY RUN:" in (state.get("text") or "")}
    return health_view.sources(health=worker_jobs(), today=today, local_max_game_date=_local_max_game_date(),
                               card=card(day), feed_lines=feed_lines(day), grading_lag=lag, team_last_graded=team_last,
                               calibration_warnings=cal_warn, calibration_fit=fit, run=run)


def _health_panel(day: str, state: dict | None) -> None:
    """Top of Today: one tile per data source (green / yellow / red, last success); problems spelled out below."""
    tiles = health_tiles(day, state)            # also puts dashboard/ on sys.path
    import health_view
    st.markdown(health_view.panel_html(tiles, _esc, _ago), unsafe_allow_html=True)
    bad = [t for t in tiles if t["level"] != "green"]
    if bad:
        st.markdown('<div class="healthnote">' + "<br>".join(
            f"{health_view.ICON[t['level']]} <b>{_esc(t['source'])}</b>: {_esc(t['detail'])}" for t in bad)
            + "</div>", unsafe_allow_html=True)


def _progress_panel(state: dict) -> None:
    text = state["text"]
    stage = stage_of(text)
    label = STAGES[stage][1] if stage >= 0 else "Starting"
    elapsed = int((datetime.now(timezone.utc) - datetime.fromisoformat(state["started"])).total_seconds())
    st.progress((stage + 1) / (len(STAGES) + 1), text=f"**{label}…**  ·  {elapsed // 60}:{elapsed % 60:02d} elapsed "
                                                       f"(a full run takes about 1–2 min)")
    tail = "\n".join(text.rstrip().splitlines()[-14:])
    st.code(tail or "waiting for output…", language=None)


@st.fragment(run_every=1.0)
def _live_run() -> None:
    state = run_state()
    if state is None:
        return
    if state["running"]:
        _progress_panel(state)
        return
    # Finished while we were watching: rerun the whole page so the board picks up the new files.
    if st.session_state.get("watching_run") == str(state["log"]):
        st.session_state.pop("watching_run")
        st.session_state["just_finished"] = str(state["log"])
        st.rerun(scope="app")


def render() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    day = slate_today().isoformat()
    state = run_state()
    running = bool(state and state["running"])

    games, board_path = team_board(day)
    generated = None
    if board_path is not None:
        generated = (load_json(board_path) or {}).get("generated_at")
    pretty = date.fromisoformat(day).strftime("%A, %B %-d")

    left, right = st.columns([3, 1], vertical_alignment="bottom")
    with left:
        st.markdown(
            f'<div class="hero"><div><h1>Tonight · {pretty}</h1>'
            f'<div class="sub">{f"Board updated {_ago(generated)}" if generated else "No board for today yet — hit Refresh."}'
            f'</div></div></div>', unsafe_allow_html=True)
    with right:
        if st.button("Running…" if running else "↻  Refresh today's picks", type="primary",
                     disabled=running, width="stretch",
                     help="Runs scripts/run_daily.py: grades yesterday, fetches lines, builds tonight's board and card."):
            if start_run():
                st.session_state["watching_run"] = str(run_state()["log"])
                st.rerun()
            else:
                st.toast("A run is already in progress.")

    _health_panel(day, state)

    st.markdown('<div class="ribbon"><b>Research-only.</b> Probabilities are unvalidated, approval and sizing '
                'are disabled (stake $0). Strength labels describe model-vs-market disagreement, not confidence. '
                'Always check injury and lineup news before acting.</div>', unsafe_allow_html=True)

    if running:
        st.session_state.setdefault("watching_run", str(state["log"]))
        with st.container(border=True):
            _live_run()
    elif state is not None:
        steps = step_results(state["text"])
        just = st.session_state.pop("just_finished", None) == str(state["log"])
        if state["exit"] == 0 and just:
            st.toast("Refresh complete — board updated.", icon="✅")
        elif state["exit"] == WARN_EXIT:
            st.warning("Last run finished **with warnings**: a step got input but produced nothing, or a worker "
                       "job is red. Details below and in **Run log**.")
        elif state["exit"] not in (0, None):
            st.error(f"Last run exited with code {state['exit']}. See **Run log** below.")
        if "DRY RUN:" in state["text"]:
            st.caption("Showing a **dry run** (no ledger/Supabase writes; today's card and picks files untouched). "
                       "The board and Props tab below are from the last real run.")
        if steps:
            st.markdown(f'<div class="steps">{step_chips(steps)}</div>', unsafe_allow_html=True)
            warned = [(label, why) for s, _, label, why in steps if s == "WARN"]
            if warned:
                st.markdown('<div class="healthnote">' + "<br>".join(
                    f"⚠ <b>{_esc(label)}</b>: {_esc(why)}" for label, why in warned) + "</div>",
                    unsafe_allow_html=True)

    views = sorted(((a, game_view(a)) for a in games), key=lambda av: av[1]["sort"])
    the_card = card(day) or {}
    legs = the_card.get("legs", [])
    mpicks = market_picks(day)

    m = st.columns(5)
    m[0].metric("Games", len(games))
    m[1].metric("Team picks", sum(1 for _, v in views if v["best"] in PICK_TIERS))
    m[2].metric("Monitor", sum(1 for _, v in views if v["best"] == "MONITOR"))
    m[3].metric("Injury flags", sum(1 for _, v in views if v["injury"]))
    m[4].metric("Card legs", len(legs))

    tab_games, tab_props, tab_y, tab_log = st.tabs(["Games", f"Props ({len(legs) + len(mpicks)})",
                                                   "Yesterday", "Run log"])

    with tab_games:
        if not games:
            st.info("No team board for today yet. Press **Refresh today's picks**.")
        else:
            show = st.segmented_control("Show", ["All games", "Picks + monitor"], default="All games",
                                        label_visibility="collapsed")
            vis = [(a, v) for a, v in views if show != "Picks + monitor" or v["best"] in PICK_TIERS + ("MONITOR",)]
            st.markdown('<div class="grid">' + "".join(game_card(a, v) for a, v in vis) + "</div>",
                        unsafe_allow_html=True)
            st.caption(f"Edge bars: tick marks at LEAN / STRONG ({MIN_SPREAD_EDGE}/{STRONG_SPREAD_EDGE} pts spread, "
                       f"{MIN_OU_EDGE}/{STRONG_OU_EDGE} pts total). Ratings are SRS + form + HCA + rest. "
                       "INJURY_BLIND games are capped at MONITOR.")

    with tab_props:
        _props_tab(the_card, legs, mpicks)

    with tab_y:
        _yesterday_tab(day)

    with tab_log:
        if state is None:
            st.caption("No dashboard-launched run yet.")
        else:
            st.caption(f"{state['log'].name} · started {_ago(state['started'])}")
            st.code(state["text"] or "(empty)", language=None, height=420)
        brief = ROOT / "data" / "cards" / f"{day}_brief.txt"
        if brief.exists():
            st.download_button("Download today's brief", brief.read_bytes(), file_name=brief.name,
                               mime="text/plain")


def _props_tab(the_card: dict, legs: list[dict], mpicks: list[dict]) -> None:
    if mpicks:
        st.subheader("Model +EV candidates (unvalidated)")
        df = pd.DataFrame([{
            "Player": p.get("player_name"), "Team": p.get("team"), "Opp": p.get("opp"), "Stat": p.get("stat"),
            "Side": p.get("direction"), "Line": p.get("line"), "Odds": _f(p.get("decimal_odds")),
            "Fair p": _f(p.get("prob")), "EV": _f(p.get("ev")), "Venue": p.get("venue"),
        } for p in mpicks])
        st.dataframe(df, hide_index=True, width="stretch", column_config={
            "Fair p": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=1),
            "EV": st.column_config.NumberColumn(format="%.1f%%"),
            "Odds": st.column_config.NumberColumn(format="%.2f"),
        })

    _injury_banner(the_card)
    if not legs:
        st.info("No prop card for today yet." if not the_card else "Nothing cleared the card thresholds today.")
        _excluded_expander(the_card)
        return
    st.subheader("Research card")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import board_view
    projections = board_view.fair_projections(slate_today().isoformat())
    df = board_view.board_frame(legs, projections)

    c1, c2, c3, c4 = st.columns([2, 2, 2, 3])
    pick_games = c1.multiselect("Games", sorted(df["Game"].dropna().unique()), placeholder="All games")
    pick_players = c2.multiselect("Players", sorted(df["Player"].dropna().unique()), placeholder="All players")
    pick_stats = c3.multiselect("Stat", sorted({m.split()[0] for m in df["Market"].dropna()}), placeholder="All stats")
    pick_venue = c4.segmented_control("Venue", ["All", "Kalshi", "Sleeper"], default="All")
    if pick_games:
        df = df[df["Game"].isin(pick_games)]
    if pick_players:
        df = df[df["Player"].isin(pick_players)]
    if pick_stats:
        df = df[df["Market"].str.split().str[0].isin(pick_stats)]
    if pick_venue and pick_venue != "All":
        df = df[df["Venue"] == pick_venue]

    st.dataframe(df, hide_index=True, width="stretch", height=min(38 + 35 * len(df), 720), column_config={
        "Player": st.column_config.TextColumn(width=140),
        "Projection": st.column_config.NumberColumn(format="%.1f", help="ML model projection from the fair board "
                                                    "(only props with a fresh sportsbook line; blank otherwise)"),
        "P raw": st.column_config.NumberColumn(format="%.2f", help="The card formula before calibration"),
        "P calibrated": st.column_config.ProgressColumn(format="%.2f", min_value=0.5, max_value=1.0,
                                                        help="What the card rules use (Calibration page)"),
        "Edge": st.column_config.NumberColumn(format="%+.2f",
                                              help="Calibrated p - Kalshi ask, or - no-vig P for the side"),
        "MIN L10": st.column_config.NumberColumn(format="%.1f"),
        "L10 values": st.column_config.TextColumn(width=150, help="Newest first"),
    })
    st.download_button("Download board (CSV)", df.to_csv(index=False).encode(), file_name=f"board_{slate_today()}.csv",
                       mime="text/csv")
    if projections and df["Projection"].isna().any():
        st.caption(f"Projection shown for {int(df['Projection'].notna().sum())} of {len(df)} legs: the ML model "
                   "only projects props that have a fresh sportsbook line (fair board).")
    elif not projections:
        st.caption("Projection blank: no fair board today (the DK-via-ESPN feed passed no fresh lines).")
    s = the_card.get("stats", {})
    st.caption(f"{the_card.get('shown', len(legs))} shown, {the_card.get('cut', 0)} cut by stat priority/p. "
               f"Hidden: {s.get('excluded_injured', 0)} injured (OUT/D/Q/GTD), {s.get('started', 0)} started, "
               f"{s.get('stale', 0)} stale quotes, {s.get('team_mismatch', 0)} team mismatch — named below. "
               f"P raw: {describe_card_blend()}. P calibrated: "
               f"{the_card.get('calibration') or 'not calibrated (card predates Phase 2)'}. "
               "Kalshi price = ask / most to pay (calibrated p - 5c).")
    _excluded_expander(the_card)
    _ladders(the_card)


def _ladders(the_card: dict) -> None:
    """Every Kalshi threshold for each player/market on the slate (Phase 4d), with ask and calibrated p."""
    ladders = (the_card or {}).get("kalshi_ladders")
    if ladders is None:
        st.caption("Kalshi ladders: this card predates them (2026-11-25); press Refresh.")
        return
    import board_view
    df = board_view.ladder_frame(ladders)
    with st.expander(f"Kalshi ladders: every threshold ({len(df)} strikes, {len(ladders)} player-markets)"):
        if df.empty:
            st.caption("No Kalshi strikes for tonight's eligible players.")
            return
        who = st.multiselect("Player", sorted(df["Player"].unique()), placeholder="All players", key="ladder_players")
        if who:
            df = df[df["Player"].isin(who)]
        st.dataframe(df, hide_index=True, width="stretch", height=min(38 + 35 * len(df), 640), column_config={
            c: st.column_config.NumberColumn(format="%.2f") for c in
            ("YES ask", "NO ask", "Mid", "P raw (YES)", "P cal (YES)")} | {
            c: st.column_config.NumberColumn(format="%+.2f") for c in ("Edge YES", "Edge NO")})
        st.caption("P cal blank = no calibration artifact (fail closed). Edge = calibrated p of that side - its ask. "
                   "The card's rules additionally need raw p > ask, edge >= 5c, ask <= 85c and p >= .68.")


def describe_card_blend() -> str:
    from config.model_config import WEIGHTS
    h = WEIGHTS["card"]["hist_weight"]
    return f"{h:.2f} hit-rate history + {1 - h:.2f} market"


def _injury_banner(the_card: dict) -> None:
    inj = (the_card or {}).get("injury")
    if not inj:
        return
    when = (f"official report {_ago(inj.get('official_as_of')) or 'none'} · "
            f"ESPN {_ago(inj.get('espn_as_of')) or 'none'}")
    if inj.get("stale"):
        st.error(f"**Injury data STALE** (older than {inj.get('max_age_h')} h): {when}. The exclusions below "
                 "may be out of date — press **Refresh** before acting.")
    else:
        st.caption(f"Injury data as of the card run: {when}. Stricter of official and ESPN wins; "
                   "unknown statuses are excluded.")


def excluded_frame(the_card: dict) -> pd.DataFrame:
    rows = (the_card or {}).get("excluded") or []
    cols = ["reason", "player", "team", "game", "stat", "venue", "line", "detail"]
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    order = {"injury": 0, "injury name not matched": 1, "team mismatch": 2, "started": 3, "stale quote": 4,
             "slp hard": 5}
    return df.sort_values(["reason", "player"], key=lambda c: c.map(order).fillna(9) if c.name == "reason" else c)


def _excluded_expander(the_card: dict) -> None:
    if not the_card:
        return
    df = excluded_frame(the_card)
    if "excluded" not in the_card:
        st.caption("This card predates named exclusions (2026-11-24); re-run Refresh to see who was hidden.")
        return
    counts = df["reason"].value_counts().to_dict() if len(df) else {}
    label = "Excluded (" + (", ".join(f"{n} {r}" for r, n in counts.items()) or "none") + ")"
    with st.expander(label, expanded=False):
        if df.empty:
            st.caption("Nothing was hidden.")
        else:
            st.dataframe(df, hide_index=True, width="stretch", column_config={
                "reason": st.column_config.TextColumn("Reason", width=120),
                "detail": st.column_config.TextColumn("Detail (status · source · as of / age)", width="large"),
            })


def _yesterday_tab(day: str) -> None:
    y = (date.fromisoformat(day) - timedelta(days=1))
    df = yesterday_results(day)
    st.subheader(f"Team picks · {y.strftime('%a %b %-d')}")
    if df is None or df.empty:
        st.caption("Nothing graded for yesterday yet.")
    else:
        decided = ("WIN", "LOSS", "PUSH")
        graded = df[df["spread_result"].isin(decided) | df.get("ml_result", pd.Series(dtype=str)).isin(decided)
                    | df.get("ou_result", pd.Series(dtype=str)).isin(decided)]
        if graded.empty:
            st.caption(f"{len(df)} games graded; no spread/ML/total picks were made.")
        cols = [c for c in ["away", "home", "tier", "market_spread", "actual_margin", "spread_result",
                            "ml_result", "ou_result", "spread_clv", "total_clv"] if c in df.columns]
        st.dataframe((graded if not graded.empty else df)[cols], hide_index=True, width="stretch")
    brief = ROOT / "data" / "cards" / f"{day}_brief.txt"
    if brief.exists():
        with st.expander("Daily brief (yesterday's results, 7-day bets ledger)"):
            st.code(brief.read_text(), language=None)
