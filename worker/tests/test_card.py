"""card: research-only shortlist formulas, Sleeper-line override, on-the-line flag, exclusions."""

from datetime import date, datetime, timedelta, timezone

import pytest

from prop_worker import card
from prop_worker.hitrates import GameLine
from prop_worker.slate import Prop

D = date(2026, 11, 16)
SZN = date(2026, 10, 1)
H2H = date(2025, 10, 1)
TIP = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)        # 4:00 pm PT Nov 16
SEEN = datetime(2026, 11, 16, 21, 0, tzinfo=timezone.utc)


def logs(reb_values, stat="reb"):
    """Most recent first: reb_values[0] is the game the day before D."""
    out = []
    for i, v in enumerate(reb_values):
        kw = {"pts": 20, "reb": 5, "ast": 4, "fg3m": 2}
        kw[stat] = v
        out.append(GameLine(D - timedelta(days=i + 1), "LAL", 30.0, False, **kw))
    return out


def prop(values, *, stat="reb", sleeper=None, consensus=None, nv=None, kalshi=(), injury=None, tip=TIP,
         seen=SEEN, name="Devin Booker"):
    books = {}
    if consensus is not None:
        books["draftkings"] = {"line": consensus, "over": -110, "under": -110, "seen": seen}
    if sleeper is not None:
        books["sleeper"] = {"line": sleeper, "over": None, "under": None, "seen": seen}
    ks = [{"line": line, "yes_ask": y, "no_ask": n, "ticker": f"K{line}", "seen": seen} for line, y, n in kalshi]
    return Prop(1, name, "PHX", "LAL", 10, tip, stat, books=books, kalshi=ks, kalshi_all=ks,
                consensus=consensus, consensus_n=1 if consensus is not None else 0, no_vig_over=nv,
                injury=injury, games=logs(values, stat), matchup="LAL@PHX")


def rows_for(*props, now=None):
    return card.build_rows(list(props), before=D, season_start=SZN, h2h_since=H2H, now=now)


# ── formulas ────────────────────────────────────────────────────────────────

def test_p_hist_formula():
    assert card.p_hist(7, 10, 10, 15, 12, 18) == pytest.approx(30.5 / 46)
    assert card.p_hist(0, 0, 0, 0, 0, 0) == pytest.approx(0.5)          # no games -> 1.5/3


def test_p_blend_market_weighted_and_no_market_shrink():
    assert card.p_blend(0.9, 0.5) == (pytest.approx(0.35 * 0.9 + 0.65 * 0.5), True)
    assert card.p_blend(0.9, None) == (pytest.approx(0.7), False)        # halfway to .5
    assert card.p_blend(0.2, None) == (pytest.approx(0.35), False)


def test_side_and_p():
    assert card.side_and_p(0.7) == ("over", 0.7)
    assert card.side_and_p(0.5) == ("over", 0.5)
    side, p = card.side_and_p(0.3)
    assert side == "under" and p == pytest.approx(0.7)


def test_kalshi_formula_and_thresholds():
    mid = card.yes_mid(0.40, 0.62)                       # (0.40 + 0.38) / 2
    assert mid == pytest.approx(0.39)
    assert card.yes_mid(0.40, None) == 0.40
    pk = card.p_kalshi(1, 10, mid)
    assert pk == pytest.approx(0.35 * 1.5 / 11 + 0.65 * 0.39)
    side, p, price = card.kalshi_pick(pk, 0.40, 0.62)
    assert side == "NO" and p == pytest.approx(1 - pk) and price == 0.62
    assert p - price >= 0.05 and p >= 0.68
    # edge under 5c -> nothing
    assert card.kalshi_pick(0.30, 0.31, 0.66) is None                  # NO p .70, edge 4c
    # price over 85c -> nothing, even with edge
    assert card.kalshi_pick(0.05, 0.04, 0.87) is None
    # p under .68 -> nothing, even with edge
    assert card.kalshi_pick(0.35, 0.40, 0.58) is None                  # NO p .65 vs 58c
    # YES side
    assert card.kalshi_pick(0.80, 0.74, 0.28)[0] == "YES"


