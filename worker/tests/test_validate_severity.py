"""validate severity (green / YELLOW / red) and next-night ESPN rechecks with bet regrading.

Fixture: LAC@SA on 2026-11-19 (ESPN 401910022). The saved ESPN summary is the one ESPN served on
11-20, which has Kris Dunn at 1 block. The 6am ingest on 11-20 had stored 2 (ESPN 2 vs PropLine 1),
so `_espn(dunn_blk=2)` rebuilds that earlier box score. The PropLine payload is built from the same
box score in PropLine's /events/{id}/stats shape, so every other player agrees.
"""

from datetime import datetime

import pytest

from jobkit import Net, Resp, load, run as _run, scoreboards, seed_players
from prop_worker import bets, config
from prop_worker.jobs import boxscores, schedule, validate
from prop_worker.runner import run_all

NIGHTLY = datetime(2026, 11, 20, 6, 5, tzinfo=config.ET)        # validates 11-19
NEXT_NIGHTLY = datetime(2026, 11, 21, 6, 5, tzinfo=config.ET)   # validates 11-20 (no games here), rechecks 11-19
EVENT, PL_EVENT = "401910022", "pl-1119-lac-sa"
ESPN_TEAM = {"LAC": "LAC", "SA": "SAS"}
TO_PROPLINE = {ours: theirs for theirs, ours in validate.PROPLINE_STATS.items()}
ALL = 100                                                       # compare every player, not a sample of 10


def _espn(**dunn):
    """The ESPN summary, with Kris Dunn's stats overridden (e.g. blk=2)."""
    s = load(f"espn/summary_{EVENT}.json")
    keys = {"blk": "blocks", "stl": "steals", "tov": "turnovers", "reb": "rebounds", "dreb": "defensiveRebounds"}
    for t in s["boxscore"]["players"]:
        st = t["statistics"][0]
        for a in st["athletes"]:
            if a["athlete"]["displayName"] == "Kris Dunn":
                for stat, v in dunn.items():
                    a["stats"][st["keys"].index(keys[stat])] = str(v)
    return s


def _propline(summary=None, edits=()):
    """PropLine /events/{id}/stats from an ESPN summary; edits = [(player, stat, delta)]."""
    rows, _ = boxscores.parse_summary(summary or _espn(), {"LAC": "LAC", "SA": "SAS"})
    delta = {(n, s): d for n, s, d in edits}
    stats = []
    for r in rows:
        if r["did_not_play"]:
            continue
        for ours, theirs in TO_PROPLINE.items():
            v = float(r[ours]) + delta.get((r["name"], ours), 0)
            stats.append({"player_name": r["name"], "team_abbr": r["team_abbr"], "stat_type": theirs, "stat_value": v})
    return {"id": PL_EVENT, "status": "final", "stats": stats}


def _net(summary=None, stats=None):
    return Net([scoreboards({"20261119": load("espn/scoreboard_20261119.json")}),
                (lambda u, p: u.endswith("/summary"), lambda u, p: Resp(summary or _espn())),
                (lambda u, p: u.endswith("/scores"), Resp([{"id": PL_EVENT, "commence_time": "2026-11-20T01:00:00Z",
                                                            "home_team_id": "nba:1610612759", "status": "final"}])),
                (lambda u, p: u.endswith(f"/events/{PL_EVENT}/stats"), Resp(stats or _propline()))])


def _ingest(pg, summary):
    """Load 11-19 as the 6am nightly on 11-20 would: schedule, then the ESPN box score."""
    s = load(f"espn/summary_{EVENT}.json")
    seed_players(pg, [(8_000_000 + 100 * k + i, a["athlete"]["displayName"], ESPN_TEAM[t["team"]["abbreviation"]])
                      for k, t in enumerate(s["boxscore"]["players"])
                      for i, a in enumerate(t["statistics"][0]["athletes"])])
    net = _net(summary)
    _run(pg, net, "schedule", schedule.run, now=NIGHTLY)
    out = _run(pg, net, "boxscores", boxscores.run, now=NIGHTLY)
    assert out.status == "green", out.red_reasons


def _validate(pg, stats=None, summary=None, now=NIGHTLY):
    return _run(pg, _net(summary, stats), "validate", validate.run, now=now, propline_key="k", players=ALL)


def _stored(pg, name, col):
    return pg.execute(f"""select l.{col} from game_logs l join players p using (player_id)
                          where p.full_name = %s""", (name,)).fetchone()[0]


def _pid(pg, name):
    return pg.execute("select player_id from players where full_name = %s", (name,)).fetchone()[0]


# ── severity ─────────────────────────────────────────────────────────────────

