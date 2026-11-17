"""bets: ledger writes, nightly grading from game_logs, void rules, Sleeper card aggregation, report."""

from datetime import date, datetime, timezone

import pytest

from jobkit import Net, run, seed_players
from prop_worker import bets, card, config
from prop_worker.jobs import bets_grade

D = date(2026, 11, 16)
SIX_AM = datetime(2026, 11, 17, 6, 5, tzinfo=config.ET)


def pid(pg, name):
    return pg.execute("select player_id from players where full_name = %s", (name,)).fetchone()[0]


def game(pg, home, away, status="final", d=D):
    return pg.execute("""insert into games (game_date, home_team, away_team, status, source)
                         values (%s, %s, %s, %s, 'espn') returning game_id""", (d, home, away, status)).fetchone()[0]


def box(pg, gid, player, team, opp, *, pts=10, reb=4, ast=3, fg3m=1, mins=30, dnp=False):
    pg.execute("""insert into game_logs (game_id, player_id, team_abbr, opp_abbr, is_home, min, did_not_play,
                                         pts, reb, ast, fg3m, source)
                  values (%s, %s, %s, %s, true, %s, %s, %s, %s, %s, %s, 'espn')""",
               (gid, player, team, opp, mins, dnp, pts, reb, ast, fg3m))


@pytest.fixture
def world(pg):
    seed_players(pg, [(1, "Devin Booker", "PHX"), (2, "De'Anthony Melton", "GSW"), (3, "Nikola Jokić", "DEN"),
                      (4, "Jayson Tatum", "BOS"), (5, "Reed Sheppard", "HOU"), (6, "Bench Guy", "PHX"),
                      (7, "Not Tonight", "DEN")])
    g1 = game(pg, "PHX", "GSW")
    g2 = game(pg, "DEN", "BOS")
    game(pg, "HOU", "LAL", status="in_progress")
    box(pg, g1, pid(pg, "Devin Booker"), "PHX", "GSW", reb=4, pts=31, ast=6)
    box(pg, g1, pid(pg, "De'Anthony Melton"), "GSW", "PHX", reb=2)
    box(pg, g1, pid(pg, "Bench Guy"), "PHX", "GSW", mins=0, reb=0)
    box(pg, g2, pid(pg, "Nikola Jokić"), "DEN", "BOS", pts=25, reb=12, ast=9)
    box(pg, g2, pid(pg, "Jayson Tatum"), "BOS", "DEN", dnp=True, mins=None, reb=None, pts=None, ast=None, fg3m=None)
    pg.commit()
    return pg


def add(pg, name, **kw):
    kw.setdefault("source", "claude")
    kw.setdefault("slate_date", D)
    return bets.add_bet(pg, player_id=pid(pg, name), **kw)


def result(pg, bid):
    return pg.execute("select result, actual_value from bets where id = %s", (bid,)).fetchone()


# ── grading ─────────────────────────────────────────────────────────────────

def test_outcome_rules():
    assert bets.outcome(4, "no", 3.5) == "loss"           # Booker 4+ REB NO, 4 rebounds
    assert bets.outcome(3, "no", 3.5) == "win"
    assert bets.outcome(4, "yes", 3.5) == "win"
    assert bets.outcome(2, "over", 3.5) == "loss"         # Melton O3.5, 2 rebounds
    assert bets.outcome(2, "under", 2.5) == "win"
    assert bets.outcome(4, "over", 4.0) == "push"


def test_parse_and_show_kalshi_strikes():
    assert bets.parse_line("4+") == 3.5 and bets.parse_line("O3.5") == 3.5 and bets.parse_line("10") == 10
    assert bets.show_line(3.5, "no") == "4+" and bets.show_line(3.5, "over") == "3.5"


def test_grading_from_box_scores(world):
    pg = world
    booker = add(pg, "Devin Booker", stat="reb", side="no", line=3.5, platform="kalshi", price_cents=62, placed=True)
    melton = add(pg, "De'Anthony Melton", stat="reb", side="over", line=3.5, placed=True)       # platform unknown
    jokic = add(pg, "Nikola Jokić", stat="ast", side="yes", line=7.5, platform="kalshi", price_cents=75)
    pra = add(pg, "Nikola Jokić", stat="pra", side="under", line=45.5, platform="sleeper")
    push = add(pg, "Devin Booker", stat="ast", side="over", line=6, platform="sleeper", source="own")
    counts = bets.grade_pending(pg, D)
    assert result(pg, booker) == ("loss", 4)
    assert result(pg, melton) == ("loss", 2)
    assert result(pg, jokic) == ("win", 9)
    assert result(pg, pra) == ("loss", 46)               # 25 + 12 + 9 = 46 > 45.5
    assert result(pg, push) == ("push", 6)
    assert counts == {"loss": 3, "win": 1, "push": 1}


