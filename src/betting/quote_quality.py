"""Reject inconsistent source times without guessing timezone corrections.

Strict by design (AUDIT_2026-11-24.md, item 1): ISO-8601 with an explicit offset only. Naive strings and
epoch numbers are rejected, never reinterpreted. Window and allowed future skew come from
config.settings (QUOTE_MAX_AGE_SECONDS, QUOTE_MAX_FUTURE_SKEW_SECONDS).
"""
from datetime import datetime

from config.settings import QUOTE_MAX_AGE_SECONDS, QUOTE_MAX_FUTURE_SKEW_SECONDS
from src.betting.eligibility import MAX_QUOTE_AGE_SECONDS  # noqa: F401  (same value; kept for importers)


def _age_seconds(updated_at, captured_at):
    updated = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
    captured = datetime.fromisoformat(captured_at.replace('Z', '+00:00'))
    if updated.tzinfo is None or captured.tzinfo is None:
        return None
    return (captured - updated).total_seconds()


def timestamp_issue(updated_at, captured_at, *, max_age=None, max_future_skew=None):
    max_age = QUOTE_MAX_AGE_SECONDS if max_age is None else max_age
    max_future_skew = QUOTE_MAX_FUTURE_SKEW_SECONDS if max_future_skew is None else max_future_skew
    try:
        age = _age_seconds(updated_at, captured_at)
        if age is None:
            return 'missing timezone'
        if age < -max_future_skew:
            return 'future provider timestamp'
        if age > max_age:
            return 'stale provider timestamp'
    except (TypeError, ValueError, AttributeError):
        return 'missing/invalid provider timestamp'
    return None


def rejection_detail(updated_at, captured_at):
    """Short, human reason with the number that failed: 'stale: age 957.2 min' / 'future: skew +124.0 min'."""
    issue = timestamp_issue(updated_at, captured_at)
    if issue is None:
        return None
    if issue in ('stale provider timestamp', 'future provider timestamp'):
        age = _age_seconds(updated_at, captured_at) / 60
        return f'stale: age {age:.1f} min' if age > 0 else f'future: skew +{-age:.1f} min'
    return f'{issue}: {updated_at!r}'


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
