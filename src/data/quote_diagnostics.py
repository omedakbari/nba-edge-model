"""Observability only: explain source timestamps without correcting or accepting them."""
from collections import Counter
from datetime import datetime, timezone


def timestamp_evidence(updated_at, captured_at):
    """Describe the existing ISO-only parser. Never assume a missing timezone.

    Epoch numbers are deliberately unsupported by the current quote contract;
    this diagnostic must not turn rejected inputs into accepted quotes.
    """
    result = dict(raw_upstream_timestamp=updated_at, parsed_utc=None,
                  source_timezone=None, assumed_timezone=None,
                  comparison_now=captured_at, comparison_now_utc=None,
                  comparison_timezone=None, age_seconds=None)
    parsed = {}
    for label, value in [('source', updated_at), ('comparison', captured_at)]:
        try:
            instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if instant.tzinfo is None:
                result[label + '_parse_error'] = 'missing timezone; no timezone assumed'
                continue
            parsed[label] = instant.astimezone(timezone.utc)
            result['source_timezone' if label == 'source' else 'comparison_timezone'] = str(instant.tzinfo)
            result['parsed_utc' if label == 'source' else 'comparison_now_utc'] = parsed[label].isoformat()
        except (TypeError, ValueError, AttributeError) as exc:
            result[label + '_parse_error'] = type(exc).__name__ + ': ' + str(exc)
    if len(parsed) == 2:
        result['age_seconds'] = (parsed['comparison'] - parsed['source']).total_seconds()
    return result


def quote_coverage(rows, rejected, consensus, *, timestamp_check_applied=True):
    """Count offered/rejected sides and selected book pairs, without changing selection."""
    offered = Counter(row[2] or '<missing book>' for row in rows)
    reasons = {book: Counter() for book in offered}
    for item in rejected:
        reasons.setdefault(item.get('book') or '<missing book>', Counter())[item['reason']] += 1
    pairs = Counter(pair['book'] for value in consensus.values() for pair in value['paired_quotes'])
    return {
        'offered_sides': len(rows),
        'timestamp_rejected_sides': len(rejected),
        'timestamp_check_applied': timestamp_check_applied,
        'consensus_props': len(consensus),
        'books': {
            book: dict(offered_sides=offered[book], timestamp_rejections=dict(sorted(reasons[book].items())),
                       timestamp_pass_sides=(offered[book] - sum(reasons[book].values())
                                             if timestamp_check_applied else None),
                       selected_consensus_pairs=pairs[book])
            for book in sorted(offered)
        },
    }


def print_quote_coverage(coverage):
    for book, counts in coverage['books'].items():
        reasons = ', '.join(f'{key}={value}' for key, value in counts['timestamp_rejections'].items()) or 'none'
        timestamp_pass = counts['timestamp_pass_sides'] if coverage['timestamp_check_applied'] else 'unchecked (historical opening)'
        print(f"  Quote source {book}: offered={counts['offered_sides']} sides; "
              f"timestamp rejected: {reasons}; timestamp pass={timestamp_pass}; "
              f"selected pairs={counts['selected_consensus_pairs']}")
