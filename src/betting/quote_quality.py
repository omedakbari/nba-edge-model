"""Reject inconsistent source times without guessing timezone corrections."""
from datetime import datetime
from src.betting.eligibility import MAX_QUOTE_AGE_SECONDS


def timestamp_issue(updated_at, captured_at):
    try:
        updated = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
        captured = datetime.fromisoformat(captured_at.replace('Z', '+00:00'))
        if updated.tzinfo is None or captured.tzinfo is None:
            return 'missing timezone'
        age = (captured - updated).total_seconds()
        if age < 0:
            return 'future provider timestamp'
        if age > MAX_QUOTE_AGE_SECONDS:
            return 'stale provider timestamp'
    except (TypeError, ValueError, AttributeError):
        return 'missing/invalid provider timestamp'
    return None


def anchor_issues(quote, now):
    issues = []
    captured = quote.get('captured_at')
    issue = timestamp_issue(captured, now)
    if issue:
        issues.append('capture: ' + issue)
    pairs = quote.get('paired_quotes', [])
    if not pairs:
        issues.append('missing paired quotes')
    for pair in pairs:
        pair_capture = pair.get('captured_at', captured)
        capture_issue = timestamp_issue(pair_capture, now)
        if capture_issue:
            issues.append('paired capture: ' + capture_issue)
        issue = timestamp_issue(pair.get('updated_at'), pair_capture)
        if issue:
            issues.append(issue)
    try:
        start = datetime.fromisoformat(quote['start_time'].replace('Z', '+00:00'))
        cutoff = datetime.fromisoformat(now.replace('Z', '+00:00'))
        if start.tzinfo is None or start <= cutoff or quote.get('status') != 'scheduled':
            issues.append('not confirmed pregame')
    except (KeyError, TypeError, ValueError, AttributeError):
        issues.append('missing/invalid start time')
    return sorted(set(issues))