def test_void_rules_dnp_zero_minutes_absent_and_pending_until_final(world):
    pg = world
    dnp = add(pg, "Jayson Tatum", stat="reb", side="under", line=7.5, platform="sleeper")
    zero = add(pg, "Bench Guy", stat="reb", side="over", line=0.5, platform="sleeper")
    absent = add(pg, "Not Tonight", stat="pts", side="over", line=9.5, platform="sleeper")   # DEN final, no row
    live = add(pg, "Reed Sheppard", stat="reb", side="under", line=2.5, platform="sleeper")  # HOU game live
    counts = bets.grade_pending(pg, D)
    assert result(pg, dnp)[0] == result(pg, zero)[0] == result(pg, absent)[0] == "void"
    assert result(pg, live) == (None, None) and counts["pending"] == 1
    pg.execute("update games set status = 'final' where home_team = 'HOU'")
    gid = pg.execute("select game_id from games where home_team = 'HOU'").fetchone()[0]
    box(pg, gid, pid(pg, "Reed Sheppard"), "HOU", "LAL", reb=1)
    assert bets.grade_pending(pg, D) == {"win": 1}       # retried the next night


def test_hand_void_and_future_bets_untouched(world):
    pg = world
    b = add(pg, "Devin Booker", stat="pts", side="over", line=25.5, platform="sleeper")
    later = add(pg, "Devin Booker", stat="pts", side="over", line=25.5, platform="sleeper", slate_date=date(2026, 11, 18))
    assert bets.set_void(pg, b) == 1 and result(pg, b)[0] == "void"
    bets.grade_pending(pg, D)
    assert result(pg, later) == (None, None)


def test_nightly_job_grades_yesterday_and_is_green_with_no_bets(world):
    pg = world
    b = add(pg, "Devin Booker", stat="reb", side="no", line=3.5, platform="kalshi", price_cents=62)
    pg.commit()
    out = run(pg, Net(), "bets", bets_grade.run, now=SIX_AM)
    assert out.status == "green" and out.meta["loss"] == 1 and out.meta["through"] == "2026-11-16"
    assert result(pg, b)[0] == "loss"
    again = run(pg, Net(), "bets", bets_grade.run, now=SIX_AM)
    assert again.status == "green" and again.rows_out == 0


# ── cards ───────────────────────────────────────────────────────────────────

def _g(result, card_id="c1", **kw):
    base = dict(id=1, slate_date=D, platform="sleeper", card_id=card_id, name="x", stat="reb", side="over",
                line=3.5, price_cents=None, payout_mult=3.0, stake=5.0, p=0.7, source="own", placed=True,
                result=result, actual=None)
    base.update(kw)
    return bets.Graded(**base)


@pytest.mark.parametrize("legs, expected, ur", [
    (["win", "win"], "win", 2.0),
    (["win", "loss"], "loss", -1.0),
    (["win", "void"], "win", 2.0),
    (["void", "push"], "void", None),
    (["win", None], None, None),
    (["loss", None], "loss", -1.0),          # already lost, no need to wait
])
def test_sleeper_card_wins_only_if_all_legs_win(legs, expected, ur):
    (c,) = bets.card_results([_g(r) for r in legs])
    assert c["result"] == expected and c["unit_return"] == ur and c["legs"] == len(legs)


def test_card_legs_get_no_single_bet_roi_and_unknown_stake_card_has_none():
    assert bets.unit_return(_g("win")) is None
    (c,) = bets.card_results([_g("win", stake=None), _g("win", stake=None)])
    assert c["result"] == "win" and c["unit_return"] is None


