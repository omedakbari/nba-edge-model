"""Parser for the official NBA injury report PDF (ak-static.cms.nba.com/referee/injury/...).

Layout (landscape table, 7 columns): Game Date | Game Time | Matchup | Team | Player Name |
Current Status | Reason. Date/time/matchup appear only on a game's first row, the team only on its
first row; both carry forward, including across page breaks. Long reasons wrap onto a line above
and a line below the player's row, so every reason-only line is attached to the nearest player row.
Teams that haven't filed show "NOT YET SUBMITTED" instead of players.

Words are read with x_tolerance=1: pdfplumber's default glues every word together on this PDF.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime

from . import config

COLS = ("game_date", "game_time", "matchup", "team", "player", "status", "reason")
# x of each column's left edge on the 2026-27 report (page width 842); used when no header is found.
DEFAULT_X = (23.1, 119.6, 200.0, 264.2, 425.0, 585.7, 666.1)
STATUSES = {"Out", "Doubtful", "Questionable", "Probable", "Available"}
NOT_SUBMITTED = "NOT YET SUBMITTED"
_TITLE = re.compile(r"Injury Report:\s*(\d{2}/\d{2}/\d{2})\s+(\d{1,2}:\d{2})\s*(AM|PM)")
_PAGE = re.compile(r"^Page \d+ of \d+$")


@dataclass
class InjuryRow:
    game_date: date
    matchup: str
    away: str
    home: str
    team_name: str
    player_raw: str          # 'Jackson Jr., Jaren'
    player_name: str         # 'Jaren Jackson Jr.'
    status: str
    reason: str


@dataclass
class Report:
    report_ts: datetime | None            # ET, from the title line
    rows: list[InjuryRow]
    not_submitted: list[tuple[date, str]]  # (game_date, team_name)
    problems: list[str]


def flip_name(raw: str) -> str:
    """'Jackson Jr., Jaren' -> 'Jaren Jackson Jr.'; 'Udeh, Jr., Ernest' -> 'Ernest Udeh, Jr.'.

    Split on the LAST comma: the first name never has one, the last name sometimes does.
    """
    if "," not in raw:
        return raw.strip()
    last, first = raw.rsplit(",", 1)
    return f"{first.strip()} {last.strip()}".strip()


def _lines(words: list[dict], tol: float = 2.5) -> list[tuple[float, list[dict]]]:
    out: list[tuple[float, list[dict]]] = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if out and abs(out[-1][0] - w["top"]) <= tol:
            out[-1][1].append(w)
        else:
            out.append((w["top"], [w]))
    return out


def _column_x(words: list[dict]) -> tuple[float, ...] | None:
    for _, line in _lines(words):
        texts = [w["text"] for w in line]
        if "Matchup" in texts and "Reason" in texts and texts.count("Game") >= 2:
            games = [w["x0"] for w in line if w["text"] == "Game"]
            x = {w["text"]: w["x0"] for w in line}
            try:
                return (games[0], games[1], x["Matchup"], x["Team"], x["Player"], x["Current"], x["Reason"])
            except KeyError:
                return None
    return None


def _cells(line: list[dict], xs: tuple[float, ...]) -> dict[str, str]:
    cells: dict[str, list[str]] = {}
    for w in sorted(line, key=lambda w: w["x0"]):
        i = max((k for k, x in enumerate(xs) if w["x0"] >= x - 3), default=0)
        cells.setdefault(COLS[i], []).append(w["text"])
    return {k: " ".join(v) for k, v in cells.items()}


def parse_report(content: bytes) -> Report:
    import pdfplumber

    problems: list[str] = []
    report_ts = None
    rows: list[InjuryRow] = []
    not_submitted: list[tuple[date, str]] = []
    cur = {"game_date": None, "matchup": None, "team": None}
    xs = None

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            words = page.extract_words(x_tolerance=1)
            xs = _column_x(words) or xs or DEFAULT_X
            anchors: list[dict] = []
            fragments: list[tuple[float, dict]] = []
            for top, line in _lines(words):
                text = " ".join(w["text"] for w in line)
                if report_ts is None and (m := _TITLE.search(text)):
                    report_ts = datetime.strptime(" ".join(m.groups()), "%m/%d/%y %I:%M %p").replace(tzinfo=config.ET)
                if text.startswith("Injury Report:") or _PAGE.match(text) or \
                        ("Matchup" in text and "Reason" in text):
                    continue
                cells = _cells(line, xs)
                if cells.get("player") or NOT_SUBMITTED in text:
                    anchors.append({"top": top, **cells, "_frag": []})
                else:
                    fragments.append((top, cells))
            for top, cells in fragments:
                if not anchors:
                    problems.append(f"page {page_no}: text with no player row: {cells}")
                    continue
                nearest = min(anchors, key=lambda a: abs(a["top"] - top))
                nearest["_frag"].append((top, cells))
            for a in anchors:
                for k in ("game_date", "matchup", "team"):
                    for _, frag in a["_frag"]:           # a centered team/matchup cell on a wrap line
                        if frag.get(k) and not a.get(k):
                            a[k] = frag[k]
                    if a.get(k):
                        cur[k] = a[k]
                if NOT_SUBMITTED in " ".join(str(v) for k, v in a.items() if k in COLS):
                    if cur["game_date"] and cur["team"]:
                        not_submitted.append((_date(cur["game_date"]), cur["team"]))
                    continue
                parts = sorted([(t, f.get("reason", "")) for t, f in a["_frag"]] + [(a["top"], a.get("reason", ""))])
                reason = " ".join(p for _, p in parts if p).strip()
                status = a.get("status", "").strip()
                if status not in STATUSES:
                    problems.append(f"page {page_no}: {a.get('player')}: unknown status {status!r}")
                    continue
                if not (cur["game_date"] and cur["matchup"] and cur["team"]):
                    problems.append(f"page {page_no}: {a.get('player')}: no game/team carried forward")
                    continue
                away, _, home = cur["matchup"].partition("@")
                rows.append(InjuryRow(_date(cur["game_date"]), cur["matchup"], away, home, cur["team"],
                                      a["player"], flip_name(a["player"]), status, reason))
    if report_ts is None:
        problems.append("no 'Injury Report: <date> <time>' title found")
    return Report(report_ts, rows, not_submitted, problems)


def _date(s: str) -> date:
    return datetime.strptime(s.strip(), "%m/%d/%Y").date()
