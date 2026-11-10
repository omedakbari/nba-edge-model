from datetime import datetime, timezone

from conftest import FIXTURES
from jobkit import NOW, Net, Resp, load, run, scoreboards, seed_players
from prop_worker import config
from prop_worker.healthcheck import injury_pdf_url
from prop_worker.jobs import injuries, schedule

PDF = (FIXTURES / "injury_report_2026-11-10_01_00PM.pdf").read_bytes()
PDF_URL = injury_pdf_url(datetime(2026, 11, 10, 13, 0))
BY_ESPN_ID = {"1": "ATL", "18": "NYK", "20": "PHI", "26": "UTA"}


def _net(espn=None, pdf_ok=True):
    net = Net([scoreboards(),
               (lambda u, p: u.endswith(".pdf"), Resp(content=PDF)),
               (lambda u, p: u.endswith("/injuries"),
                espn if isinstance(espn, Resp) else Resp(espn or load("espn/injuries_2026-11-10.json")))],
              heads={PDF_URL: 200} if pdf_ok else {})
    return net


def _setup(pg):
    seed_players(pg, [(1, "Jalen Brunson", "NYK"), (2, "Jaren Jackson Jr.", "UTA"), (3, "Joel Embiid", "PHI"),
                      (4, "Tyler Herro", "MIL"), (5, "Dominick Barlow", "PHI")])
    run(pg, _net(), "schedule", schedule.run)


def test_parse_espn_injuries_ids_and_teams():
    rows, skipped = injuries.parse_espn_injuries(load("espn/injuries_2026-11-10.json"), BY_ESPN_ID)
    assert rows and all(r["team_abbr"] in BY_ESPN_ID.values() for r in rows)
    assert all(r["espn_athlete_id"] and r["espn_athlete_id"].isdigit() for r in rows)
    assert {r["status"] for r in rows} <= {"Out", "Day-To-Day"}
    assert len(skipped) == 25                          # teams outside this small map are listed, not dropped


def _r(status, reason="Injury/Illness - Knee"):
    return {"status": status, "reason": reason}


def test_compare_rules():
    nba = {"NYK:a": _r("Questionable"), "NYK:b": _r("Out"), "NYK:c": _r("Out"), "NYK:d": _r("Out", "G League - Two-Way"),
           "NYK:e": _r("Probable"), "NYK:f": _r("Available")}
    espn = {"NYK:a": _r("Day-To-Day"), "NYK:b": _r("Day-To-Day"), "NYK:f": _r("Out"), "NYK:g": _r("Out"),
            "BOS:h": _r("Out")}
    kinds = {f["player_key"]: f["kind"] for f in injuries.compare(nba, espn, filed_teams={"NYK"})}
    assert kinds == {"NYK:b": "status_mismatch", "NYK:c": "missing_in_espn", "NYK:f": "status_mismatch",
                     "NYK:g": "missing_in_nba"}           # d: G League; e: absent but Probable; h: team not filed


def test_job_stores_both_sources_and_is_change_only(pg):
    _setup(pg)
    out = run(pg, _net(), "injuries", injuries.run)
    assert out.status == "green", out.red_reasons
    n_pdf = pg.execute("select count(*) from injuries where source = 'nba_pdf'").fetchone()[0]
    n_espn = pg.execute("select count(*) from injuries where source = 'espn'").fetchone()[0]
    assert n_pdf == 46 and n_espn == out.meta["espn_rows"] > 0
    ts = pg.execute("select distinct provider_ts from injuries where source = 'nba_pdf'").fetchall()
    assert ts == [(datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc),)]        # 1:00 pm ET
    jjj = pg.execute("select player_id is not null, status from injuries where player_key = 'UTA:jaren jackson'")
    assert jjj.fetchone() == (True, "Out")
    unresolved = pg.execute("select count(*) from injuries where player_id is null").fetchone()[0]
    assert unresolved > 0 and any("without a player_id" in w for w in out.warnings)

    again = run(pg, _net(), "injuries", injuries.run)
    assert again.meta["new_nba"] == 0 and again.meta["new_espn"] == 0
    assert pg.execute("select count(*) from injuries").fetchone()[0] == n_pdf + n_espn


def test_disagreements_are_flagged_once_and_resolved(pg):
    _setup(pg)
    espn = load("espn/injuries_2026-11-10.json")
    knicks = next(t for t in espn["injuries"] if t["id"] == "18")
    knicks["injuries"] = [{"id": "x", "status": "Out", "date": "2026-11-10T15:00Z", "details": {"type": "Ankle"},
                           "athlete": {"displayName": "Jalen Brunson",
                                       "links": [{"rel": ["playercard"], "href": "https://x/id/3934672/jalen-brunson"}]}}]
    run(pg, _net(espn), "injuries", injuries.run)
    run(pg, _net(espn), "injuries", injuries.run)
    flags = pg.execute("""select player_key, kind, nba_status, espn_status from injury_flags
                          where resolved_at is null and player_key = 'NYK:jalen brunson'""").fetchall()
    assert flags == [("NYK:jalen brunson", "missing_in_nba", None, "Out")]   # Knicks filed; Brunson not on it
    assert pg.execute("select count(*) from injury_flags where player_key = 'NYK:jalen brunson'").fetchone()[0] == 1

    knicks["injuries"] = []                                                # ESPN drops him -> agreement
    run(pg, _net(espn), "injuries", injuries.run)
    assert pg.execute("""select resolved_at is not null from injury_flags
                         where player_key = 'NYK:jalen brunson'""").fetchone()[0]


def test_missing_official_report_is_red_but_espn_still_stored(pg):
    _setup(pg)
    out = run(pg, _net(pdf_ok=False), "injuries", injuries.run)
    assert out.status == "red" and "no official injury report" in out.red_reasons[0]
    assert pg.execute("select count(*) from injuries where source = 'espn'").fetchone()[0] > 0


def test_espn_down_is_red_but_official_rows_stored(pg):
    _setup(pg)
    out = run(pg, _net(espn=Resp(status=503)), "injuries", injuries.run)
    assert out.status == "red" and any("ESPN injuries (secondary) failed" in r for r in out.red_reasons)
    assert pg.execute("select count(*) from injuries where source = 'nba_pdf'").fetchone()[0] == 46


def test_both_sources_down_is_an_error(pg):
    _setup(pg)
    out = run(pg, _net(espn=Resp(status=503), pdf_ok=False), "injuries", injuries.run)
    assert out.status == "error" and "503" in out.error        # nothing usable from either source
    assert pg.execute("select count(*) from injuries").fetchone()[0] == 0


def test_find_latest_pdf_walks_back_at_most_two_hours():
    url, slot = injuries.find_latest_pdf(Net(heads={PDF_URL: 200}), NOW.astimezone(config.ET))
    assert url == PDF_URL and slot.hour == 13
    later = datetime(2026, 11, 10, 15, 20, tzinfo=config.ET)               # 2h20m after the only report
    assert injuries.find_latest_pdf(Net(heads={PDF_URL: 200}), later) == (None, None)
