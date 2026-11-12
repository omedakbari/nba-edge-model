"""Shared hit-rate rules: the same golden cases also run in web/ (web/lib/hitRates.test.ts)."""

import json
from datetime import date

import pytest

from conftest import FIXTURES
from prop_worker.hitrates import GameLine, consensus_line, hit_rates, implied, no_vig_over

CASES = json.loads((FIXTURES / "shared" / "hit_rates_cases.json").read_text())
P = CASES["params"]
GAMES = [GameLine(date.fromisoformat(g["game_date"]), g["opp"], g["minutes"], g["did_not_play"],
                  g["pts"], g["reb"], g["ast"], g["fg3m"]) for g in CASES["games"]]


@pytest.mark.parametrize("case", CASES["hit_rates"], ids=lambda c: f"{c['stat']}-{c['line']}-{c['side']}")
def test_hit_rate_golden_cases(case):
    got = hit_rates(GAMES, case["stat"], case["line"], before=date.fromisoformat(P["before"]),
                    season_start=date.fromisoformat(P["season_start"]), opp=P["opp"],
                    h2h_since=date.fromisoformat(P["h2h_since"]), side=case["side"])
    assert {k: [r.hits, r.n, r.pushes] for k, r in got.items()} == case["expect"]


@pytest.mark.parametrize("case", CASES["consensus"], ids=lambda c: json.dumps(c["lines"]))
def test_consensus_golden_cases(case):
    assert list(consensus_line(case["lines"])) == case["expect"]


@pytest.mark.parametrize("case", CASES["no_vig_over"], ids=lambda c: json.dumps(c["pairs"]))
def test_no_vig_golden_cases(case):
    got = no_vig_over([tuple(p) for p in case["pairs"]])
    assert (got is None and case["expect"] is None) or round(got, 6) == case["expect"]


def test_hand_checked_case():
    # pts over 24.5, last 5 played: 30, 25, 18, 25, 31 -> 4 of 5; the slate-date game and the DNP don't count
    l5 = hit_rates(GAMES, "pts", 24.5, before=date(2026, 11, 11), season_start=date(2026, 10, 1),
                   opp="BOS", h2h_since=date(2025, 10, 1))["l5"]
    assert (l5.hits, l5.n, l5.pct) == (4, 5, 0.8)


def test_implied_probability():
    assert implied(-110) == pytest.approx(0.5238, abs=1e-4) and implied(150) == pytest.approx(0.4)
    assert implied(None) is None


def test_bad_inputs():
    with pytest.raises(ValueError):
        hit_rates(GAMES, "blk", 1.5, before=date(2026, 11, 11), season_start=date(2026, 10, 1), opp=None,
                  h2h_since=date(2025, 10, 1))
    with pytest.raises(ValueError):
        hit_rates(GAMES, "pts", 1.5, before=date(2026, 11, 11), season_start=date(2026, 10, 1), opp=None,
                  h2h_since=date(2025, 10, 1), side="push")