def test_kalshi_row_has_max_price_p_minus_5c():
    # 1 of 10 games at 4+, market ~39c YES -> NO
    p = prop([2, 3, 2, 1, 3, 2, 5, 3, 2, 3], kalshi=[(3.5, 0.40, 0.62)])
    rows, _ = rows_for(p)
    (r,) = [r for r in rows if r.platform == "kalshi"]
    assert (r.side, r.line, r.price) == ("NO", 3.5, 0.62)
    assert r.max_price == pytest.approx(round(r.p - 0.05, 2))
    assert "4+" in card.render([r], 0, slate=str(D), stats={"excluded_injured": 0, "started": 0, "stale": 0})


# ── Sleeper line override ───────────────────────────────────────────────────

def test_sleeper_line_differs_hit_rates_taken_at_sleeper_line_and_flagged():
    vals = [5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5]           # always 5
    # consensus 5.5 (all unders), Sleeper 4.5 (all overs): the leg must use 4.5
    p = prop(vals, sleeper=4.5, consensus=5.5, nv=0.45)
    (r,) = [r for r in rows_for(p)[0] if r.platform == "sleeper"]
    assert r.line == 4.5 and r.side == "over"
    ph = card.p_hist(10, 10, 15, 15, 15, 15)
    assert r.p == pytest.approx(0.35 * ph + 0.65 * 0.45)
    assert "slp soft" in r.flags and "market disagrees" in r.flags        # |~0.96 - .45| > .25


def test_sleeper_line_harder_for_side_is_slp_hard():
    vals = [8] * 15
    p = prop(vals, sleeper=7.5, consensus=6.5, nv=0.70)               # over, Sleeper asks for more
    (r,) = [r for r in rows_for(p)[0] if r.platform == "sleeper"]
    assert r.side == "over" and "slp hard" in r.flags
    p = prop([2] * 15, sleeper=2.5, consensus=3.5, nv=0.30)            # under, Sleeper line lower = harder
    (r,) = [r for r in rows_for(p)[0] if r.platform == "sleeper"]
    assert r.side == "under" and "slp hard" in r.flags


def test_no_market_shrinks_and_flags():
    p = prop([6] * 15, sleeper=4.5)                                   # no consensus, no nv
    (r,) = rows_for(p)[0]
    assert "no market" in r.flags
    assert r.p == pytest.approx(0.5 + (card.p_hist(10, 10, 15, 15, 15, 15) - 0.5) / 2)


def test_sleeper_leg_below_062_is_dropped():
    p = prop([4, 3] * 8, sleeper=3.5, consensus=3.5, nv=0.5)           # coin flip
    assert rows_for(p)[0] == []


# ── on the line ─────────────────────────────────────────────────────────────

def test_on_the_line_booker_4_rebounds_for_a_4_plus_no():
    # Booker: exactly 4 rebounds in 3 of his last 10 -> a 4+ NO (or under 3.5) loses at exactly 4
    last10 = [4, 2, 4, 3, 1, 4, 2, 3, 2, 5]
    assert card.hinge(3.5, "NO") == 4 and card.hinge(3.5, "under") == 4
    assert card.hinge(3.5, "YES") == 3 and card.hinge(3.5, "over") == 3
    assert card.hinge(4.0, "over") == 4                                 # whole line -> the push value
    assert card.on_the_line(last10, 3.5, "NO")
    assert card.on_the_line(last10, 3.5, "under")
    assert not card.on_the_line([4, 2, 4, 3, 1, 1, 2, 2, 2, 5], 3.5, "NO")     # only 2 at 4
    # same prop, both platforms carry the flag
    p = prop(last10 + [1, 1, 2, 2, 1], sleeper=3.5, consensus=3.5, nv=0.40, kalshi=[(3.5, 0.30, 0.72)])
    rows, _ = rows_for(p)
    assert rows and all("on the line" in r.flags for r in rows)
    assert {r.side for r in rows} <= {"under", "NO"}


def test_on_the_line_counts_only_last_10_played():
    vals = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 4, 4, 4, 4]                  # the 4s are games 11-14
    assert card.last_values(logs(vals), "reb", D) == [1.0] * 10
    assert not card.on_the_line(card.last_values(logs(vals), "reb", D), 3.5, "under")


# ── exclusions ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", ["OUT", "D", "Q", "GTD"])
def test_listed_players_are_excluded(code):
    p = prop([8] * 15, sleeper=4.5, consensus=4.5, nv=0.8, kalshi=[(4.5, 0.70, 0.32)],
             injury={"code": code, "source": "official"})
    rows, stats = rows_for(p)
    assert rows == [] and stats["excluded_injured"] == 1


