"""Timestamp audit and source reporting must not relax quote selection."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from src.betting.quote_quality import timestamp_issue
from src.data import espn_props as EP
from src.data.quote_diagnostics import timestamp_evidence, quote_coverage, print_quote_coverage
from audit_quote_timestamps import analyze
import find_bets as F


NOW = '2026-11-06T18:32:23.203700+00:00'


@pytest.mark.parametrize(('value', 'reason', 'utc'), [
    ('2026-11-06T18:24:00', 'missing timezone', None),
    ('2026-11-06T10:24:00-08:00', None, '2026-11-06T18:24:00+00:00'),
    ('2026-11-06T18:24Z', None, '2026-11-06T18:24:00+00:00'),
    (1793989440, 'missing/invalid provider timestamp', None),
    (1793989440000, 'missing/invalid provider timestamp', None),
    ('2026-11-06T20:46Z', 'future provider timestamp', '2026-11-06T20:46:00+00:00'),
    ('2026-11-06T13:54Z', 'stale provider timestamp', '2026-11-06T13:54:00+00:00'),
])
def test_formats_keep_existing_acceptance_contract(value, reason, utc):
    assert timestamp_issue(value, NOW) == reason
    evidence = timestamp_evidence(value, NOW)
    assert evidence['parsed_utc'] == utc
    assert evidence['assumed_timezone'] is None
    assert evidence['comparison_now_utc'] == NOW
    assert evidence['comparison_timezone'] == 'UTC'


def test_dst_fall_back_uses_explicit_offsets_not_local_wall_clock():
    now = '2026-11-01T01:05:00-08:00'
    fresh = '2026-11-01T01:55:00-07:00'
    assert timestamp_issue(fresh, now) is None  # ten elapsed minutes across the repeated hour
    evidence = timestamp_evidence(fresh, now)
    assert evidence['age_seconds'] == 600
    assert evidence['parsed_utc'] == '2026-11-01T08:55:00+00:00'
    assert evidence['comparison_now_utc'] == '2026-11-01T09:05:00+00:00'
    assert timestamp_issue('2026-11-01T01:55:00-08:00', now) == 'future provider timestamp'


def test_timestamp_thresholds_unchanged():
    assert timestamp_issue('2026-11-06T18:17:23.203700Z', NOW) is None
    assert timestamp_issue('2026-11-06T18:17:23.203699Z', NOW) == 'stale provider timestamp'
    assert timestamp_issue('2026-11-06T18:32:23.203701Z', NOW) == 'future provider timestamp'


def pair(book, stamp, aid='1'):
    return [EP.PropRow(('401', '2026-11-06', book, aid, 'pts', side, 20.5, 1.9, 20.5, 1.9, stamp),
                      raw={'lastUpdated': stamp}) for side in ('over', 'under')]


def test_per_book_rejections_include_missing_times_and_preserve_consensus(capsys):
    rows = pair('Fresh', '2026-11-06T18:24Z') + pair('Bad', '2026-11-06T20:46Z')
    rows += pair('Bad', '2026-11-06T13:54Z', '2') + pair('Missing', None)
    rejected = []
    result = EP.no_vig_consensus(rows, True, captured_at=NOW, rejected=rejected)
    assert len(result) == 1 and result[('1', 'pts')]['n_books'] == 1
    assert result[('1', 'pts')]['paired_quotes'][0]['book'] == 'Fresh'
    counts = quote_coverage(rows, rejected, result)
    assert counts['books']['Bad']['timestamp_rejections'] == {
        'future provider timestamp': 2, 'stale provider timestamp': 2}
    assert counts['books']['Missing']['timestamp_rejections'] == {'missing/invalid provider timestamp': 2}
    assert counts['books']['Fresh']['selected_consensus_pairs'] == 1
    assert rejected[0]['timestamp_evidence']['raw_upstream_timestamp'] == '2026-11-06T20:46Z'
    print_quote_coverage(counts)
    assert 'Quote source Bad' in capsys.readouterr().out


def test_provider_inventory_records_all_advertised_books_and_live_filter(monkeypatch):
    session = SimpleNamespace(quote_provider_evidence=[])
    def get(_session, url):
        if url.endswith('/odds'):
            return {'items': [{'provider': {'id': i, 'name': name}}
                              for i, name in [(100, 'Draft Kings'), (200, 'Other'), (300, 'Live Book')]]}
        return {'items': []}
    monkeypatch.setattr(EP, '_get', get)
    assert EP._fetch_event(session, '401', '2026-11-06') == []
    assert len(session.quote_provider_evidence) == 3
    assert [r['provider']['name'] for r in session.quote_provider_evidence] == ['Draft Kings', 'Other', 'Live Book']
    assert session.quote_provider_evidence[-1]['skipped_live_provider']
    assert session.quote_provider_evidence[1]['returned_prop_items'] == 0


def test_live_fetch_archives_diagnostics_without_overwriting_original(monkeypatch, tmp_path, capsys):
    import config.settings as settings
    monkeypatch.setattr(settings, 'PROJECT_ROOT', tmp_path)
    event = {'id': '401', 'date': '2099-11-07T00:00Z', 'status': {'type': {'state': 'pre'}}}
    def get(session, url):
        if 'scoreboard?' in url:
            return {'events': [event]}
        return {'fullName': 'Player'}
    monkeypatch.setattr(EP, '_get', get)
    def fetch(session, eid, day):
        session.quote_provider_evidence.append({'event_id': eid, 'provider': {'name': 'Bad'}})
        return pair('Bad', '2099-11-06T20:46Z')
    monkeypatch.setattr(EP, 'fetch_event', fetch)
    directory = tmp_path / 'data/quote_snapshots'
    directory.mkdir(parents=True)
    original = directory / '2099-11-06.json'
    original.write_text('original')
    assert EP.fetch_book_props('2099-11-06') == {}
    revisions = list(directory.glob('*.revision-*.json'))
    assert len(revisions) == 1 and original.read_text() == 'original'
    saved = json.loads(revisions[0].read_text())
    assert saved['quote_coverage']['books']['Bad']['timestamp_rejections'] == {'future provider timestamp': 2}
    assert saved['rejected_quotes'][0]['timestamp_evidence']['source_timezone'] == 'UTC'
    assert saved['provider_inventory'][0]['provider']['name'] == 'Bad'
    assert 'Quote source Bad' in capsys.readouterr().out


def test_offline_audit_keeps_raw_evidence_and_samples_both_reasons():
    rows = pair('Book', '2026-11-06T20:46Z') + pair('Book', '2026-11-06T13:54Z', '2')
    rejected = []
    EP.no_vig_consensus(rows, True, captured_at=NOW, rejected=rejected)
    snapshot = dict(rows=rows, row_evidence=[{'raw': r.raw} for r in rows],
                    captured_at=NOW, rejected_quotes=rejected)
    report, consensus = analyze(snapshot, sample_per_reason=1)
    assert not consensus and report['rejection_counts_match']
    assert len(report['samples']) == 2
    assert not report['raw_timestamp_mismatch_rows']
    assert report['maximum_entry_cost'] == 0


def test_empty_candidates_explain_sources_and_preserve_original(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(F, 'MARKET_PICKS_DIR', tmp_path)
    original = tmp_path / '2026-11-06.json'
    original.write_text('original forecast')
    board = [{'quote': {'paired_quotes': [{'book': 'Draft Kings'}]}}] * 7
    path = F.save_market_picks([], board, '2026-11-06', '2026-27', 'regular')
    payload = json.loads(path.read_text())
    assert payload['price_sources'] == [] and payload['picks'] == []
    assert payload['scan_summary']['eligible_props'] == 7
    assert payload['scan_summary']['anchor_books'] == ['Draft Kings']
    assert 'price_sources=[] is expected' in capsys.readouterr().out
    assert original.read_text() == 'original forecast'
