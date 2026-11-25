"""Calibrated card rules (AUDIT_2026-11-24.md item 4, DECISIONS.md D9-D13)."""

import json
import math
from datetime import date, datetime, timedelta, timezone

import pytest

from prop_worker import card
from prop_worker.calibration import Calibrator, apply
from test_card import prop, rows_for, D, SZN, H2H

ART = {"fit_date": "2026-11-25T00:00:00+00:00",
       "kalshi": {"maps": {"_pooled": {"method": "logit2", "b0": -0.174, "b_mid": 0.877, "b_dev": 1.045,
                                       "w_hist": 0.35, "n": 9147},
                           "reb": {"method": "pooled", "n": 2110}},
                  "dates": ["2026-11-11", "2026-11-23"], "n_region": 9147},
       "sleeper": {"method": "offset", "offset": -0.05, "n": 75}}


def test_logit2_matches_formula_and_is_symmetric():
    c = Calibrator(ART)
    z = -0.174 + 0.877 * math.log(0.7 / 0.3) + 1.045 * (0.8 - 0.7) / 0.35
    assert c("kalshi", "reb", 0.8, 0.7) == pytest.approx(1 / (1 + math.exp(-z)))
    assert c("kalshi", "reb", 0.2, 0.3) == pytest.approx(1 - c("kalshi", "reb", 0.8, 0.7))
    with pytest.raises(ValueError):
        apply(ART["kalshi"]["maps"]["_pooled"], 0.8)                  # logit2 needs the mid


def test_isotonic_and_offset_and_identity():
    assert apply({"method": "isotonic", "x": [0.5, 0.9], "y": [0.5, 0.7]}, 0.7) == pytest.approx(0.6)
    assert apply({"method": "isotonic", "x": [0.5, 0.9], "y": [0.5, 0.7]}, 0.99) == pytest.approx(0.7)
    assert Calibrator(ART)("sleeper", "pts", 0.70) == pytest.approx(0.65)
    assert Calibrator(None)("kalshi", "pts", 0.8, 0.7) == 0.8 and not Calibrator(None).ok


def test_missing_artifact_is_identity_and_says_so(tmp_path):
    c = Calibrator.load(tmp_path / "nope.json")
    assert not c.ok and c.label().startswith("UNCALIBRATED")
    p = tmp_path / "a.json"
    p.write_text(json.dumps(ART))
    assert Calibrator.load(p).ok and "Kalshi replay 2026-11-11..2026-11-23" in Calibrator.load(p).label()


def test_main_package_and_worker_agree():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.models.calibration import apply_map
    m = ART["kalshi"]["maps"]["_pooled"]
    for p, mid in ((0.8, 0.7), (0.69, 0.6), (0.95, 0.9)):
        assert float(apply_map(m, [p], [mid])[0]) == pytest.approx(apply(m, p, mid))


def test_kalshi_rules_run_on_calibrated_p():
    """Raw rules would show this leg (p .815 vs 75c ask); calibrated p falls below ask + 5c -> hidden."""
    pr = prop([10] * 10, kalshi=[(8.5, 0.75, 0.27)])               # L10 10/10 over 8.5, mid .74
    raw, _ = rows_for(pr)
    assert len(raw) == 1 and raw[0].p == raw[0].p_raw              # identity: p == raw
    cal_rows, _ = card.build_rows([pr], before=D, season_start=SZN, h2h_since=H2H, cal=Calibrator(ART))
    assert cal_rows == []


def test_kalshi_row_carries_raw_cal_edge_mid():
    pr = prop([10] * 10, kalshi=[(8.5, 0.70, 0.10)])                # wide spread: mid .80, YES ask 70c
    rows, _ = card.build_rows([pr], before=D, season_start=SZN, h2h_since=H2H, cal=Calibrator(ART))
    (r,) = rows
    assert r.side == "YES" and r.p_raw == pytest.approx(0.35 * (10.5 / 11) + 0.65 * 0.80)
    assert r.p == pytest.approx(Calibrator(ART)("kalshi", "reb", r.p_raw, 0.80)) and r.p < r.p_raw
    assert r.edge == pytest.approx(r.p - 0.70) and r.mid == pytest.approx(0.80)
    assert r.max_price == round(r.p - 0.05, 2)
    d = card.to_dict(rows, 0, slate=str(D), stats={}, calibration="x")
    assert {"p", "p_raw", "edge", "mid"} <= set(d["legs"][0]) and d["calibration"] == "x"


def test_sleeper_threshold_uses_calibrated_p():
    # raw p just above .62; the -.05 Sleeper offset pushes it below -> dropped
    pr = prop([5, 5, 5, 5, 5, 5, 5, 3, 3, 3], sleeper=4.5, consensus=4.5, nv=0.6)
    raw, _ = rows_for(pr)
    assert raw and raw[0].p >= 0.62
    cal_rows, _ = card.build_rows([pr], before=D, season_start=SZN, h2h_since=H2H, cal=Calibrator(ART))
    assert all(r.p >= 0.62 for r in cal_rows) and all(r.p == pytest.approx(r.p_raw - 0.05) for r in cal_rows)
    assert len(cal_rows) <= len(raw)


def test_ledger_note_keeps_raw_p_and_mid():
    from prop_worker.bets import card_note
    r = card.Row(None, "g", "x", "reb", "kalshi", "YES", 8.5, 0.66, 0.6, 0.61, ["on the line"], p_raw=0.8443, mid=0.59)
    assert card_note(r) == "on the line, p_raw=0.8443, mid=0.5900"
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.models.calibration import _note_float
    assert _note_float(card_note(r), "p_raw=") == 0.8443 and _note_float(card_note(r), "mid=") == 0.59
