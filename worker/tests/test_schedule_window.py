"""The UTC cron + ET guard: runs happen 9am-1am ET in both EST and EDT, and 6am ET nightly."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from prop_worker import ingest, nightly
from prop_worker.schedule_window import at_hour, in_window

W = "09:00-01:00"


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


@pytest.mark.parametrize("now,inside", [
    (utc(2026, 11, 11, 13, 45), False),   # 8:45 am EST
    (utc(2026, 11, 11, 14, 0), True),     # 9:00 am EST
    (utc(2026, 11, 12, 6, 0), True),      # 1:00 am EST
    (utc(2026, 11, 12, 6, 15), False),    # 1:15 am EST
    (utc(2026, 7, 1, 13, 0), True),       # 9:00 am EDT
    (utc(2026, 7, 2, 5, 0), True),        # 1:00 am EDT
    (utc(2026, 7, 2, 6, 0), False),       # 2:00 am EDT
    (utc(2026, 11, 11, 23, 30), True),    # 6:30 pm
])
def test_ingest_window(now, inside):
    assert in_window(now, W) is inside


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        a, b = (lo, hi) if rng == "*" else (int(rng.split("-")[0]), int(rng.split("-")[-1]))
        out |= set(range(a, b + 1, int(step) if step else 1))
    return out


def cron_fires(expr: str, t: datetime) -> bool:
    """Minimal 5-field cron matcher (minute hour dom month dow), enough for these workflows."""
    m, h, dom, mon, dow = expr.split()
    return (t.minute in _field(m, 0, 59) and t.hour in _field(h, 0, 23) and t.day in _field(dom, 1, 31)
            and t.month in _field(mon, 1, 12) and (dow == "*" or t.isoweekday() % 7 in _field(dow, 0, 6)))


def _crons(name):
    doc = yaml.safe_load((WORKFLOWS / name).read_text())
    return [c["cron"] for c in doc[True]["schedule"]]          # YAML 1.1 reads the 'on:' key as True


def test_ingest_crons_plus_guard_cover_9am_to_1am_every_day_of_the_year():
    from prop_worker import config
    crons = _crons("ingest.yml")
    t, end = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2027, 1, 2, tzinfo=timezone.utc)
    fired, wasted = set(), 0
    while t < end:
        if any(cron_fires(c, t) for c in crons):
            if in_window(t, W):
                fired.add(t)
            else:
                wasted += 1
        t += timedelta(minutes=15)
    day = datetime(2026, 1, 1, 9, 0, tzinfo=config.ET)
    while day < datetime(2027, 1, 1, tzinfo=config.ET):
        for n in range(65):                                      # 09:00 .. 01:00 next day, every 15 min
            slot = (day + timedelta(minutes=15 * n)).astimezone(timezone.utc)
            assert slot in fired, f"no run at {slot.astimezone(config.ET):%Y-%m-%d %H:%M} ET"
        day = datetime.combine(day.date() + timedelta(days=1), day.timetz())
    assert wasted < 7 * 61 + 10                                  # only Mar/Nov fire outside the window


def test_nightly_crons_hit_6am_et_once_a_day_all_year():
    from prop_worker import config
    crons = _crons("nightly.yml")
    t, hits = datetime(2026, 1, 1, tzinfo=timezone.utc), {}
    while t < datetime(2027, 1, 1, tzinfo=timezone.utc):
        if any(cron_fires(c, t) for c in crons) and at_hour(t, 6):
            hits.setdefault(t.astimezone(config.ET).date(), []).append(t)
        t += timedelta(minutes=15)
    assert len(hits) == 365 and all(len(v) == 1 for v in hits.values())


def test_nightly_hour_guard():
    assert at_hour(utc(2026, 11, 11, 11, 0), 6) and not at_hour(utc(2026, 11, 11, 10, 0), 6)   # EST
    assert at_hour(utc(2026, 7, 1, 10, 0), 6) and not at_hour(utc(2026, 7, 1, 11, 0), 6)       # EDT


def test_ingest_outside_window_does_nothing(capsys, monkeypatch):
    monkeypatch.setattr(ingest, "run_all", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert ingest.main(["--window", W], now=utc(2026, 11, 11, 8, 0)) == 0
    assert "outside" in capsys.readouterr().out


def test_nightly_wrong_hour_does_nothing(capsys, monkeypatch):
    monkeypatch.setattr(nightly, "run_all", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert nightly.main(["--only-at-et-hour", "6"], now=utc(2026, 11, 11, 10, 0)) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_unknown_job_names_are_rejected():
    with pytest.raises(SystemExit):
        ingest.main(["--jobs", "kalshi,bogus"])
    with pytest.raises(SystemExit):
        nightly.main(["--jobs", "prune,bogus"])


def test_second_trigger_in_a_slot_does_nothing(pg, monkeypatch, capsys):
    from contextlib import contextmanager
    from prop_worker import ingest

    @contextmanager
    def same_conn():
        yield pg
    monkeypatch.setattr(ingest, "connect", same_conn)
    ran = []
    monkeypatch.setattr(ingest, "run_all", lambda jobs, **kw: ran.append(len(jobs)) or (0, []))
    now = utc(2026, 11, 12, 0, 15)
    pg.execute("insert into job_runs (job, status, started_at) values ('kalshi', 'green', %s)",
               (now - timedelta(minutes=4),))
    pg.commit()
    assert ingest.main(["--window", W, "--min-gap-minutes", "10"], now=now) == 0
    assert ran == [] and "already ran" in capsys.readouterr().out
    later = now + timedelta(minutes=11)                       # next slot: runs
    assert ingest.main(["--window", W, "--min-gap-minutes", "10"], now=later) == 0 and ran == [6]


def test_an_errored_run_does_not_block_a_retry(pg, monkeypatch):
    from contextlib import contextmanager
    from prop_worker import ingest

    @contextmanager
    def same_conn():
        yield pg
    monkeypatch.setattr(ingest, "connect", same_conn)
    ran = []
    monkeypatch.setattr(ingest, "run_all", lambda jobs, **kw: ran.append(len(jobs)) or (0, []))
    now = utc(2026, 11, 12, 0, 15)
    pg.execute("insert into job_runs (job, status, started_at) values ('propline', 'error', %s)",
               (now - timedelta(minutes=2),))
    pg.commit()
    ingest.main(["--window", W, "--min-gap-minutes", "10"], now=now)
    assert ran == [6]


def test_workflow_passes_the_gap_guard():
    text = (Path(__file__).resolve().parents[2] / ".github/workflows/ingest.yml").read_text()
    assert "--window 09:00-01:00 --min-gap-minutes 10" in text
