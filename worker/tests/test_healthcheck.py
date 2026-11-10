"""Healthcheck tests: offline, against a fake session + the saved real injury PDF."""

from datetime import datetime
from pathlib import Path

import pytest

from prop_worker import config, healthcheck as hc

FIXTURES = Path(__file__).parent / "fixtures"
PDF = (FIXTURES / "injury_report_2026-11-10_01_00PM.pdf").read_bytes()
NOW = datetime(2026, 11, 10, 13, 40, tzinfo=config.ET)


class Resp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code, self._payload, self.content = status, payload, content

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

    def head(self, url, **kw):
        return Resp(200 if self.pdf_slot and self.pdf_slot in url else 403)

    def get(self, url, params=None, **kw):
        if url.endswith(".pdf"):
            return Resp(200, content=PDF) if self.pdf_slot in url else Resp(403)
        key = (url.rsplit("/", 1)[-1], tuple(sorted((params or {}).items())))
        for (path, match), resp in self.routes.items():
            if key[0] == path and all(item in key[1] for item in match):
                return resp
        return Resp(404)


def healthy_routes():
    return {
        ("scoreboard", (("dates", "20261110"),)): Resp(payload={"events": [_event("1", False)]}),
        ("scoreboard", (("dates", "20261109"),)): Resp(payload={"events": [_event("401", True)]}),
        ("summary", ()): Resp(payload={"boxscore": {"players": [_team(12), _team(11)]}}),
        ("injuries", ()): Resp(payload={"injuries": [{"injuries": [{}, {}]}]}),
        ("markets", ()): Resp(payload={"markets": [{}, {}]}),
        ("todaysScoreboard_00.json", ()): Resp(403),
    }


def test_injury_pdf_url_format():
    assert hc.injury_pdf_url(datetime(2026, 11, 10, 13, 0)).endswith("Injury-Report_2026-11-10_01_00PM.pdf")
    assert hc.injury_pdf_url(datetime(2026, 11, 10, 11, 30)).endswith("Injury-Report_2026-11-10_11_30AM.pdf")
    assert hc.injury_pdf_url(datetime(2026, 11, 10, 0, 15)).endswith("Injury-Report_2026-11-10_12_15AM.pdf")
    assert hc.injury_pdf_url(datetime(2026, 11, 10, 12, 45)).endswith("Injury-Report_2026-11-10_12_45PM.pdf")


def test_recent_slots_floor_to_quarter_hour():
    slots = list(hc.recent_slots(NOW, hours=1))
    assert [s.strftime("%H:%M") for s in slots] == ["13:30", "13:15", "13:00", "12:45"]


def test_all_sources_healthy_passes_even_when_cdn_blocked():
    results = hc.run(FakeSession(healthy_routes()), NOW, db_url="", require_db=False)
    by = {p.name: p for p in results}
    assert by["postgres"].status == hc.SKIP
    for name in ("espn_scoreboard", "espn_boxscore", "espn_injuries", "nba_injury_pdf", "kalshi"):
        assert by[name].status == hc.PASS, by[name]
    assert by["cdn_nba (info)"].status == hc.INFO  # blocked, but not required
    assert "Injury Report: 11/10/26 01:00 PM" in by["nba_injury_pdf"].detail
    assert "40 min old" in by["nba_injury_pdf"].detail
    assert "23 player rows" in by["espn_boxscore"].detail
    assert hc.overall_ok(results)


def test_missing_db_fails_when_required():
    results = hc.run(FakeSession(healthy_routes()), NOW, db_url="", require_db=True)
    assert {p.name: p.status for p in results}["postgres"] == hc.FAIL
    assert not hc.overall_ok(results)


def test_blocked_injury_pdf_fails_loudly():
    results = hc.run(FakeSession(healthy_routes(), pdf_slot=None), NOW, db_url="")
    pdf = {p.name: p for p in results}["nba_injury_pdf"]
    assert pdf.status == hc.FAIL and "missing or blocked" in pdf.detail
    assert not hc.overall_ok(results)


def test_boxscore_missing_columns_fails():
    routes = healthy_routes()
    routes[("summary", ())] = Resp(payload={"boxscore": {"players": [
        {"statistics": [{"labels": ["PTS"], "athletes": []}]}] * 2}})
    probe = {p.name: p for p in hc.run(FakeSession(routes), NOW, db_url="")}["espn_boxscore"]
    assert probe.status == hc.FAIL and "missing" in probe.detail


def test_source_exception_becomes_fail_not_crash():
    routes = healthy_routes()
    routes[("markets", ())] = Resp(503)
    probe = {p.name: p for p in hc.run(FakeSession(routes), NOW, db_url="")}["kalshi"]
    assert probe.status == hc.FAIL and "503" in probe.detail


@pytest.mark.parametrize("payload", [{}, {"events": None}])
def test_scoreboard_shape_change_fails(payload):
    routes = healthy_routes()
    routes[("scoreboard", (("dates", "20261110"),))] = Resp(payload=payload)
    probe = {p.name: p for p in hc.run(FakeSession(routes), NOW, db_url="")}["espn_scoreboard"]
    assert probe.status == hc.FAIL
