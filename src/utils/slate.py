"""Slate dates are Pacific; persisted instants are timezone-aware UTC."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo('America/Los_Angeles')


def slate_today():
    return datetime.now(PACIFIC).date()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def revision_path(path):
    """Never replace a previously saved prediction, including an empty rerun."""
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        return path.with_name(f'{path.stem}.revision-{stamp}{path.suffix}')
    return path


def latest_revision(path):
    """Latest live view; grading/research can still explicitly use the original."""
    candidates = list(path.parent.glob(f'{path.stem}.revision-*{path.suffix}'))
    if path.exists():
        candidates.append(path)
    return max(candidates, key=lambda p: p.stat().st_mtime_ns) if candidates else path
