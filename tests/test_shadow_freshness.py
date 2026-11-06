"""Shadow freshness may explain supply, but must never relax production gates."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.data.shadow_freshness import compare
from src.data.espn_props import no_vig_consensus

NOW='2026-11-06T18:32:23.203700Z'
SCORE={'events':[dict(id='401',date='2026-11-07T00:00Z',status={'type':{'state':'pre'}})]}
HTTP=[dict(source_url='https://example.com/events/401/competitions/401/odds/100/propBets?limit=1000',
    http_status=200,http_date='Fri, 06 Nov 2026 18:32:22 GMT',captured_at='2026-11-06T18:32:22.5Z')]


def pair(stamp):
    return [('401','2026-11-06','Book','9','pts',side,20.5,1.9,20.5,1.9,stamp) for side in ('over','under')]


def test_live_feed_can_admit_old_last_change_only_in_shadow():
    rows=pair('2026-11-06T10:00Z')
    r=compare(rows,SCORE,HTTP,NOW)
    assert r['current_paired_props']==0 and r['shadow_paired_props']==1
    assert not no_vig_consensus(rows,True,captured_at=NOW)
    assert r['approved'] is False and r['maximum_entry_cost']==0


def test_future_within_tolerance_is_always_flagged_and_beyond_rejected():
    r=compare(pair('2026-11-06T18:33Z'),SCORE,HTTP,NOW)
    assert r['shadow_paired_props']==1 and r['future_anomaly_sides']==2
    r=compare(pair('2026-11-06T18:34Z'),SCORE,HTTP,NOW)
    assert r['shadow_paired_props']==0 and r['future_anomaly_sides']==2


def test_missing_http_or_old_capture_cannot_enter_shadow():
    assert compare(pair('2026-11-06T10:00Z'),SCORE,[],NOW)['shadow_paired_props']==0
    old=[dict(HTTP[0],captured_at='2026-11-06T18:20:00Z',http_date='Fri, 06 Nov 2026 18:20:00 GMT')]
    assert compare(pair('2026-11-06T10:00Z'),SCORE,old,NOW)['shadow_paired_props']==0


def test_started_game_and_invalid_timezone_cannot_enter_shadow():
    started={'events':[dict(SCORE['events'][0],date='2026-11-06T18:00Z')]}
    assert compare(pair('2026-11-06T18:24Z'),started,HTTP,NOW)['shadow_paired_props']==0
    assert compare(pair('2026-11-06T18:24'),SCORE,HTTP,NOW)['shadow_paired_props']==0


def test_stats_and_books_have_separate_counts():
    rows=pair('2026-11-06T10:00Z')+[tuple(list(r[:2])+['Second']+list(r[3:4])+['reb']+list(r[5:])) for r in pair('2026-11-06T18:24Z')]
    result=compare(rows,SCORE,HTTP,NOW)
    assert {(g['book'],g['stat']) for g in result['by_book_stat']}=={('Book','pts'),('Second','reb')}
