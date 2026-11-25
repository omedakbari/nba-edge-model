"""Step status beyond exit codes: OK / WARN / FAILED.

A step that received input but produced nothing (quotes offered -> 0 lines, picks saved -> 0 graded) must
not show as OK. Such a step calls warn(reason): the reason is printed and, when the step runs under
nba.py's run() or scripts/run_daily.py, appended as one JSON line to the file named by $NBA_WARN_FILE.
The runner reads that file after the step and records WARN instead of OK; the exit code stays 0 so the
rest of the workflow still runs.

Log line format (parsed by dashboard/today.py STEP_RE):
    "  OK (exit 0) — <label>"
    "  WARN (exit 0) — <label> :: <reason>; <reason>"
    "  FAILED (exit 2) — <label>"
The worker package has the same protocol in prop_worker/util.py (warn) so the card step can report too.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

WARN_ENV = "NBA_WARN_FILE"
SEP = " :: "
WARN_EXIT = 3          # run_daily's exit code when every step ran but at least one warned


def warn(reason: str, step: str = "") -> None:
    print(f"  WARN: {reason}", flush=True)
    path = os.getenv(WARN_ENV)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"step": step, "reason": reason}) + "\n")
    except OSError:
        pass


def warn_if_empty(n_in: int, n_out: int, what: str, step: str = "") -> bool:
    """warn() when n_in > 0 and n_out == 0. Returns True if it warned."""
    if n_in > 0 and n_out == 0:
        warn(f"{what}: {n_in} in, 0 out", step)
        return True
    return False


def read_warnings(path: str | Path | None) -> list[dict]:
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("reason"):
            out.append(item)
    return out


def status_of(code: int, warnings: list) -> str:
    return "FAILED" if code else ("WARN" if warnings else "OK")


def status_line(label: str, code: int, warnings: list[dict] | None = None) -> str:
    warnings = warnings or []
    s = status_of(code, warnings)
    tail = (SEP + "; ".join(w["reason"] for w in warnings)) if s == "WARN" else ""
    return f"  {s} (exit {code}) — {label}{tail}"
