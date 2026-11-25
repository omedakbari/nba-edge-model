"""injury_status: one place for how an injury-report status becomes a card decision.

Rules (AUDIT_2026-11-24.md, item 2):
  * codes      OUT, D (doubtful), Q (questionable), GTD (game-time / day-to-day), UNK (anything we don't
               recognise), P (probable), AVL (available)
  * excluded   OUT, D, Q, GTD and UNK. An unknown status fails CLOSED: a word we've never seen is treated
               as "maybe not playing", never as "fine".
  * conflicts  when the official NBA report and ESPN both list a player, the STRICTER status wins
               (official Probable + ESPN Day-To-Day -> GTD, excluded). The winning source and its
               timestamp are kept, and so is the other source's status, so the dashboard can show both.
"""

from __future__ import annotations

EXCLUDED = frozenset({"OUT", "D", "Q", "GTD", "UNK"})
SAFE = frozenset({"P", "AVL"})

# Higher = stricter. UNK sits above P/AVL (fail closed) but below every real "may not play" status, so a
# known status from the other source is the one shown when it is at least as strict.
SEVERITY = {"OUT": 6, "D": 5, "Q": 4, "GTD": 3, "UNK": 2, "P": 1, "AVL": 0}

_ALIASES = {
    "out": "OUT", "o": "OUT", "suspended": "OUT", "suspension": "OUT", "not with team": "OUT",
    "inactive": "OUT", "injured reserve": "OUT", "out for season": "OUT",
    "doubtful": "D", "d": "D",
    "questionable": "Q", "q": "Q",
    "day-to-day": "GTD", "day to day": "GTD", "dtd": "GTD", "gtd": "GTD", "game-time decision": "GTD",
    "game time decision": "GTD",
    "probable": "P", "p": "P",
    "available": "AVL", "active": "AVL",
}


def code(status: str | None) -> str:
    """Normalise a raw status ('Day-To-Day', 'Out', 'questionable ') to a code; unknown -> 'UNK'."""
    key = " ".join((status or "").strip().lower().split())
    return _ALIASES.get(key, "UNK")


def excluded(c: str | None) -> bool:
    return c in EXCLUDED


def stricter(a: dict | None, b: dict | None) -> dict | None:
    """The stricter of two status dicts (each has 'code'); ties keep `a` (pass the official one first)."""
    if a is None:
        return b
    if b is None:
        return a
    return b if SEVERITY.get(b["code"], SEVERITY["UNK"]) > SEVERITY.get(a["code"], SEVERITY["UNK"]) else a


def combine(official: dict | None, espn: dict | None) -> dict | None:
    """Card-facing status for one player.

    official / espn: {'status', 'reason', 'as_of'} or None. Returns
    {'status', 'code', 'reason', 'source', 'as_of', 'sources': {'official': {...}, 'espn': {...}}}
    where 'source' / 'as_of' belong to whichever source set the (stricter) code.
    """
    cand = {}
    for name, row in (("official", official), ("espn", espn)):
        if row is not None:
            cand[name] = {**row, "code": code(row.get("status")), "source": name}
    if not cand:
        return None
    win = stricter(cand.get("official"), cand.get("espn"))
    return {"status": win.get("status"), "code": win["code"], "reason": win.get("reason"),
            "source": win["source"], "as_of": win.get("as_of"),
            "sources": {k: {"status": v.get("status"), "code": v["code"], "as_of": v.get("as_of")}
                        for k, v in cand.items()}}


def label(inj: dict | None) -> str:
    """'OUT (official 6:30 PM ET report seen 23:30Z)'-style short text for exclusion lists."""
    if not inj:
        return ""
    other = [f"{k} {v['status']}" for k, v in (inj.get("sources") or {}).items() if k != inj.get("source")]
    tail = f"; {', '.join(other)}" if other else ""
    return f"{inj['code']} ({inj.get('status')}, {inj.get('source')} as of {inj.get('as_of') or '?'}{tail})"