def test_group_card_cli(world):
    pg = world
    a = add(pg, "Devin Booker", stat="reb", side="under", line=4.5, platform="sleeper", source="card")
    b = add(pg, "Nikola Jokić", stat="ast", side="over", line=7.5, platform="sleeper", source="card")
    pg.commit()
    assert bets.main(["card", str(a), str(b), "--stake", "5", "--mult", "3"], conn=pg) == 0
    rows = pg.execute("select card_id, placed, stake, payout_mult from bets where id = any(%s)", ([a, b],)).fetchall()
    assert len({r[0] for r in rows}) == 1 and all(r[1] and float(r[2]) == 5 and float(r[3]) == 3 for r in rows)
    bets.grade_pending(pg, D)
    (c,) = bets.card_results(bets.fetch(pg, D))
    assert c["result"] == "win" and c["unit_return"] == 2.0         # Booker 4 < 4.5, Jokic 9 > 7.5


# ── report ──────────────────────────────────────────────────────────────────

def test_report_hit_rate_counts_unknown_stake_but_roi_does_not(world):
    pg = world
    add(pg, "Devin Booker", stat="reb", side="no", line=3.5, platform="kalshi", price_cents=62, placed=True)  # stake ?
    add(pg, "De'Anthony Melton", stat="reb", side="over", line=3.5, placed=True)                             # platform ?
    add(pg, "Nikola Jokić", stat="ast", side="yes", line=7.5, platform="kalshi", price_cents=75, p=0.82, source="card")
    add(pg, "Nikola Jokić", stat="reb", side="over", line=13.5, platform="sleeper", p=0.64, source="card")
    bets.grade_pending(pg, D)
    bs = bets.fetch(pg, D)
    s = bets._summ(bs)
    assert (s["w"], s["l"]) == (1, 3) and s["hit"] == pytest.approx(0.25)
    assert s["roi_n"] == 1 and s["roi"] == pytest.approx(25 / 75)         # only the priced, recommended Kalshi win
    text = bets.report(bs, 7, date(2026, 11, 17))
    assert "recommended only" in text and "placed" in text and "unknown" in text
    assert ".80+" in text and ".60-.70" in text and "no p" in text
    assert "calibration: mean p 0.730 vs actual 0.500 over 2" in text


def test_p_buckets():
    assert [bets.p_bucket(p) for p in (None, 0.55, 0.60, 0.6999, 0.70, 0.80, 0.95)] == \
        ["no p", "<.60", ".60-.70", ".60-.70", ".70-.80", ".80+", ".80+"]


# ── card logging and CLI ────────────────────────────────────────────────────

def _legs(pg, tip):
    return [card.Row(tip, "GSW@PHX", "Devin Booker", "reb", "kalshi", "NO", 3.5, 0.71, 0.62, 0.66, ["on the line"],
                     pid(pg, "Devin Booker")),
            card.Row(tip, "GSW@PHX", "De'Anthony Melton", "reb", "sleeper", "over", 3.5, 0.64,
                     player_id=pid(pg, "De'Anthony Melton"))]


def test_every_card_leg_is_logged_once(world):
    pg = world
    tip = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)
    noon = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)
    assert bets.log_card(pg, D, _legs(pg, tip), now=noon)["new"] == 2
    assert bets.log_card(pg, D, _legs(pg, tip), now=noon)["updated"] == 2       # re-run: no duplicates
    got = pg.execute("""select platform, side, line_or_strike, price_cents, p_at_placement, source, placed, note
                        from bets order by id""").fetchall()
    assert [(g[0], g[1], float(g[2]), g[3] and float(g[3]), float(g[4]), g[5], g[6], g[7]) for g in got] == [
        ("kalshi", "no", 3.5, 62.0, 0.71, "card", False, "on the line"),
        ("sleeper", "over", 3.5, None, 0.64, "card", False, None)]