def test_kris_dunn_blk_2_vs_1_is_yellow_and_exits_zero(pg):
    _ingest(pg, _espn(blk=2))
    assert _stored(pg, "Kris Dunn", "blk") == 2
    sent = []
    clock = iter([0.0, 5.0])
    code, outs = run_all([("validate", validate.run)], conn=pg, session=_net(), now=NIGHTLY,
                         options={"propline_key": "k", "players": ALL},
                         notifier=lambda *a: sent.append(a) or "sent", clock=lambda: next(clock))
    out = outs[0]
    assert code == 0 and sent == []                              # not failed, no alert, exit 0
    assert out.status == "green" and out.red_reasons == []
    assert out.meta["mismatch_severity"] == "yellow"
    assert out.warnings == ["YELLOW: 1 mismatch(es) vs PropLine on 2026-11-19: "
                            "Kris Dunn (LAC): blk ESPN 2 vs PropLine 1.0"]
    stored = pg.execute("""select warnings, status from job_runs where job = 'validate'""").fetchone()
    assert stored == (out.warnings, "green")                     # logged in job_runs
    assert out.rows_in == out.rows_out + 1                       # every other player matched


def test_everything_matching_is_green(pg):
    _ingest(pg, _espn())
    out = _validate(pg)
    assert out.status == "green" and out.warnings == [] and out.meta["mismatch_severity"] == "green"


@pytest.mark.parametrize("stat", ["stl", "tov", "blk"])
def test_single_minor_stat_off_by_one_is_yellow(pg, stat):
    _ingest(pg, _espn())
    out = _validate(pg, _propline(edits=[("Kris Dunn", stat, -1)]))
    assert out.status == "green" and out.warnings[0].startswith("YELLOW: 1 mismatch(es)")


@pytest.mark.parametrize("stat", ["pts", "reb", "ast", "fg3m"])
def test_headline_stat_off_by_one_is_red(pg, stat):
    _ingest(pg, _espn())
    out = _validate(pg, _propline(edits=[("Kris Dunn", stat, 1)]))
    assert out.status == "red" and out.meta["mismatch_severity"] == "red"
    assert out.red_reasons[0].startswith("1 mismatch(es) vs PropLine on 2026-11-19")
    assert f"Kris Dunn (LAC): {stat} ESPN" in out.red_reasons[0]


def test_other_exact_column_off_by_one_is_still_red(pg):
    _ingest(pg, _espn())
    out = _validate(pg, _propline(edits=[("Kris Dunn", "fga", 1)]))
    assert out.status == "red"


@pytest.mark.parametrize("stat", ["blk", "stl", "tov"])
def test_minor_stat_off_by_two_is_red(pg, stat):
    _ingest(pg, _espn())
    out = _validate(pg, _propline(edits=[("Kris Dunn", stat, -2)]))
    assert out.status == "red" and f"{stat} ESPN" in out.red_reasons[0]


def _others(pg, n):
    return [r[0] for r in pg.execute(
        """select p.full_name from game_logs l join players p using (player_id)
           where not l.did_not_play and p.full_name <> 'Kris Dunn' order by p.full_name limit %s""", (n,))]


def test_two_minor_mismatches_on_a_date_stay_yellow(pg):
    _ingest(pg, _espn(blk=2))                                    # Dunn blk, plus one more
    other = _others(pg, 1)[0]
    out = _validate(pg, _propline(edits=[(other, "stl", 1)]))
    assert out.status == "green" and out.warnings[0].startswith("YELLOW: 2 mismatch(es)")


def test_more_than_two_mismatches_on_a_date_is_red(pg):
    _ingest(pg, _espn(blk=2))                                    # Dunn blk, plus two more, all off by 1
    a, b = _others(pg, 2)
    out = _validate(pg, _propline(edits=[(a, "stl", 1), (b, "tov", -1)]))
    assert out.status == "red" and out.red_reasons[0].startswith("3 mismatch(es) vs PropLine on 2026-11-19")
    assert out.meta["mismatch_severity"] == "red"


def test_player_missing_from_propline_is_still_red(pg):
    _ingest(pg, _espn())
    stats = _propline()
    stats["stats"] = [r for r in stats["stats"] if r["player_name"] != "Kris Dunn"]
    out = _validate(pg, stats)
    assert out.status == "red" and any("Kris Dunn (LAC): not found in PropLine" in m for m in out.meta["mismatches"])


def test_minutes_off_by_more_than_one_is_red(pg):
    _ingest(pg, _espn())
    out = _validate(pg, _propline(edits=[("Kris Dunn", "min", 3)]))
    assert out.status == "red" and "minutes ESPN" in out.red_reasons[0]


# ── rechecks ─────────────────────────────────────────────────────────────────

def _recheck(pg, summary):
    """The next nightly: no games on 11-20, ESPN box score for 11-19 re-fetched."""
    return _run(pg, _net(summary), "validate", validate.run, now=NEXT_NIGHTLY, propline_key="k", players=ALL)


