"""Failures must remain failures across source, process and orchestration boundaries."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pandas as pd
import pytest
import requests
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1]/'scripts')]
import nba
import run_daily
import find_bets as F
import generate_todays_picks as P
from src.data import espn_odds as O, espn_props as E, live_evidence as L
from src.data.errors import SourceError
from src.ingestion import team_stats as T


@pytest.mark.parametrize('failed_step', range(6))
def test_daily_failure_nonzero_summary_and_stops(monkeypatch, capsys, failed_step):
    calls = []
    def child(*a, **kw):
        calls.append(a)
        return SimpleNamespace(returncode=9 if len(calls)-1 == failed_step else 0)
    monkeypatch.setattr(sys, 'argv', ['nba', 'daily'])
    monkeypatch.setattr(nba.subprocess, 'run', child)
    monkeypatch.setattr(nba, 'detect_phase', lambda *a: 'regular')
    monkeypatch.setattr(nba, 'refreshed_today', lambda *a: False)
    assert nba.main() == 9
    assert len(calls) == failed_step + 1
    output = capsys.readouterr().out
    assert 'STEP STATUS SUMMARY' in output and 'FAILED (exit 9)' in output


def test_handler_return_code_reaches_main(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['nba', 'status'])
    monkeypatch.setattr(nba, 'cmd_status', lambda *a: 7)
    assert nba.main() == 7


@pytest.mark.parametrize('failure', ['exception', 'empty', 'missing_team', 'duplicate'])
def test_team_stats_incomplete_and_transport_fail_before_write(monkeypatch, failure):
    monkeypatch.setattr(T, '_team_abbr_map', lambda: {1: 'AAA', 2: 'BBB'})
    frame = pd.DataFrame([dict(team_abbr=t, season='2026-27', pace=100, off_rtg=110,
        def_rtg=110, net_rtg=0, reb_rate=.5, ast_rate=.5) for t in ['AAA', 'BBB']])
    if failure == 'empty': frame = frame.iloc[:0]
    if failure == 'missing_team': frame = frame.iloc[:1]
    if failure == 'duplicate': frame.loc[1, 'team_abbr'] = 'AAA'
    def fetch(*a):
        if failure == 'exception': raise requests.Timeout('timeout')
        return frame
    monkeypatch.setattr(T, 'fetch_team_stats', fetch)
    monkeypatch.setattr(T, 'upsert_team_stats', lambda *a: pytest.fail('no writes'))
    with pytest.raises(SourceError): T.run(['2026-27'], dry_run=True)


@pytest.mark.parametrize('source', ['injuries', 'odds', 'athlete', 'kalshi'])
def test_required_source_transport_failure_raises(monkeypatch, tmp_path, source):
    def fail(*a, **kw): raise requests.Timeout('network timeout')
    monkeypatch.setattr(requests, 'get', fail)
    if source == 'injuries':
        with pytest.raises(SourceError): P.fetch_injury_report()
    elif source == 'odds':
        monkeypatch.setattr(O.time, 'sleep', lambda *a: None)
        with pytest.raises(SourceError): O.fetch_odds('2026-11-06', retries=0)
    elif source == 'kalshi':
        monkeypatch.setattr(F, 'PROJECT_ROOT', tmp_path)
        with pytest.raises(SourceError): F.kalshi_bets([], '2026-11-06')
        archived = list((tmp_path/'data/kalshi_snapshots/2026-11-06').glob('*.json'))
        assert len(archived) == 1 and not json.loads(archived[0].read_text())['ok']
    else:
        monkeypatch.setattr(E, '_get', lambda *a: (_ for _ in ()).throw(E.Blocked('athlete failed')))
        # The same failure is explicit even before hydration.
        with pytest.raises(RuntimeError): E.fetch_book_props('2099-11-06')


def test_successful_empty_odds_is_not_transport_failure(monkeypatch):
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'events': []})
    monkeypatch.setattr(O.requests, 'get', lambda *a, **kw: response)
    assert O.fetch_odds('2026-11-06') == []


@pytest.mark.parametrize('mode', ['dns', 'timeout', 'http'])
def test_preflight_three_attempts_archive_every_failure(monkeypatch, mode):
    monkeypatch.setattr(L.time, 'sleep', lambda *a: None)
    def dns(*a):
        if mode == 'dns': raise OSError('resolver unavailable')
        return []
    def get(*a, **kw):
        if mode == 'timeout': raise requests.Timeout('timeout')
        return SimpleNamespace(status_code=503, headers={}, content=b'error', text='error',
            raise_for_status=lambda: (_ for _ in ()).throw(requests.HTTPError('503')))
    monkeypatch.setattr(L.socket, 'getaddrinfo', dns)
    monkeypatch.setattr(L.requests, 'get', get)
    record = L.fetch_snapshot('https://example.com/api')
    assert not record['ok'] and len(record['attempts']) == 3
    assert all('captured_at' in a and not a['ok'] for a in record['attempts'])


def test_preflight_retry_then_success_retains_first_error(monkeypatch):
    monkeypatch.setattr(L.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(L.socket, 'getaddrinfo', lambda *a: [])
    calls = []
    def get(*a, **kw):
        calls.append(1)
        if len(calls) == 1: raise requests.Timeout('timeout')
        return SimpleNamespace(status_code=200, headers={}, content=b'{}', text='{}',
                               raise_for_status=lambda: None, json=lambda: {})
    monkeypatch.setattr(L.requests, 'get', get)
    r = L.fetch_snapshot('https://example.com/api')
    assert r['ok'] and len(r['attempts']) == 2 and 'error' in r['attempts'][0]
    assert 'error' not in r


def test_athlete_hydration_failure_is_explicit(monkeypatch, tmp_path):
    import config.settings as settings
    monkeypatch.setattr(settings, 'PROJECT_ROOT', tmp_path)
    def get(session, url):
        if 'scoreboard?' in url:
            return {'events': [{'id': '401', 'date': '2099-11-07T00:00Z', 'status': {'type': {'state': 'pre'}}}]}
        raise E.Blocked('athlete transport failed')
    monkeypatch.setattr(E, '_get', get)
    monkeypatch.setattr(E, 'fetch_event', lambda *a: [])
    monkeypatch.setattr(E, 'no_vig_consensus', lambda *a, **kw: {('1', 'pts'): {
        'event_id': '401', 'paired_quotes': []}})
    with pytest.raises(SourceError, match='ESPN athlete 1'): E.fetch_book_props('2099-11-06')


@pytest.mark.parametrize('payload', [{'markets': []}, {'bad': []}])
def test_kalshi_raw_success_and_schema_failures_are_archived(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(F, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(F, 'KALSHI_SERIES', {'KXNBAPTS': 'pts'})
    response = SimpleNamespace(status_code=200, headers={'Date': 'Fri, 06 Nov 2026 20:00:00 GMT'},
        text=json.dumps(payload), raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: response)
    if 'markets' in payload:
        assert F.kalshi_bets([], '2026-11-06') == []
    else:
        with pytest.raises(SourceError): F.kalshi_bets([], '2026-11-06')
    record = json.loads(next((tmp_path/'data/kalshi_snapshots/2026-11-06').glob('*.json')).read_text())
    assert record['payload'] == payload and record['raw_body'] == json.dumps(payload)
