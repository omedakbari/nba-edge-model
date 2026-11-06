"""Offline, as-of quote audit. Reads original artifacts; writes only an audit directory.

This never fetches historical opening quotes as a substitute for a saved live
forecast, refreshes the database, or writes fair boards/picks/results.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.betting.quote_quality import anchor_issues
from src.data.espn_props import PropRow, no_vig_consensus
from src.data.quote_diagnostics import quote_coverage, timestamp_evidence


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def analyze(snapshot, *, sample_per_reason=10):
    rows = [PropRow(row, side_verified=evidence.get('side_verified', False), raw=evidence.get('raw'))
            for row, evidence in zip(snapshot['rows'], snapshot['row_evidence'], strict=True)]
    captured = snapshot['captured_at']
    rejected = []
    consensus = no_vig_consensus(rows, True, captured_at=captured, rejected=rejected)
    lookup = {(r['event_id'], r['athlete_id'], r['stat'], r['book'], r['side'], r['updated_at']): r['reason']
              for r in rejected}
    candidates = defaultdict(lambda: defaultdict(list))
    mismatches = []
    for index, row in enumerate(rows):
        if (row.raw or {}).get('lastUpdated') != row[-1]:
            mismatches.append(index)
        reason = lookup.get((row[0], row[3], row[4], row[2], row[5], row[-1]))
        if reason:
            sample = dict(row_index=index, event_id=row[0], book=row[2], athlete_id=row[3],
                          stat=row[4], side=row[5], reason=reason,
                          **timestamp_evidence((row.raw or {}).get('lastUpdated'), captured))
            candidates[reason][row[0]].append(sample)
    samples = []
    for reason, events in sorted(candidates.items()):
        # Round-robin events and distinct player/stat pairs, rather than taking
        # both sides of ten otherwise identical observations from one event.
        seen = set()
        selected = []
        for position in range(max(map(len, events.values()))):
            for event in sorted(events):
                if position >= len(events[event]):
                    continue
                sample = events[event][position]
                identity = (event, sample['athlete_id'], sample['stat'])
                if identity in seen:
                    continue
                seen.add(identity)
                selected.append(sample)
                if len(selected) == sample_per_reason:
                    break
            if len(selected) == sample_per_reason:
                break
        samples.extend(selected)
    http = []
    for record in snapshot.get('http_evidence', []):
        entry = {key: record.get(key) for key in
                 ['source_url', 'requested_at', 'captured_at', 'http_status', 'http_date',
                  'age', 'last_modified', 'content_sha256']}
        try:
            server = parsedate_to_datetime(record['http_date']).astimezone(timezone.utc)
            capture = datetime.fromisoformat(record['captured_at']).astimezone(timezone.utc)
            entry['capture_minus_http_date_seconds'] = (capture - server).total_seconds()
        except (KeyError, TypeError, ValueError, AttributeError):
            entry['capture_minus_http_date_seconds'] = None
        http.append(entry)
    reason_counts = Counter(r['reason'] for r in rejected)
    original_counts = Counter(r['reason'] for r in snapshot.get('rejected_quotes', []))
    return dict(
        mode='Saved current-quote replay at original capture time, not a new live recommendation',
        captured_at=captured, coverage=quote_coverage(rows, rejected, consensus),
        original_rejection_counts=dict(original_counts), replay_rejection_counts=dict(reason_counts),
        rejection_counts_match=reason_counts == original_counts,
        raw_timestamp_mismatch_rows=mismatches,
        raw_timestamp_types=dict(Counter(type((row.raw or {}).get('lastUpdated')).__name__ for row in rows)),
        samples=samples, http_evidence=http,
        provider_inventory=snapshot.get('provider_inventory'),
        provider_inventory_limit=('Original odds-index response bodies were not archived; HTTP URLs and '
                                  'parsed rows establish the fetched provider, not every provider advertised upstream.'),
        approved=False, maximum_entry_cost=0.0), consensus


def saved_board_replay(board, market_payload, consensus):
    from find_bets import book_bets
    now = market_payload['generated_at']
    kept = [record for record in board if not anchor_issues(record.get('quote') or {}, now)]
    matches = all((str(record['quote']['espn_athlete_id']), record['stat']) in consensus
                  for record in kept)
    bets = book_bets(kept)
    return dict(
        mode='Saved fair-board probabilities and sportsbook prices, checked at original forecast time',
        eligible_props=len(kept), anchor_books=sorted({pair['book'] for record in kept
                                                    for pair in record['quote']['paired_quotes']}),
        all_saved_board_anchors_in_replayed_consensus=matches,
        sportsbook_sides_priced=len(bets), sportsbook_ev_candidates=sum(bet['ev'] >= .04 for bet in bets),
        original_selected_candidates=len(market_payload['picks']),
        original_price_sources=market_payload.get('price_sources'),
        limitations=['Does not rerun models or reconstruct unarchived Kalshi responses',
                     'Does not claim saved quotes are currently executable'],
        approved=False, maximum_entry_cost=0.0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--fair-board', type=Path)
    parser.add_argument('--market-picks', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if bool(args.fair_board) != bool(args.market_picks):
        parser.error('--fair-board and --market-picks must be supplied together')
    # A distinct directory protects original evidence even on repeated audits.
    args.output_dir.mkdir(parents=True, exist_ok=False)
    inputs = [args.snapshot]
    if args.fair_board:
        inputs += [args.fair_board, args.market_picks]
    before = {str(path.resolve()): sha256(path) for path in inputs}
    report, consensus = analyze(json.loads(args.snapshot.read_text()))
    if args.fair_board:
        report['saved_board_replay'] = saved_board_replay(
            json.loads(args.fair_board.read_text()), json.loads(args.market_picks.read_text()), consensus)
    report['input_hashes'] = before
    report['inputs_unchanged'] = all(sha256(Path(path)) == value for path, value in before.items())
    (args.output_dir / 'audit.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    samples = report['samples']
    with (args.output_dir / 'rejected_quote_samples.csv').open('w', newline='') as stream:
        fields = list(dict.fromkeys(key for sample in samples for key in sample))
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(samples)
    print(json.dumps({key: report[key] for key in
                      ['coverage', 'replay_rejection_counts', 'rejection_counts_match',
                       'raw_timestamp_mismatch_rows', 'inputs_unchanged']}, indent=2))
    if 'saved_board_replay' in report:
        print(json.dumps(report['saved_board_replay'], indent=2))
    print(f'Audit evidence: {args.output_dir.resolve()}')


if __name__ == '__main__':
    main()