def test_kris_dunn_blk_corrected_by_espn_next_night_is_updated(pg):
    _ingest(pg, _espn(blk=2))
    first = _validate(pg)
    assert first.meta["rechecks_queued"] == 1
    assert pg.execute("""select stat, stored_value, other_value, severity, found_on::text, checked_at
                         from validate_rechecks""").fetchall() == [("blk", 2, 1, "yellow", "2026-11-19", None)]
    net = _net(_espn())                                          # ESPN's 11-20 box score: Dunn 1 block
    out = _run(pg, net, "validate", validate.run, now=NEXT_NIGHTLY, propline_key="k", players=ALL)
    assert out.status == "green" and out.meta["date"] == "2026-11-20"
    assert any(c[0].endswith("/summary") and c[1] == {"event": EVENT} for c in net.calls)
    assert _stored(pg, "Kris Dunn", "blk") == 1
    assert out.meta["rechecks"] == [{"player": "Kris Dunn", "stat": "blk", "date": "2026-11-19", "stored": 2,
                                     "espn_now": 1, "outcome": "updated", "regraded": []}]
    assert out.warnings == ["recheck: Kris Dunn blk on 2026-11-19 ESPN corrected 2 -> 1; 0 bet(s) regraded"]
    assert pg.execute("select outcome, espn_value, bets_regraded from validate_rechecks").fetchone() == ("updated", 1, 0)
    again = _recheck(pg, _espn())                                # checked once, not again
    assert again.meta["rechecks"] == []


def test_espn_unchanged_next_night_leaves_the_stat(pg):
    _ingest(pg, _espn(blk=2))
    _validate(pg)
    out = _recheck(pg, _espn(blk=2))
    assert out.meta["rechecks"][0]["outcome"] == "unchanged" and out.warnings == []
    assert _stored(pg, "Kris Dunn", "blk") == 2


def test_not_rechecked_on_the_same_night(pg):
    _ingest(pg, _espn(blk=2))
    _validate(pg)
    rerun = _validate(pg, summary=_espn())                       # same-day re-run validates 11-19 again
    assert rerun.meta["rechecks"] == [] and _stored(pg, "Kris Dunn", "blk") == 2


def test_espn_fetch_failure_leaves_the_recheck_open(pg):
    _ingest(pg, _espn(blk=2))
    _validate(pg)
    net = _net().add(lambda u, p: u.endswith("/summary"), Resp(status=503))
    out = _run(pg, net, "validate", validate.run, now=NEXT_NIGHTLY, propline_key="k", players=ALL)
    assert out.status == "green" and "fetch failed" in out.warnings[0]
    assert pg.execute("select checked_at from validate_rechecks").fetchone()[0] is None
    assert _recheck(pg, _espn()).meta["rechecks"][0]["outcome"] == "updated"


def test_reb_correction_regrades_bets_and_legs_on_that_player_stat_date(pg):
    _ingest(pg, _espn(reb=3, dreb=3))                            # ingested 3 rebounds; ESPN later says 2
    pid = _pid(pg, "Kris Dunn")
    d = datetime(2026, 11, 19).date()
    reb_over = bets.add_bet(pg, slate_date=d, player_id=pid, stat="reb", side="over", line=2.5, source="own",
                            platform="sleeper", card_id="slp-20261119-1")       # a card leg
    reb_yes = bets.add_bet(pg, slate_date=d, player_id=pid, stat="reb", side="yes", line=2.5, source="own",
                           platform="kalshi", price_cents=55)
    ra_over = bets.add_bet(pg, slate_date=d, player_id=pid, stat="ra", side="over", line=4.5, source="own")
    pts_over = bets.add_bet(pg, slate_date=d, player_id=pid, stat="pts", side="over", line=5.5, source="own")
    by_hand = bets.add_bet(pg, slate_date=d, player_id=pid, stat="reb", side="under", line=2.5, source="own")
    bets.grade_pending(pg, d, now=NIGHTLY)
    pg.execute("update bets set result = 'void', note = 'voided by hand' where id = %s", (by_hand,))
    pg.commit()
    result = lambda b: pg.execute("select result, actual_value from bets where id = %s", (b,)).fetchone()  # noqa: E731
    assert result(reb_over) == ("win", 3) and result(ra_over) == ("win", 5)

    first = _validate(pg)                                        # PropLine 2 vs ESPN 3: red, queued
    assert first.status == "red" and first.meta["rechecks_queued"] >= 1

    out = _recheck(pg, _espn())                                  # ESPN now 2
    assert _stored(pg, "Kris Dunn", "reb") == 2
    assert result(reb_over) == ("loss", 2) and result(reb_yes) == ("loss", 2) and result(ra_over) == ("loss", 4)
    assert result(pts_over) == ("win", 6)                        # pts untouched
    assert result(by_hand) == ("void", 3)                        # hand-voided untouched
    reb = next(r for r in out.meta["rechecks"] if r["stat"] == "reb")
    assert sorted((g["bet"], g["was"], g["now"]) for g in reb["regraded"]) == \
        sorted([(reb_over, "win", "loss"), (reb_yes, "win", "loss"), (ra_over, "win", "loss")])
    note = pg.execute("select note from bets where id = %s", (reb_over,)).fetchone()[0]
    assert note == "regraded 2026-11-21: ESPN corrected reb 3 -> 2"
    assert any("reb on 2026-11-19 ESPN corrected 3 -> 2; 3 bet(s) regraded" in w for w in out.warnings)
