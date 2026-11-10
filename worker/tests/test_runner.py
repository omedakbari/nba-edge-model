from datetime import datetime, timezone

import pytest

from jobkit import NOW, Net, Resp, load, run, scoreboards
from prop_worker import config, runner
from prop_worker.jobs import schedule
from prop_worker.runner import JobResult, judge, run_all, send_ntfy, too_fast


# ── the red rules ─────────────────────────────────────────────────────────────

def test_zero_rows_on_game_day_is_red():
    reasons, _ = judge(JobResult(10, 0), 5.0, True, None, 0)
    assert reasons == ["loaded 0 rows on a game day"]


def test_zero_rows_on_off_day_or_when_not_expected_is_fine():
    assert judge(JobResult(0, 0), 5.0, False, None, 0)[0] == []
    assert judge(JobResult(0, 0, expected=False), 0.01, True, None, 0)[0] == []


def test_under_half_trailing_per_game_is_red():
    reasons, volume = judge(JobResult(100, 100, units=10), 5.0, True, trailing=25.0, trailing_n=8)
    assert volume == 10 and "10.0 rows per game vs trailing average 25.0" in reasons[0]
    assert judge(JobResult(100, 130, units=10), 5.0, True, trailing=25.0, trailing_n=8)[0] == []


def test_trailing_rule_needs_enough_history():
    assert judge(JobResult(100, 10, units=10), 5.0, True, trailing=25.0, trailing_n=4)[0] == []


def test_job_specific_red_reasons_apply_any_day():
    assert judge(JobResult(1, 1, red_reasons=["parse failed"]), 5.0, False, None, 0)[0] == ["parse failed"]


def test_fast_rule_relative_mode():
    assert not too_fast(0.5, median_s=0.6, n=10)          # normal for this job
    assert too_fast(0.1, median_s=0.6, n=10)              # < 25% of usual
    assert not too_fast(0.1, median_s=None, n=0)          # no history yet
    assert not too_fast(2.5, median_s=20.0, n=10)         # never red at >= 2 s


def test_fast_rule_absolute_mode(monkeypatch):
    monkeypatch.setattr(config, "RED_DURATION_MODE", "absolute")
    assert too_fast(1.9, median_s=None, n=0)
    reasons, _ = judge(JobResult(5, 5), 1.0, True, None, 0)
    assert reasons and "finished in 1.00s" in reasons[0]


# ── job_runs, alerts, summary ────────────────────────────────────────────────

def _ok(ctx):
    return JobResult(3, 3)


def _empty(ctx):
    return JobResult(0, 0)


def _boom(ctx):
    ctx.conn.execute("insert into job_runs (job) values ('should be rolled back')")
    raise RuntimeError("source exploded")


class Notifier:
    def __init__(self):
        self.sent = []

    def __call__(self, title, body):
        self.sent.append((title, body))
        return "ntfy sent"


def _clock():
    t = iter([0.0, 5.0] * 20)
    return lambda: next(t)


def _game_day(pg):
    run(pg, Net([scoreboards()]), "schedule", schedule.run)


def test_run_all_records_every_job_and_alerts_once(pg, tmp_path, monkeypatch):
    _game_day(pg)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    note = Notifier()
    code, outs = run_all([("ok", _ok), ("boom", _boom), ("empty", _empty), ("ok2", _ok)], conn=pg,
                         session=Net(), now=NOW, notifier=note, clock=_clock())
    assert code == 1
    assert [(o.job, o.status) for o in outs] == [("ok", "green"), ("boom", "error"), ("empty", "red"), ("ok2", "green")]
    rows = pg.execute("select job, status, rows_out, error from job_runs where job <> 'schedule' order by id").fetchall()
    assert rows == [("ok", "green", 3, None), ("boom", "error", None, "RuntimeError: source exploded"),
                    ("empty", "red", 0, None), ("ok2", "green", 3, None)]   # boom's insert was rolled back
    assert len(note.sent) == 1
    title, body = note.sent[0]
    assert title == "NBA ingest: 2 job(s) ERROR/RED"
    assert "boom ERROR: RuntimeError: source exploded" in body and "empty RED: loaded 0 rows" in body
    assert body.endswith("https://github.com/me/repo/actions/runs/42")
    text = summary.read_text()
    assert "| boom | ERROR |" in text and "| ok | GREEN |" in text


