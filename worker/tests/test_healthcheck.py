"""Healthcheck tests: offline, against a fake session + saved real responses (injury PDF, PropLine)."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from prop_worker import config, healthcheck as hc

FIXTURES = Path(__file__).parent / "fixtures"
PDF = (FIXTURES / "injury_report_2026-11-10_01_00PM.pdf").read_bytes()
PL = FIXTURES / "propline"
NOW = datetime(2026, 11, 10, 13, 40, tzinfo=config.ET)
KEY = "test-key-never-printed"


def _pl(name):
    return json.loads((PL / name).read_text())


class Resp:
    def __init__(self, status=200, payload=None, content=b"", headers=None):
        self.status_code, self._payload, self.content = status, payload, content
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _event(eid, completed):
    return {"id": eid, "status": {"type": {"completed": completed}}}


def _team(n):
    return {"statistics": [{"labels": ["MIN", "PTS", "FG", "3PT", "REB", "AST"],
                            "athletes": [{}] * n}]}


class FakeSession:
    def __init__(self, routes=None, pdf_slot="2026-11-10_01_00PM"):
        self.routes = routes or {}
        self.pdf_slot = pdf_slot
        self.headers_seen = []

    def head(self, url, **kw):
        return Resp(200 if self.pdf_slot and self.pdf_slot in url else 403)

    def get(self, url, params=None, headers=None, **kw):
        self.headers_seen.append(headers or {})
        if url.endswith(".pdf"):
            return Resp(200, content=PDF) if self.pdf_slot in url else Resp(403)
        key = (url.rsplit("/", 1)[-1], tuple(sorted((params or {}).items())))
        for (path, match), resp in self.routes.items():
            if key[0] == path and all(item in key[1] for item in match):
                return resp
        return Resp(404)


QUOTA = {"x-daily-remaining": "994", "x-daily-limit": "1000"}


def healthy_routes():
    return {
        ("scoreboard", (("dates", "20261110"),)): Resp(payload={"events": [_event("1", False)]}),
        ("scoreboard", (("dates", "20261109"),)): Resp(payload={"events": [_event("401", True)]}),
        ("summary", ()): Resp(payload={"boxscore": {"players": [_team(12), _team(11)]}}),
        ("injuries", ()): Resp(payload={"injuries": [{"injuries": [{}, {}]}]}),
        ("markets", ()): Resp(payload={"markets": [{}, {}]}),
        ("todaysScoreboard_00.json", ()): Resp(403),
        # PropLine (real responses saved 2026-11-10)
        ("events", ()): Resp(payload=_pl("events_2026-11-10.json"), headers=QUOTA),
        ("odds", ()): Resp(payload=_pl("event_odds_642879.json"), headers=QUOTA),
        ("scores", ()): Resp(payload=_pl("scores_2026-11-10.json"), headers=QUOTA),
        ("stats", ()): Resp(payload=_pl("event_stats_641560.json"), headers=QUOTA),
    }


def _probe(routes, name, **kw):
    kw.setdefault("propline_key", KEY)
    return {p.name: p for p in hc.run(FakeSession(routes), NOW, db_url="", **kw)}[name]


def test_injury_pdf_url_format():
    assert hc.injury_pdf_url(datetime(2026, 11, 10, 13, 0)).endswith("Injury-Report_2026-11-10_01_00PM.pdf")
    assert hc.injury_pdf_url(datetime(2026, 11, 10, 11, 30)).endswith("Injury-Report_2026-11-10_11_30AM.pdf")
    assert hc.injury_pdf_url(datetime(2026, 11, 10, 0, 15)).endswith("Injury-Report_2026-11-10_12_15AM.pdf")
    assert hc.injury_pdf_url(datetime(2026, 11, 10, 12, 45)).endswith("Injury-Report_2026-11-10_12_45PM.pdf")


def test_recent_slots_floor_to_quarter_hour():
    slots = list(hc.recent_slots(NOW, hours=1))
    assert [s.strftime("%H:%M") for s in slots] == ["13:30", "13:15", "13:00", "12:45"]


def test_all_sources_healthy_passes_even_when_cdn_blocked():
    results = hc.run(FakeSession(healthy_routes()), NOW, db_url="", require_db=False, propline_key=KEY)
    by = {p.name: p for p in results}
    assert by["postgres"].status == hc.SKIP
    for name in ("espn_scoreboard", "espn_boxscore", "espn_injuries", "nba_injury_pdf", "kalshi",
                 "propline_props (info)", "propline_boxscore (info)"):
        assert by[name].status == hc.PASS, by[name]
    assert by["cdn_nba (info)"].status == hc.INFO  # blocked, but not required
    assert "Injury Report: 11/10/26 01:00 PM" in by["nba_injury_pdf"].detail
    assert "40 min old" in by["nba_injury_pdf"].detail
    assert "23 player rows" in by["espn_boxscore"].detail
    assert hc.overall_ok(results)


def test_missing_db_fails_when_required():
    results = hc.run(FakeSession(healthy_routes()), NOW, db_url="", require_db=True, propline_key=KEY)
    assert {p.name: p.status for p in results}["postgres"] == hc.FAIL
    assert not hc.overall_ok(results)


def test_blocked_injury_pdf_fails_loudly():
    results = hc.run(FakeSession(healthy_routes(), pdf_slot=None), NOW, db_url="", propline_key=KEY)
    pdf = {p.name: p for p in results}["nba_injury_pdf"]
    assert pdf.status == hc.FAIL and "missing or blocked" in pdf.detail
    assert not hc.overall_ok(results)


def test_boxscore_missing_columns_fails():
    routes = healthy_routes()
    routes[("summary", ())] = Resp(payload={"boxscore": {"players": [
        {"statistics": [{"labels": ["PTS"], "athletes": []}]}] * 2}})
    probe = _probe(routes, "espn_boxscore")
    assert probe.status == hc.FAIL and "missing" in probe.detail


def test_source_exception_becomes_fail_not_crash():
    routes = healthy_routes()
    routes[("markets", ())] = Resp(503)
    probe = _probe(routes, "kalshi")
    assert probe.status == hc.FAIL and "503" in probe.detail


@pytest.mark.parametrize("payload", [{}, {"events": None}])
def test_scoreboard_shape_change_fails(payload):
    routes = healthy_routes()
    routes[("scoreboard", (("dates", "20261110"),))] = Resp(payload=payload)
    assert _probe(routes, "espn_scoreboard").status == hc.FAIL


# ── PropLine ──────────────────────────────────────────────────────────────────

def test_propline_props_reports_books_markets_and_quota():
    p = _probe(healthy_routes(), "propline_props (info)")
    assert p.status == hc.PASS
    assert "player props on our tier" in p.detail
    assert "3 books" in p.detail and "pts=3" in p.detail and "reb=3" in p.detail and "ast=3" in p.detail
    assert "missing ['player_points_rebounds_assists', 'player_threes']" in p.detail
    assert "quota 994/1000" in p.detail


def test_propline_boxscore_is_free_and_has_core_stats():
    p = _probe(healthy_routes(), "propline_boxscore (info)")
    assert p.status == hc.PASS and "event 641560" in p.detail and "24 players" in p.detail


def test_propline_key_sent_as_header_and_never_in_output():
    s = FakeSession(healthy_routes())
    results = hc.run(s, NOW, db_url="", propline_key=KEY)
    assert any(h.get("X-API-Key") == KEY for h in s.headers_seen)
    assert KEY not in hc.render(results)


@pytest.mark.parametrize("code", [402, 403])
def test_propline_props_off_tier_is_info_not_failure(code):
    routes = healthy_routes()
    routes[("odds", ())] = Resp(code)
    results = hc.run(FakeSession(routes), NOW, db_url="", propline_key=KEY)
    p = {r.name: r for r in results}["propline_props (info)"]
    assert p.status == hc.INFO and "NOT on our tier" in p.detail and str(code) in p.detail
    assert hc.overall_ok(results)  # never blocks the run


def test_propline_no_prop_markets_posted_yet():
    routes = healthy_routes()
    empty = dict(_pl("event_odds_642879.json"), bookmakers=[])
    routes[("odds", ())] = Resp(payload=empty)
    p = _probe(routes, "propline_props (info)")
    assert p.status == hc.INFO and "0 player-prop markets" in p.detail


def test_propline_without_key_is_skipped():
    for name in ("propline_props (info)", "propline_boxscore (info)"):
        assert _probe(healthy_routes(), name, propline_key="").status == hc.SKIP


def test_propline_only_past_events_fails_clearly():
    routes = healthy_routes()
    late = datetime(2026, 12, 31, 12, 0, tzinfo=config.ET)
    p = {r.name: r for r in hc.run(FakeSession(routes), late, db_url="", propline_key=KEY)}
    assert p["propline_props (info)"].status == hc.INFO
    assert "no upcoming" in p["propline_props (info)"].detail
