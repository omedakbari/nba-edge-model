"""pick_snapshots: archive pick-time Kalshi prices before prune (DECISIONS.md D15). Off by default."""

from datetime import timedelta
from pathlib import Path

import pytest

from jobkit import run
from prop_worker import config, replay
from prop_worker.jobs import pick_snapshots
from test_replay import PICK, TIP, D, slate  # noqa: F401  (fixture)

SQL = Path(__file__).resolve().parents[2] / "sql" / "011_kalshi_pick_snapshots.sql"
AFTER = TIP + timedelta(hours=1)


def _apply(pg):
    pg.execute(SQL.read_text())
    pg.commit()


def test_off_by_default_is_a_green_noop(pg):
    assert config.PICK_SNAPSHOTS_ENABLED is False
    out = run(pg, None, "pick_snapshots", pick_snapshots.run, now=AFTER)
    assert out.status == "green" and out.rows_in == 0


def test_on_without_table_is_red_and_names_the_migration(pg, monkeypatch):
    monkeypatch.setattr(config, "PICK_SNAPSHOTS_ENABLED", True)
    out = run(pg, None, "pick_snapshots", pick_snapshots.run, now=AFTER)
    assert out.status == "red" and "sql/011_kalshi_pick_snapshots.sql" in out.red_reasons[0]


def test_archives_pick_time_prices_once_and_replay_uses_them_after_prune(slate, pg, monkeypatch):  # noqa: F811
    _apply(pg)
    monkeypatch.setattr(config, "PICK_SNAPSHOTS_ENABLED", True)
    early = run(pg, None, "pick_snapshots", pick_snapshots.run, now=PICK - timedelta(minutes=5))
    assert early.rows_out == 0                                      # pick time not reached: nothing yet
    out = run(pg, None, "pick_snapshots", pick_snapshots.run, now=AFTER)
    snap = {t: (float(y), float(n)) for t, y, n in pg.execute(
        "select ticker, yes_ask, no_ask from kalshi_pick_snapshots where slate_date = %s", (D,))}
    assert snap == {"TA": (0.60, 0.42), "TE": (0.70, 0.32)}       # not the post-pick 0.95, not stale TB
    assert out.status == "green" and out.rows_out == 2
    again = run(pg, None, "pick_snapshots", pick_snapshots.run, now=AFTER)
    assert again.rows_out == 0                                      # idempotent
    # prune compacts the intraday rows; the replay must now read the archive, not fall back to openings
    pg.execute("delete from kalshi_quotes")
    pg.commit()
    rows = {r["player_id"]: r for r in replay.build(pg, D, D)}
    kinds = {r["quote_kind"] for r in rows.values() if r["yes_ask"] in (0.60, 0.70)}
    assert kinds == {"archived"}


def test_nightly_runs_it_before_prune():
    from prop_worker import nightly
    order = list(nightly.JOBS)
    assert order.index("pick_snapshots") < order.index("prune")