@pytest.mark.parametrize("code", ["P", "AVL", None])
def test_probable_and_available_stay(code):
    p = prop([8] * 15, sleeper=4.5, consensus=4.5, nv=0.8, injury={"code": code, "source": "official"})
    assert rows_for(p)[0]


def test_started_games_and_stale_quotes_are_hidden():
    p = prop([8] * 15, sleeper=4.5, consensus=4.5, nv=0.8)
    rows, stats = rows_for(p, now=TIP + timedelta(minutes=1))
    assert rows == [] and stats["started"] == 1
    fresh = prop([8] * 15, sleeper=4.5, consensus=4.5, nv=0.8, name="A")
    stale = prop([8] * 15, sleeper=4.5, consensus=4.5, nv=0.8, name="B", seen=SEEN - timedelta(hours=2))
    rows, stats = rows_for(fresh, stale)
    assert [r.player for r in rows] == ["A"] and stats["stale"] == 1


# ── shortlist and output ────────────────────────────────────────────────────

def test_shortlist_cuts_by_stat_priority_then_sorts_by_tip_and_p():
    def row(stat, p, tip_h):
        return card.Row(TIP + timedelta(hours=tip_h), "LAL@PHX", f"{stat}{p}", stat, "sleeper", "over", 4.5, p)
    rows = [row("pts", 0.90, 0), row("reb", 0.63, 1), row("ast", 0.70, 0), row("pra", 0.80, 1)]
    keep, cut = card.shortlist(rows, max_rows=3)
    assert cut == 1 and "pts0.9" not in [r.player for r in keep]        # PTS goes first despite p .90
    assert [r.player for r in keep] == ["ast0.7", "pra0.8", "reb0.63"]  # tip, then p


def test_render_stays_under_60_lines_and_says_research_only():
    rows = [card.Row(TIP, "LAL@PHX", f"P{i}", "reb", "sleeper", "over", 4.5, 0.7) for i in range(200)]
    keep, cut = card.shortlist(rows)
    text = card.render(keep, cut, slate=str(D), stats={"excluded_injured": 0, "started": 0, "stale": 0})
    lines = text.splitlines()
    assert len(lines) < 60 and cut == 150
    assert "RESEARCH-ONLY (UNVALIDATED)" in lines[0]
    assert "4:00p" in lines[4] and "LAL@PHX" in lines[4]


def test_slp_hard_legs_need_p_068_and_the_footer_counts_them():
    # over 4.5 at Sleeper vs consensus 3.5 (harder); 8 of 15 over 4.5, market .80 at 3.5 -> p ~.71: shown
    vals = [5, 6, 5, 3, 6, 5, 2, 5, 3, 6, 2, 3, 2, 3, 5]
    shown = prop(vals, sleeper=4.5, consensus=3.5, nv=0.80)
    rows, stats = rows_for(shown)
    (r,) = [r for r in rows if r.platform == "sleeper"]
    assert "slp hard" in r.flags and r.p >= 0.68 and stats["slp_hard_hidden"] == 0
    # same leg with a weaker market -> p in [.62, .68): hidden and counted
    weak = prop(vals, sleeper=4.5, consensus=3.5, nv=0.70)
    rows, stats = rows_for(weak)
    assert not [r for r in rows if r.platform == "sleeper"] and stats["slp_hard_hidden"] == 1
    # a soft or equal line at the same p stays
    soft = prop(vals, sleeper=4.5, consensus=5.5, nv=0.70)
    assert [r for r in rows_for(soft)[0] if r.platform == "sleeper"]
    text = card.render([], 0, slate=str(D), stats=stats)
    assert "1 slp hard < 0.68" in text


def test_to_dict_matches_render_for_dashboard():
    p = prop([2, 3, 2, 1, 3, 2, 5, 3, 2, 3], kalshi=[(3.5, 0.40, 0.62)])
    rows, _ = rows_for(p)
    stats = {"excluded_injured": 1, "started": 0, "stale": 0}
    d = card.to_dict(rows, 3, slate=str(D), stats=stats)
    assert d["research_only"] and d["stake"] == 0 and d["cut"] == 3 and d["stats"] == stats
    (k,) = [leg for leg in d["legs"] if leg["platform"] == "kalshi"]
    assert (k["side"], k["line"], k["price"]) == ("NO", "4+", 0.62)
    import json
    json.dumps(d, default=str)          # serializable as written by slate --card --out
