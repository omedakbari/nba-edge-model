"""Regression fixture for the 2026-11-24 audit (AUDIT_2026-11-24.md): a Day-To-Day star that the official
report calls Probable, an Out player, a diacritic name the injury feed never mapped, and a player whose
market sits in the wrong game. Each must be excluded with a named reason; a healthy player must survive."""

import json
from datetime import timedelta

import pytest

from jobkit import seed_players
from prop_worker import card, slate
from prop_worker.util import WARN_ENV
from test_slate import D, SEEN, TIP, game, injury, kalshi, logs, pid


@pytest.fixture(autouse=True)
def raw_calibration(monkeypatch):
    """These tests are about exclusions; pin calibration to identity so a fitted artifact can't change them."""
    from prop_worker.calibration import Calibrator
    monkeypatch.setattr(Calibrator, "load", classmethod(lambda cls, path=None: cls.identity()))


@pytest.fixture
def nov24(pg):
    seed_players(pg, [(1, "Cade Cunningham", "DET"), (2, "Jalen Duren", "DET"), (3, "Isaiah Joe", "DET"),
                      (4, "Nikola Vučević", "ORL"), (5, "Franz Wagner", "ORL")])
    ids = {n: pid(pg, n) for n in ("Cade Cunningham", "Jalen Duren", "Isaiah Joe", "Nikola Vučević",
                                   "Franz Wagner")}
    det_orl = game(pg, D, "ORL", "DET", TIP)
    bos_was = game(pg, D, "WAS", "BOS", TIP + timedelta(hours=1))
    for name, team, opp in (("Cade Cunningham", "DET", "ORL"), ("Jalen Duren", "DET", "ORL"),
                            ("Isaiah Joe", "DET", "ORL"), ("Nikola Vučević", "ORL", "DET"),
                            ("Franz Wagner", "ORL", "DET")):
        logs(pg, ids[name], team, [(2 * i + 2, "MIA", 30) for i in range(10)])
        gid = bos_was if name == "Isaiah Joe" else det_orl          # Joe's market is in the wrong game
        kalshi(pg, gid, ids[name], 19.5, 0.75, 0.27)                 # clears every Kalshi rule when healthy
    injury(pg, ids["Cade Cunningham"], "Cade Cunningham", "DET", "Probable")                   # official
    injury(pg, ids["Cade Cunningham"], "Cade Cunningham", "DET", "Day-To-Day", source="espn")  # stricter
    injury(pg, ids["Jalen Duren"], "Jalen Duren", "DET", "Out")
    injury(pg, None, "Nikola Vucevic", "ORL", "Questionable")        # report spelling, never mapped to an id
    pg.commit()
    return ids


def test_build_flags_each_issue(nov24, pg):
    props, info = slate.build(pg, D)
    by = {p.name: p for p in props}
    assert by["Cade Cunningham"].injury["code"] == "GTD" and by["Cade Cunningham"].injury["source"] == "espn"
    assert by["Jalen Duren"].injury["code"] == "OUT"
    assert by["Nikola Vučević"].injury["code"] == "Q" and by["Nikola Vučević"].injury["matched_by"] == "name"
    assert by["Isaiah Joe"].mismatch == "team mismatch: roster DET, last game log DET, game BOS@WAS"
    assert by["Franz Wagner"].injury is None and by["Franz Wagner"].mismatch is None
    assert info["injury"]["official_as_of"] == SEEN.isoformat() and info["injury"]["unmatched"] == []


