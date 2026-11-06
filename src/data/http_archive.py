"""Archive raw JSON/body and errors before returning a required source response."""
import json
from pathlib import Path
import requests
from src.data.errors import SourceError
from src.utils.slate import revision_path, utc_now


def archived_json(url, *, params=None, timeout=15, directory):
    record = dict(source_url=url, params=params, requested_at=utc_now(), ok=False)
    error = None
    try:
        response = requests.get(url, params=params, timeout=timeout)
        record.update(captured_at=utc_now(), http_status=response.status_code,
                      http_date=getattr(response, 'headers', {}).get('Date'),
                      raw_body=getattr(response, 'text', None))
        response.raise_for_status()
        record['payload'] = response.json()
        if not isinstance(record['payload'], dict):
            raise ValueError('Expected a JSON object')
        record['ok'] = True
    except (requests.RequestException, ValueError) as exc:
        record.update(captured_at=utc_now(), error=str(exc))
        error = exc
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = revision_path(directory / (record['requested_at'].replace(':', '') + '.json'))
    path.write_text(json.dumps(record, indent=2))
    if error:
        raise SourceError(str(error), source=url) from error
    return record
