"""Free, bounded network diagnostics and append-only contemporaneous snapshots."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import socket
import time
from urllib.parse import urlparse

import requests
from src.utils.slate import utc_now,slate_today


def fetch_snapshot(url, *, timeout=8, attempts=3, backoff=1):
    """Bounded attempts; every response or failure remains in the archive."""
    record = dict(source_url=url, requested_at=utc_now(), ok=False, attempts=[])
    for attempt in range(attempts):
        item = dict(attempt=attempt + 1, requested_at=utc_now(), ok=False)
        stage = 'dns'
        try:
            socket.getaddrinfo(urlparse(url).hostname, 443)
            stage = 'http_or_transport'
            response = requests.get(url, timeout=timeout, headers={'User-Agent': 'NBA-edge-audit/2'})
            item.update(captured_at=utc_now(), http_status=response.status_code,
                        source_http_date=response.headers.get('Date'),
                        source_last_modified=response.headers.get('Last-Modified'),
                        content_sha256=hashlib.sha256(response.content).hexdigest(),
                        raw_body=response.text)
            response.raise_for_status()
            item.update(ok=True, payload=response.json())
        except (requests.RequestException, ValueError, OSError) as exc:
            item.update(captured_at=utc_now(), error_kind=stage, error=str(exc))
        record['attempts'].append(item)
        record.update({k: v for k, v in item.items() if k != 'attempt'})
        if item['ok']:
            record.pop('error', None); record.pop('error_kind', None)
            break
        status = item.get('http_status')
        if status and status < 500 and status not in (408, 429):
            break
        if attempt + 1 < attempts:
            time.sleep(backoff * 2 ** attempt)
    return record


def collect(target_date,output_root):
    day=target_date.replace('-','')
    endpoints={
      'scoreboard':f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={day}',
      'injuries':'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries',
      'nba_scoreboard':f'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json'}
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses=dict(zip(endpoints,pool.map(fetch_snapshot,endpoints.values())))
    from src.data.context_archive import archive, ESPN_TEAM_IDS
    for name, record in responses.items():
        if name == 'injuries' and record['ok']:
            archive('injuries', 'league', record['payload'], record['captured_at'], source=record['source_url'])
    roster_urls = ({tid: f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster'
                    for tid in ESPN_TEAM_IDS.values()} if all(responses[k]['ok'] for k in ('scoreboard', 'injuries')) else {})
    for event in responses['scoreboard'].get('payload',{}).get('events',[]) if roster_urls else []:
        for competitor in (event.get('competitions') or [{}])[0].get('competitors',[]):
            team=competitor.get('team',{});tid=team.get('id')
            if tid:
                roster_urls[str(tid)]=f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster'
    with ThreadPoolExecutor(max_workers=2) as pool:
        rosters=dict(zip(roster_urls,pool.map(fetch_snapshot,roster_urls.values())))
    for tid, record in rosters.items():
        archive('espn_roster', tid, record.get('payload', {'error': record.get('error')}), record['captured_at'],
                source=record['source_url'], ok=record['ok'])
    report=dict(target_date=target_date,captured_at=utc_now(),responses=responses,rosters=rosters,
                live_api_ready=all(responses[k]['ok'] for k in ('scoreboard','injuries')),
                # Neither absence of a response nor a failed lookup proves no slate.
                approved_picks=[],maximum_entry_cost=0.0)
    out=Path(output_root)/utc_now().replace(':','').replace('+','_')
    out.mkdir(parents=True,exist_ok=False)
    (out/'live_evidence.json').write_text(json.dumps(report,indent=2))
    return report,out


def pregame_event(event,now=None):
    now=now or datetime.now(timezone.utc)
    try:
        start=datetime.fromisoformat(event['date'].replace('Z','+00:00'))
        return start.tzinfo is not None and start>now and event['status']['type']['state']=='pre'
    except (KeyError,ValueError,TypeError):
        return False