def test_all_green_exits_zero_without_alert(pg):
    _game_day(pg)
    note = Notifier()
    code, _ = run_all([("ok", _ok)], conn=pg, session=Net(), now=NOW, notifier=note, clock=_clock())
    assert code == 0 and note.sent == []


def test_empty_game_day_everything_green(pg):
    from prop_worker.jobs import kalshi
    empty = load("espn/scoreboard_20260801.json")
    net = Net([(lambda u, p: u.endswith("/scoreboard"), Resp(empty)),
               (lambda u, p: u.endswith("/markets"), Resp({"markets": [], "cursor": ""}))])
    code, outs = run_all([("schedule", schedule.run), ("kalshi", kalshi.run)], conn=pg, session=net,
                         now=NOW, notifier=Notifier(), clock=_clock())
    assert code == 0 and all(o.status == "green" and o.is_game_day is False for o in outs)


def test_trailing_average_uses_same_time_of_day(pg):
    def add(hour_et, volume):
        ts = datetime(2026, 11, 9, hour_et, 0, tzinfo=config.ET)
        pg.execute("""insert into job_runs (job, started_at, status, is_game_day, expected, volume, duration_s)
                      values ('k', %s, 'green', true, true, %s, 1.0)""", (ts, volume))
    for _ in range(5):
        add(13, 10)                 # afternoon runs
        add(19, 100)                # evening runs
        add(0, 50)                  # just after midnight
    pg.commit()
    avg, n, med = runner.trailing_average(pg, "k", datetime(2026, 11, 10, 13, 30, tzinfo=config.ET))
    assert (avg, n, med) == (10.0, 5, 1.0)
    avg, n, _ = runner.trailing_average(pg, "k", datetime(2026, 11, 10, 23, 30, tzinfo=config.ET))
    assert (avg, n) == (50.0, 5)    # 23h is within 1 h of 0h (wraps around midnight)


def test_ntfy_posts_and_never_raises(monkeypatch):
    net = Net()
    assert send_ntfy("t", "b", session=net, topic="my-topic") == "ntfy sent"
    url, data, headers = net.posts[0]
    assert url == "https://ntfy.sh/my-topic" and data == b"b" and headers["Title"] == "t"
    assert send_ntfy("t", "b", session=net, topic="") == "ntfy skipped: NTFY_TOPIC not set"

    class Broken:
        def post(self, *a, **k):
            raise ConnectionError("down")
    assert send_ntfy("t", "b", session=Broken(), topic="x").startswith("ntfy FAILED")


def test_judged_volume_is_stored(pg):
    _game_day(pg)

    def per_game(ctx):
        return JobResult(60, 60, units=6)
    run_all([("pg", per_game)], conn=pg, session=Net(), now=NOW, notifier=Notifier(), clock=_clock())
    assert pg.execute("select volume, expected, is_game_day from job_runs where job = 'pg'").fetchone() == \
        (10, True, True)


@pytest.mark.parametrize("now,slate", [
    (datetime(2026, 11, 11, 5, 30, tzinfo=timezone.utc), "2026-11-10"),      # 12:30 am ET
    (datetime(2026, 11, 11, 14, 0, tzinfo=timezone.utc), "2026-11-11"),      # 9 am ET
])
def test_slate_rolls_over_at_6am_et(now, slate):
    from prop_worker.util import slate_date
    assert slate_date(now).isoformat() == slate


def test_status_report_shows_latest_and_per_day(pg):
    from prop_worker import status
    _game_day(pg)
    run_all([("ok", _ok), ("empty", _empty)], conn=pg, session=Net(), now=NOW, notifier=Notifier(), clock=_clock())
    text = status.render(status.latest_runs(pg), status.by_day(pg, 30), status.db_size_mb(pg), status.table_sizes(pg))
    assert "empty" in text and "RED" in text and "loaded 0 rows" in text
    assert "2026-11-10  game day  HAS RED" in text and "MB of 500 MB" in text
