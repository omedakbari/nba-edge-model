from datetime import date

import pytest

from prop_worker.names import (Candidate, PlayerIndex, kalshi_player_name, normalize_name,
                               parse_kalshi_ticker, season_start)

SEASON = date(2026, 10, 1)


@pytest.mark.parametrize("raw,norm", [
    ("Nikola Jokić", "nikola jokic"),
    ("Jaime Jaquez Jr.", "jaime jaquez"),
    ("Ernest Udeh, Jr.", "ernest udeh"),
    ("De'Aaron Fox", "deaaron fox"),
    ("Karl-Anthony Towns", "karl anthony towns"),
    ("P.J. Washington", "pj washington"),
    ("  Gary   Trent Jr ", "gary trent"),
    ("Robert Williams III", "robert williams"),
    ("Tarik Biberović", "tarik biberovic"),
    ("", ""),
])
def test_normalize_name(raw, norm):
    assert normalize_name(raw) == norm


@pytest.mark.parametrize("today,start", [
    (date(2026, 11, 10), date(2026, 10, 1)), (date(2027, 3, 1), date(2026, 10, 1)),
    (date(2027, 8, 15), date(2027, 10, 1)),
])
def test_season_start(today, start):
    assert season_start(today) == start


def _idx():
    rows = [(1, "Nicolas Claxton", "BKN", date(2026, 11, 8)), (2, "Jalen Williams", "OKC", date(2026, 11, 8)),
            (3, "Jaylin Williams", "OKC", date(2026, 11, 8)), (4, "Jaime Jaquez Jr.", "MIL", date(2026, 11, 8)),
            (5, "Cameron Thomas", "BKN", date(2026, 11, 8)), (6, "Cam Johnson", "BKN", date(2026, 11, 8)),
            (7, "Jalen Brunson", "NYK", date(2026, 11, 8)),
            (8, "Tobias Harris", "DET", date(2026, 5, 17)),          # last season's team only
            (9, "Henri Veesaar", None, None),                         # rookie, no game logs
            (10, "Marcus Morris", "CLE", date(2025, 3, 1)), (11, "Marcus Morris", "LAC", date(2024, 3, 1))]
    return PlayerIndex([Candidate(i, n, t, normalize_name(n), d) for i, n, t, d in rows])


def test_exact_team_name():
    r = _idx().match("Jaime Jaquez Jr.", "MIL")
    assert (r.player_id, r.method) == (4, "team_name")


def test_last_name_and_initial_on_same_team():
    r = _idx().match("Nic Claxton", "BKN")
    assert (r.player_id, r.method) == (1, "team_last_initial")
    assert _idx().match("Cam Thomas", "BKN").player_id == 5


def test_initial_must_match():
    assert _idx().match("Mikal Claxton", "BKN").player_id is None


def test_never_matches_across_teams_with_current_season_evidence():
    r = _idx().match("Jalen Brunson", "BOS", stale_before=SEASON)
    assert r.player_id is None and r.reason == "other_team_only"
    assert r.candidates == [{"player_id": 7, "name": "Jalen Brunson", "team": "NYK", "team_as_of": "2026-11-08"}]


def test_offseason_move_links_when_name_unique_and_team_stale():
    r = _idx().match("Tobias Harris", "SAS", stale_before=SEASON)
    assert (r.player_id, r.method) == (8, "name_unique_stale_team")


def test_rookie_without_team_links_when_name_unique():
    r = _idx().match("Henri Veesaar", "ATL", stale_before=SEASON)
    assert (r.player_id, r.method) == (9, "name_unique_stale_team")


def test_stale_fallback_off_without_season_cutoff():
    assert _idx().match("Tobias Harris", "SAS").player_id is None


def test_stale_fallback_needs_unique_name():
    r = _idx().match("Marcus Morris", "NYK", stale_before=SEASON)
    assert r.player_id is None and r.reason == "ambiguous_name" and len(r.candidates) == 2


def test_ambiguous_last_initial_is_not_guessed():
    r = _idx().match("J. Williams", "OKC")
    assert r.player_id is None and r.reason == "ambiguous_last_initial" and len(r.candidates) == 2


def test_missing_team_or_name():
    assert _idx().match("Jalen Brunson", None).reason == "no_team"
    assert _idx().match("", "NYK").reason == "empty_name"


def test_unknown_player():
    r = _idx().match("Totally New Rookie", "NYK", stale_before=SEASON)
    assert r.player_id is None and r.reason == "no_candidate" and r.candidates == []


def test_move_reindexes_without_leaving_a_ghost():
    idx = _idx()
    idx.move(8, "SAS", date(2026, 11, 10))
    assert idx.match("Tobias Harris", "SAS").method == "team_name"
    assert idx.match("Tobias Harris", "DET").player_id is None
    assert len(idx.by_name["tobias harris"]) == 1


def test_parse_kalshi_prop_ticker():
    t = parse_kalshi_ticker("KXNBAPTS-26NOV10MIAUTA-UTABSENSABAUGH28-25")
    assert (t.series, t.date_code, t.away, t.home) == ("KXNBAPTS", "26NOV10", "MIA", "UTA")
    assert (t.fragment, t.team, t.tail) == ("UTABSENSABAUGH28", "UTA", "25")


def test_parse_kalshi_game_tickers():
    g = parse_kalshi_ticker("KXNBAGAME-26NOV13UTAHOU-UTA")
    assert (g.fragment, g.team, g.tail, g.away, g.home) == (None, None, "UTA", "UTA", "HOU")
    s = parse_kalshi_ticker("KXNBASPREAD-26NOV11OKCPOR-POR8")
    assert s.tail == "POR8" and s.fragment is None


def test_fragment_team_must_be_in_the_game():
    t = parse_kalshi_ticker("KXNBAPTS-26NOV10MIAUTA-BOSJTATUM0-25")
    assert t.fragment == "BOSJTATUM0" and t.team is None


@pytest.mark.parametrize("bad", ["", "KXNBAPTS", "KXNBAPTS-NOTADATE-X-1"])
def test_parse_kalshi_garbage(bad):
    assert parse_kalshi_ticker(bad) is None


def test_kalshi_player_name():
    assert kalshi_player_name({"title": "Brice Sensabaugh: 25+ points"}) == "Brice Sensabaugh"
    assert kalshi_player_name({"yes_sub_title": "Myles Turner: 10+"}) == "Myles Turner"
