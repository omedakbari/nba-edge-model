"""Merge user-supplied, timestamped paired quotes with existing event identities.

This adapter makes no network calls or paid API requests. Explicitly labeled
sides are required; source URLs are provenance, not an approval assertion.
"""
from collections import defaultdict
from src.data.espn_props import PropRow, no_vig_consensus
from src.betting.quote_quality import anchor_issues


def merge_quotes(book, records, now):
    merged = {key: dict(value) for key, value in book.items()}
    rejected = []
    for record in records:
        try:
            key = (record['player'], record['stat'])
            original = merged[key]
            if (record['event_id'] != original['event_id'] or
                    set(record['teams']) != set(original['teams']) or
                    record['start_time'] != original['start_time'] or
                    not record['source_url'].startswith('https://') or
                    not record['book']):
                raise ValueError('source or event identity mismatch')
            candidate = dict(record, paired_quotes=[record], status='scheduled')
            issues = anchor_issues(candidate, now)
            if issues:
                raise ValueError('; '.join(issues))
            if record['book'] in {q['book'] for q in original['paired_quotes']}:
                raise ValueError('Duplicate bookmaker; not an independent quote')
            rows = []
            for quote in original['paired_quotes'] + [record]:
                for side in ('over','under'):
                    rows.append(PropRow((original['event_id'], '', quote['book'],
                        original['espn_athlete_id'], key[1], side, quote['line'], quote[side],
                        quote['line'], quote[side], quote['updated_at']),
                        side_verified=bool(quote.get('side_mapping_verified', original.get('side_mapping_verified')))))
            # Restrict supplemental quotes to the existing anchor line. Changing
            # the line requires rebuilding the corresponding model probability.
            if record['line'] != original['line']:
                raise ValueError('supplemental line differs from anchor')
            consensus = no_vig_consensus(rows, True, captured_at=now)
            value = consensus[(original['espn_athlete_id'], key[1])]
            if value['n_books'] != len(original['paired_quotes']) + 1:
                raise ValueError('ambiguous, duplicate, or invalid bookmaker pair')
            value.update({k: original[k] for k in ['captured_at','start_time','status','teams']})
            value['captured_at'] = now
            for pair in value['paired_quotes']:
                source = record if pair['book'] == record['book'] else next(
                    q for q in original['paired_quotes'] if q['book'] == pair['book'])
                pair['captured_at'] = source.get('captured_at', original['captured_at'])
                pair['source_url'] = source.get('source_url', 'https://sports.core.api.espn.com')
                pair['side_mapping_verified'] = source.get('side_mapping_verified', original.get('side_mapping_verified',False))
            merged[key] = value
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            rejected.append(dict(player=record.get('player'), reason=str(exc)))
    return merged, rejected