def test_relog_refreshes_until_tip_adds_new_marks_dropped_and_freezes_placed(world):
    pg = world
    tip = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)                    # 7 pm PT
    pg.execute("update games set start_time = %s", (tip,))
    noon = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)
    three = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)
    after = datetime(2026, 11, 17, 3, 30, tzinfo=timezone.utc)                # 30 min after tip
    booker, melton = _legs(pg, tip)
    bets.log_card(pg, D, [booker, melton], now=noon)
    ids = dict(pg.execute("select player_id, id from bets").fetchall())

    # 3 pm: Booker's price moved, Melton fell off the card, Jokic is new
    booker2 = card.Row(tip, "GSW@PHX", "Devin Booker", "reb", "kalshi", "NO", 3.5, 0.75, 0.66, 0.70, [],
                       pid(pg, "Devin Booker"))
    jokic = card.Row(tip, "BOS@DEN", "Nikola Jokić", "ast", "kalshi", "YES", 7.5, 0.80, 0.72, 0.75, [],
                     pid(pg, "Nikola Jokić"))
    counts = bets.log_card(pg, D, [booker2, jokic], now=three)
    assert counts == {"new": 1, "updated": 1, "frozen": 0, "started": 0, "dropped": 1}
    b = pg.execute("""select price_cents, p_at_placement, note, last_logged_at, dropped_at from bets
                      where id = %s""", (ids[pid(pg, "Devin Booker")],)).fetchone()
    assert (float(b[0]), float(b[1]), b[2], b[3], b[4]) == (66.0, 0.75, None, three, None)
    dropped_at, p_m = pg.execute("select dropped_at, p_at_placement from bets where id = %s",
                                 (ids[pid(pg, "De'Anthony Melton")],)).fetchone()
    assert dropped_at == three and float(p_m) == 0.64                         # left in place, p unchanged
    assert pg.execute("select count(*) from bets").fetchone()[0] == 3

    # placed rows are frozen at the p you bet at; a returning leg clears dropped
    bets.set_placed(pg, ids[pid(pg, "Devin Booker")], 10)
    booker3 = card.Row(tip, "GSW@PHX", "Devin Booker", "reb", "kalshi", "NO", 3.5, 0.60, 0.55, 0.55, [],
                       pid(pg, "Devin Booker"))
    counts = bets.log_card(pg, D, [booker3, jokic, melton], now=three)
    assert counts["frozen"] == 1 and counts["updated"] == 2
    assert float(pg.execute("select price_cents from bets where id = %s",
                            (ids[pid(pg, "Devin Booker")],)).fetchone()[0]) == 66.0
    assert pg.execute("select dropped_at from bets where id = %s",
                      (ids[pid(pg, "De'Anthony Melton")],)).fetchone()[0] is None

    # after tip: nothing is refreshed, added, or marked dropped (started games leave the card)
    late = card.Row(tip, "GSW@PHX", "De'Anthony Melton", "reb", "sleeper", "over", 3.5, 0.90,
                    player_id=pid(pg, "De'Anthony Melton"))
    counts = bets.log_card(pg, D, [late], now=after)
    assert counts == {"new": 0, "updated": 0, "frozen": 0, "started": 1, "dropped": 0}
    assert float(pg.execute("select p_at_placement from bets where id = %s",
                            (ids[pid(pg, "De'Anthony Melton")],)).fetchone()[0]) == 0.64
    assert pg.execute("select count(*) from bets where dropped_at is not null").fetchone()[0] == 0


def test_placed_with_and_without_stake(world):
    pg = world
    b = add(pg, "Devin Booker", stat="reb", side="no", line=3.5, platform="kalshi", price_cents=62, source="card")
    pg.commit()
    assert bets.main(["placed", str(b)], conn=pg) == 0
    assert pg.execute("select placed, stake, placed_at is not null from bets where id = %s", (b,)).fetchone() == \
        (True, None, True)
    assert bets.main(["placed", str(b), "10"], conn=pg) == 0
    assert float(pg.execute("select stake from bets where id = %s", (b,)).fetchone()[0]) == 10


def test_add_resolves_by_name_with_confirm(world, capsys):
    pg = world
    argv = ["add", "--player", "jokic", "--stat", "ast", "--side", "yes", "--line", "8+", "--platform", "kalshi",
            "--price", "75", "--date", "2026-11-16"]
    assert bets.main(argv, conn=pg, inp=lambda _: "n") == 1
    assert pg.execute("select count(*) from bets").fetchone()[0] == 0
    asked = []
    assert bets.main(argv, conn=pg, inp=lambda q: asked.append(q) or "y") == 0
    assert "Nikola Jokić (DEN)" in asked[0]
    assert pg.execute("select side, line_or_strike from bets").fetchone() == ("yes", 7.5)
    assert bets.main(["add", "--player", "nobody here", "--stat", "pts", "--side", "o", "--line", "1.5"],
                     conn=pg, inp=lambda _: "y") == 2


def test_platform_side_mismatch_is_rejected(world):
    with pytest.raises(ValueError):
        add(world, "Devin Booker", stat="reb", side="over", line=3.5, platform="kalshi")
