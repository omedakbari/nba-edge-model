"""Phase 4 board fields: per-leg hit rates at the leg's own line/side, L10 values, MIN L10, full Kalshi ladders."""

from datetime import timedelta

import pytest

from prop_worker import card
from prop_worker.calibration import Calibrator
from prop_worker.hitrates import GameLine
from test_card import D, H2H, SZN, prop, rows_for
from test_calibration_apply import ART


def test_leg_stats_at_the_legs_line_and_side():
    games = [GameLine(D - timedelta(days=i + 1), "LAL" if i < 2 else "BOS", 30.0 + i, False, 20, v, 4, 2)
             for i, v in enumerate([6, 4, 5, 7, 3, 5, 6, 2, 8, 5, 9, 1, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5])]
    over = card.leg_stats(games, "reb", 4.5, "over", before=D, season_start=SZN, opp="LAL", h2h_since=H2H)
    under = card.leg_stats(games, "reb", 4.5, "under", before=D, season_start=SZN, opp="LAL", h2h_since=H2H)
    assert over["l5"] == "3/5" and under["l5"] == "2/5"               # 6 4 5 7 3
    assert over["l10"] == "7/10" and over["l20"].endswith("/20") and over["h2h"] == "1/2"
    assert over["l10_values"] == [6, 4, 5, 7, 3, 5, 6, 2, 8, 5]
    assert over["min_l10"] == pytest.approx(sum(30.0 + i for i in range(10)) / 10)


def test_rows_carry_context_and_market_label():
    rows, _ = rows_for(prop([5] * 10, sleeper=3.5, consensus=3.5, nv=0.7, kalshi=[(3.5, 0.75, 0.27)]))
    d = card.to_dict(rows, 0, slate=str(D), stats={})
    by = {leg["platform"]: leg for leg in d["legs"]}
    assert by["sleeper"]["market"] == "REB over 3.5" and by["kalshi"]["market"] == "REB 4+"
    for leg in d["legs"]:
        assert {"l5", "l10", "l20", "season", "h2h", "l10_values", "min_l10", "team", "opp", "p_raw", "edge"} <= set(leg)
        assert leg["l10"] == "10/10" and leg["min_l10"] == 30.0


def test_ladder_lists_every_strike_with_calibrated_p():
    pr = prop([5] * 10, kalshi=[(2.5, 0.9, 0.12), (4.5, 0.6, 0.42), (6.5, 0.2, 0.82)])
    _, stats = card.build_rows([pr], before=D, season_start=SZN, h2h_since=H2H, cal=Calibrator(ART))
    (lad,) = stats["ladders"]
    assert [s["strike"] for s in lad["strikes"]] == [3, 5, 7]
    s5 = lad["strikes"][1]
    assert s5["mid"] == pytest.approx(0.59) and s5["l10"] == "10/10"
    assert s5["p_raw_yes"] == pytest.approx(0.35 * 10.5 / 11 + 0.65 * 0.59, abs=1e-4)
    assert s5["p_cal_yes"] == pytest.approx(Calibrator(ART)("kalshi", "reb", s5["p_raw_yes"], 0.59), abs=1e-4)
    missing = Calibrator(None)
    _, st2 = card.build_rows([pr], before=D, season_start=SZN, h2h_since=H2H, cal=missing)
    assert all(s["p_cal_yes"] is None for s in st2["ladders"][0]["strikes"])   # fail closed: no calibrated p


def test_excluded_props_get_no_ladder():
    pr = prop([5] * 10, kalshi=[(4.5, 0.6, 0.42)], injury={"code": "OUT", "status": "Out", "source": "official"})
    _, stats = rows_for(pr)
    assert stats["ladders"] == []