def test_card_names_every_exclusion_and_warns(nov24, pg, tmp_path, monkeypatch, capsys):
    warn_file = tmp_path / "warn.jsonl"
    monkeypatch.setenv(WARN_ENV, str(warn_file))
    out = tmp_path / "card.txt"
    assert slate.main(["--card", "--date", D.isoformat(), "--out", str(out)], conn=pg) == 0
    data = json.loads(out.with_suffix(".json").read_text())
    assert [leg["player"] for leg in data["legs"]] == ["Franz Wagner"]
    assert data["legs"][0]["team"] == "ORL" and data["legs"][0]["opp"] == "DET"
    reasons = {e["player"]: (e["reason"], e["detail"]) for e in data["excluded"]}
    assert reasons["Cade Cunningham"][0] == "injury" and "GTD" in reasons["Cade Cunningham"][1]
    assert "official Probable" in reasons["Cade Cunningham"][1]              # the other source is shown too
    assert reasons["Jalen Duren"][0] == "injury" and reasons["Jalen Duren"][1].startswith("OUT")
    assert reasons["Nikola Vučević"][0] == "injury"
    assert reasons["Isaiah Joe"] == ("team mismatch", "team mismatch: roster DET, last game log DET, game BOS@WAS")
    assert data["stats"] == {"excluded_injured": 3, "started": 0, "stale": 0, "slp_hard_hidden": 0,
                             "team_mismatch": 1}
    text = out.read_text()
    assert "excluded injury: Jalen Duren" in text and "excluded team mismatch: Isaiah Joe" in text
    warns = [json.loads(line) for line in warn_file.read_text().splitlines()]
    assert warns == [{"step": "Prop card", "reason": "card: 1 prop(s) excluded for team mismatch"}]
    pg.rollback()
    pg.read_only = False


def test_injury_data_older_than_max_age_is_stale(nov24, pg):
    _, fresh = slate.build(pg, D, now=SEEN + timedelta(hours=2))
    _, old = slate.build(pg, D, now=SEEN + timedelta(hours=3, minutes=1))
    assert fresh["injury"]["stale"] is False and old["injury"]["stale"] is True
    assert any("STALE" in w for w in slate.card_warnings(old, [object()], {}))


def test_unmatched_name_with_no_prop_is_listed(nov24, pg, tmp_path):
    injury(pg, None, "Ebuka Okorie", "DET", "Out")
    pg.commit()
    _, info = slate.build(pg, D)
    assert [u["name"] for u in info["injury"]["unmatched"]] == ["Ebuka Okorie"]
    out = tmp_path / "card.txt"
    assert slate.main(["--card", "--date", D.isoformat(), "--out", str(out)], conn=pg) == 0
    ex = json.loads(out.with_suffix(".json").read_text())["excluded"]
    assert {"player": "Ebuka Okorie", "reason": "injury name not matched"}.items() <= next(
        e for e in ex if e["player"] == "Ebuka Okorie").items()
    pg.rollback()
    pg.read_only = False


def test_stale_quote_exclusion_reports_age():
    from test_card import prop, rows_for, SEEN as CSEEN
    fresh = prop([5] * 10, kalshi=[(3.5, 0.75, 0.27)], name="A")
    old = prop([5] * 10, kalshi=[(3.5, 0.75, 0.27)], name="B", seen=CSEEN - timedelta(minutes=50))
    _, stats = rows_for(fresh, old)
    assert stats["excluded"] == [{"player": "B", "player_id": 1, "team": "PHX", "game": "LAL@PHX", "stat": "REB",
                                  "venue": "Kalshi", "line": "4+", "reason": "stale quote",
                                  "detail": "quote 50 min older than newest"}]
    assert card.STALE_MIN == 45


def test_two_way_unmatched_name_gets_its_own_reason(nov24, pg, tmp_path):
    pg.execute("""insert into injuries (source, player_key, player_id, external_name, team_abbr, game_date, status,
                                        reason, fetched_at, last_seen_at)
                  values ('nba_pdf', 'DET:isaac jones', null, 'Isaac Jones', 'DET', %s, 'Out',
                          'G League - Two-Way', %s, %s)""", (D, SEEN, SEEN))
    pg.commit()
    out = tmp_path / "card.txt"
    assert slate.main(["--card", "--date", D.isoformat(), "--out", str(out)], conn=pg) == 0
    ex = {e["player"]: e for e in json.loads(out.with_suffix(".json").read_text())["excluded"]}
    assert ex["Isaac Jones"]["reason"] == "not in roster ingest — two-way"
    assert "unprotected" not in ex["Isaac Jones"]["detail"] and "G League - Two-Way" in ex["Isaac Jones"]["detail"]
    pg.rollback()
    pg.read_only = False
