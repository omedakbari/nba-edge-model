"""Immutable fetch-time contexts; historical queries never call a current-roster API."""
import json
from datetime import datetime, timezone
from pathlib import Path
from config.settings import PROJECT_ROOT
from src.data.errors import SourceError
from src.utils.slate import revision_path

ESPN_TEAM_IDS = dict(zip(
    ['ATL','BOS','NOP','CHI','CLE','DAL','DEN','DET','GSW','HOU','IND','LAC','LAL','MIA','MIL',
     'MIN','BKN','NYK','ORL','PHI','PHX','POR','SAC','SAS','OKC','UTA','WAS','TOR','MEM','CHA'],
    map(str, range(1, 31))))


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Explicit timezone required')
    return parsed.astimezone(timezone.utc)


def archive(kind, identity, payload, captured_at, *, source, season=None, ok=True, root=None):
    directory = Path(root or PROJECT_ROOT / 'data/asof_context') / kind / str(identity)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = instant(captured_at).strftime('%Y%m%dT%H%M%S%fZ')
    path = revision_path(directory / f'{stamp}.json')
    path.write_text(json.dumps(dict(kind=kind, identity=str(identity), captured_at=captured_at,
        source=source, season=season, ok=ok, payload=payload), indent=2))
    return path


def latest(kind, identity, cutoff, *, season=None, root=None):
    cutoff = instant(cutoff)
    records = []
    for path in (Path(root or PROJECT_ROOT / 'data/asof_context') / kind / str(identity)).glob('*.json'):
        record = json.loads(path.read_text())
        if record.get('ok') and instant(record['captured_at']) <= cutoff and (season is None or record.get('season') == season):
            records.append(record)
    if not records:
        raise SourceError(f'No archived {kind}/{identity} at or before {cutoff.isoformat()}', kind='asof_unavailable')
    return max(records, key=lambda r: instant(r['captured_at']))
