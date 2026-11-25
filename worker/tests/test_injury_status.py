"""injury_status: status codes, fail-closed unknowns, stricter-wins across sources."""

import pytest

from prop_worker import injury_status as inj


@pytest.mark.parametrize("raw,code", [
    ("Out", "OUT"), ("OUT", "OUT"), ("Doubtful", "D"), ("Questionable", "Q"), ("Day-To-Day", "GTD"),
    ("day to day", "GTD"), ("DTD", "GTD"), ("GTD", "GTD"), ("Probable", "P"), ("Available", "AVL"),
    ("  questionable ", "Q"), ("Suspension", "OUT"), ("Not With Team", "OUT"),
    ("Reconditioning", "UNK"), ("", "UNK"), (None, "UNK"),
])
def test_codes(raw, code):
    assert inj.code(raw) == code


def test_excluded_set_fails_closed():
    assert {c for c in inj.SEVERITY if inj.excluded(c)} == {"OUT", "D", "Q", "GTD", "UNK"}
    assert not inj.excluded("P") and not inj.excluded("AVL")


def test_stricter_wins_and_tie_keeps_first():
    off = {"status": "Probable", "as_of": "t1"}
    espn = {"status": "Day-To-Day", "as_of": "t2"}
    c = inj.combine(off, espn)
    assert (c["code"], c["source"], c["as_of"]) == ("GTD", "espn", "t2")
    assert c["sources"] == {"official": {"status": "Probable", "code": "P", "as_of": "t1"},
                            "espn": {"status": "Day-To-Day", "code": "GTD", "as_of": "t2"}}
    same = inj.combine({"status": "Out", "as_of": "a"}, {"status": "Out", "as_of": "b"})
    assert same["source"] == "official"
    assert inj.combine({"status": "Out"}, {"status": "Probable"})["source"] == "official"


def test_one_source_only_and_none():
    assert inj.combine(None, {"status": "Out", "as_of": "x"})["source"] == "espn"
    assert inj.combine(None, None) is None


def test_label_names_source_time_and_other_source():
    c = inj.combine({"status": "Probable", "as_of": "T1"}, {"status": "Day-To-Day", "as_of": "T2"})
    assert inj.label(c) == "GTD (Day-To-Day, espn as of T2; official Probable)"
