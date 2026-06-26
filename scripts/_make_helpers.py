"""
Helpers called by Makefile targets. Not a user-facing script.

Usage (internal):
    python scripts/_make_helpers.py freshness
    python scripts/_make_helpers.py doctor
    python scripts/_make_helpers.py preflight     # used by 'make picks'
"""

import os
import sys
import datetime
import sqlite3
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DB_PATH, CURRENT_SEASON


def _db_freshness():
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute(
        f"SELECT MAX(game_date), COUNT(*) FROM game_logs WHERE season=?", (CURRENT_SEASON,)
    ).fetchone()
    po = conn.execute(
        "SELECT COUNT(*) FROM game_logs WHERE season=? AND season_type='Playoffs'",
        (CURRENT_SEASON,),
    ).fetchone()[0]
    conn.close()
    return row[0], row[1], po


def cmd_freshness():
    from src.utils.season import detect_phase
    print()
    print("================================================================")
    print("  FRESHNESS CHECK")
    print("================================================================")
    max_d, total, po = _db_freshness()
    today = date.today().isoformat()
    gap = (date.fromisoformat(today) - date.fromisoformat(max_d)).days
    phase = detect_phase()
    stale_threshold = 7 if phase in ("playoffs", "play-in") else 1
    marker = "OK" if gap <= stale_threshold else "STALE"
    print(f"  DB max game date : {max_d}  (gap: {gap}d)  [{marker}]")
    print(f"  {CURRENT_SEASON} rows     : {total:,}  (Playoffs: {po:,})")

    import pandas as pd
    feat = ROOT / "data" / "processed" / "features.parquet"
    if feat.exists():
        df = pd.read_parquet(feat)
        fmax = str(df["game_date"].max())[:10]
        fgap = (date.fromisoformat(today) - date.fromisoformat(fmax)).days
        fmark = "OK" if fgap <= stale_threshold else "STALE"
        print(f"  features.parquet : {fmax}  (gap: {fgap}d)  [{fmark}]")
    else:
        print("  features.parquet : NOT FOUND — run: make train")
        fgap = 999

    mdir = ROOT / "data" / "processed" / "models"
    if mdir.exists():
        pkls = list(mdir.glob("*.pkl"))
        if pkls:
            oldest = min(pkls, key=os.path.getmtime)
            age_d = (
                datetime.datetime.now()
                - datetime.datetime.fromtimestamp(os.path.getmtime(oldest))
            ).days
            print(f"  oldest model     : {oldest.name}  ({age_d}d old)")

    sys.exit(1 if gap > stale_threshold else 0)


def cmd_doctor():
    print()
    print("================================================================")
    print("  DOCTOR  —  system health check")
    print("================================================================")
    ok = True

    # --- Data freshness ---
    print("\n  --- Data freshness ---")
    from src.utils.season import detect_phase as _detect_phase
    phase = _detect_phase()
    stale_threshold = 7 if phase in ("playoffs", "play-in") else 1
    max_d, total, po = _db_freshness()
    today = date.today().isoformat()
    gap = (date.fromisoformat(today) - date.fromisoformat(max_d)).days
    marker = "OK" if gap <= stale_threshold else "STALE"
    if gap > stale_threshold:
        ok = False
    print(f"  DB max date      : {max_d}  (gap: {gap}d)  [{marker}]")
    print(f"  {CURRENT_SEASON} rows     : {total:,}  Playoffs: {po:,}")

    import pandas as pd
    feat = ROOT / "data" / "processed" / "features.parquet"
    if feat.exists():
        df = pd.read_parquet(feat)
        fmax = str(df["game_date"].max())[:10]
        fgap = (date.fromisoformat(today) - date.fromisoformat(fmax)).days
        fmark = "OK" if fgap <= stale_threshold else "STALE"
        if fgap > stale_threshold:
            ok = False
        print(f"  features.parquet : {fmax}  (gap: {fgap}d)  [{fmark}]")
    else:
        print("  features.parquet : NOT FOUND")
        ok = False

    # --- Season phase ---
    print("\n  --- Season phase ---")
    from src.utils.season import get_season_context
    ctx = get_season_context()
    print(f"  Phase            : {ctx['phase'].upper()}  (season: {ctx['season_id']})")

    # --- Model staleness ---
    print("\n  --- Model staleness ---")
    mdir = ROOT / "data" / "processed" / "models"
    if mdir.exists():
        for p in sorted(mdir.glob("*.pkl")):
            age_d = (
                datetime.datetime.now()
                - datetime.datetime.fromtimestamp(os.path.getmtime(p))
            ).days
            marker = "OK" if age_d < 60 else "STALE"
            print(f"  {p.name:<25} {age_d:>3}d old  [{marker}]")
    else:
        print("  No models found — run: make train")
        ok = False

    print()
    if ok:
        print("  STATUS: OK — ready to run picks")
    else:
        print("  STATUS: NEEDS ATTENTION — see items marked [STALE] above")
    sys.exit(0 if ok else 1)


def cmd_preflight():
    """Pre-check before generating picks. Fails loudly if data is stale or offseason."""
    from src.utils.season import detect_phase

    phase = detect_phase()
    if phase == "offseason":
        print("\n  ERROR: Offseason — no games in the last 14 days. Picks not available.")
        sys.exit(1)

    max_d, _total, _po = _db_freshness()
    today = date.today().isoformat()
    gap = (date.fromisoformat(today) - date.fromisoformat(max_d)).days
    stale_threshold = 7 if phase in ("playoffs", "play-in") else 1
    if gap > stale_threshold:
        print(f"\n  ERROR: DB is {gap} days stale (max: {max_d}). Run: make pull")
        sys.exit(1)

    print(f"  Pre-flight OK  |  phase={phase.upper()}  |  DB max={max_d}")
    sys.exit(0)


if __name__ == "__main__":
    cmds = {"freshness": cmd_freshness, "doctor": cmd_doctor, "preflight": cmd_preflight}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(f"Usage: python scripts/_make_helpers.py [{' | '.join(cmds)}]")
        sys.exit(1)
    cmds[sys.argv[1]]()
