"""DK-via-ESPN freshness (AUDIT_2026-11-24.md item 1): strict rejection stays, every rejection carries a
reason with the number that failed, and the window / future skew are config values."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.betting import quote_quality as Q  # noqa: E402
from src.data.quote_diagnostics import feed_summary, quote_coverage  # noqa: E402
from src.data.espn_props import no_vig_consensus  # noqa: E402

CAP = '2026-11-24T22:41:13.615877+00:00'


@pytest.mark.parametrize('updated,issue', [
    ('2026-11-24T22:40Z', None),                               # 1 min old, ESPN's real format
    ('2026-11-24T17:40:00-05:00', None),                       # same instant with an offset
    ('2026-11-24T06:44Z', 'stale provider timestamp'),         # a real 16 h-old DK side from today
    ('2026-11-25T00:45Z', 'future provider timestamp'),        # +124 min
    ('2026-11-24T22:40:00', 'missing timezone'),               # naive: never assumed UTC
    (1795992000000, 'missing/invalid provider timestamp'),     # epoch ms: rejected, never reinterpreted
    ('1795992000000', 'missing/invalid provider timestamp'),
    (None, 'missing/invalid provider timestamp'),
])
def test_strict_parsing(updated, issue):
    assert Q.timestamp_issue(updated, CAP) == issue


def test_rejection_detail_names_age_or_skew():
    assert Q.rejection_detail('2026-11-24T06:44Z', CAP) == 'stale: age 957.2 min'
    assert Q.rejection_detail('2026-11-25T00:45Z', CAP) == 'future: skew +123.8 min'
    assert Q.rejection_detail('2026-11-24T22:40Z', CAP) is None
    assert Q.rejection_detail('2026-11-24T22:40:00', CAP).startswith('missing timezone')


def test_window_and_skew_are_configurable():
    assert Q.timestamp_issue('2026-11-24T22:20Z', CAP) == 'stale provider timestamp'          # 21 min > 15
    assert Q.timestamp_issue('2026-11-24T22:20Z', CAP, max_age=1800) is None
    assert Q.timestamp_issue('2026-11-24T22:42Z', CAP) == 'future provider timestamp'          # +47 s, skew 0
    assert Q.timestamp_issue('2026-11-24T22:42Z', CAP, max_future_skew=60) is None
    from config import settings
    assert settings.QUOTE_MAX_AGE_SECONDS == 900 and settings.QUOTE_MAX_FUTURE_SKEW_SECONDS == 0


def _row(upd, side='over', aid='1'):
    # (event_id, date, provider, athlete_id, stat, side, line_open, price_open, line_cur, price_cur, updated_at)
    return ('e1', '2026-11-24', 'Draft Kings', aid, 'pts', side, 20.5, 1.9, 20.5, 1.9, upd)


def test_rejected_sides_carry_detail_and_summary_reads_like_the_log():
    rows = [_row('2026-11-24T06:44Z', 'over', str(i)) for i in range(4)] + \
           [_row('2026-11-25T00:45Z', 'under', str(i)) for i in range(2)]
    rejected = []
    cons = no_vig_consensus(rows, True, captured_at=CAP, rejected=rejected)
    assert cons == {} and len(rejected) == 6
    assert {r['detail'] for r in rejected} == {'stale: age 957.2 min', 'future: skew +123.8 min'}
    cov = quote_coverage(rows, rejected, cons)
    assert feed_summary(cov) == {'Draft Kings': 'DK via ESPN: 0/6 passed (4 stale, 2 future-skew)'}


def test_summary_on_todays_real_counts():
    cov = {'books': {'Draft Kings': {'offered_sides': 506, 'timestamp_pass_sides': 0,
                                     'timestamp_rejections': {'future provider timestamp': 100,
                                                              'stale provider timestamp': 406}}}}
    assert feed_summary(cov)['Draft Kings'] == 'DK via ESPN: 0/506 passed (406 stale, 100 future-skew)'
