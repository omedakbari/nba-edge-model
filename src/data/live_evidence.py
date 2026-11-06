"""Free, bounded network diagnostics and append-only contemporaneous snapshots."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import socket
from urllib.parse import urlparse

import requests
from src.utils.slate import utc_now,slate_today


def fetch_snapshot(url, *, timeout=8):
    record=dict(source_url=url,requested_at=utc_now(),ok=False)
    host=urlparse(url).hostname
    try:
        socket.getaddrinfo(host,443)
    except OSError as exc:
        record.update(captured_at=utc_now(),error_kind='dns',error=str(exc))
        return record
    try:
        response=requests.get(url,timeout=timeout,headers={'User-Agent':'NBA-edge-audit/2'})
        record.update(captured_at=utc_now(),http_status=response.status_code,
                      source_http_date=response.headers.get('Date'),
                      source_last_modified=response.headers.get('Last-Modified'),
                      content_sha256=hashlib.sha256(response.content).hexdigest())
        response.raise_for_status()
        record.update(ok=True,payload=response.json())
    except (requests.RequestException,ValueError) as exc:
        record.update(captured_at=utc_now(),error_kind='http_or_transport',error=str(exc))
    return record


def collect(target_date,output_root):
    day=target_date.replace('-','')
    endpoints={
      'scoreboard':f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={day}',
      'injuries':'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries',
      'nba_scoreboard':f'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json'}
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses=dict(zip(endpoints,pool.map(fetch_snapshot,endpoints.values())))
    roster_urls={}
    for event in responses['scoreboard'].get('payload',{}).get('events',[]):
        for competitor in (event.get('competitions') or [{}])[0].get('competitors',[]):
            team=competitor.get('team',{});tid=team.get('id')
            if tid:
                roster_urls[str(tid)]=f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{tid}/roster'
    with ThreadPoolExecutor(max_workers=2) as pool:
        rosters=dict(zip(roster_urls,pool.map(fetch_snapshot,roster_urls.values())))
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
