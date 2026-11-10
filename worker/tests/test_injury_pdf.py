from collections import Counter
from datetime import date, datetime

import pytest

from conftest import FIXTURES
from prop_worker import config
from prop_worker.injury_pdf import flip_name, parse_report

REPORT = parse_report((FIXTURES / "injury_report_2026-11-10_01_00PM.pdf").read_bytes())
BY_NAME = {r.player_name: r for r in REPORT.rows}


def test_title_time_and_no_problems():
    assert REPORT.report_ts == datetime(2026, 11, 10, 13, 0, tzinfo=config.ET)
    assert REPORT.problems == []


def test_row_counts_per_game_and_status():
    assert len(REPORT.rows) == 46
    per_game = Counter(r.matchup for r in REPORT.rows)
    assert per_game == {"ORL@SAC": 12, "OKC@POR": 10, "MIA@UTA": 8, "BOS@PHI": 7, "LAC@PHX": 6, "MIL@NYK": 3}
    assert Counter(r.status for r in REPORT.rows) == {"Out": 34, "Questionable": 9, "Available": 2, "Probable": 1}


def test_wrapped_reasons_are_joined_in_reading_order():
    assert BY_NAME["Dominick Barlow"].reason == "Injury/Illness - Low Back Stress Reaction"
    assert BY_NAME["Ariel Hukporti"].reason == "Injury/Illness - Right Achilles Tendon Rupture / Tear"
    assert BY_NAME["Chet Holmgren"].reason == "Injury/Illness - Right Knee/ Injury Recovery"


def test_team_and_matchup_carry_forward_across_page_break():
    r = BY_NAME["Yanic Konan Niederhauser"]          # first row on page 2
    assert (r.team_name, r.matchup, r.game_date) == ("LA Clippers", "LAC@PHX", date(2026, 11, 10))
    assert BY_NAME["Domantas Sabonis"].team_name == "Sacramento Kings"   # first row on page 3


def test_names_and_suffixes():
    assert BY_NAME["Jaren Jackson Jr."].team_name == "Utah Jazz"
    assert BY_NAME["Tristan da Silva"].status == "Questionable"
    assert BY_NAME["Christopher Cenac Jr."].reason == "G League - On Assignment"


def test_not_submitted_teams_including_tomorrow():
    assert (date(2026, 11, 10), "Phoenix Suns") in REPORT.not_submitted
    assert (date(2026, 11, 11), "Boston Celtics") in REPORT.not_submitted
    assert len(REPORT.not_submitted) == 21
    assert all(r.game_date == date(2026, 11, 10) for r in REPORT.rows)


@pytest.mark.parametrize("raw,name", [
    ("Jackson Jr., Jaren", "Jaren Jackson Jr."), ("Udeh, Jr., Ernest", "Ernest Udeh, Jr."),
    ("da Silva, Tristan", "Tristan da Silva"), ("Nance", "Nance"),
])
def test_flip_name(raw, name):
    assert flip_name(raw) == name


def test_garbage_pdf_raises_so_the_job_errors_loudly():
    with pytest.raises(Exception):
        parse_report(b"%PDF-1.4 not really")
