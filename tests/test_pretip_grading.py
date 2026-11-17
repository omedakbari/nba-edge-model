"""log_team_results grades each game from the newest pick revision saved before that game's tip."""
import json
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'scripts')]
import log_team_results as G

D = '2026-11-17'


def save(tmp, name, generated, picks):
    (tmp / name).write_text(json.dumps({'date': D, 'generated_at': generated, 'picks': picks}))


def g(gid, tip, tier):
    return {'game_id': gid, 'home': 'SAS', 'away': 'DET', 'start_time': tip, 'tier': tier}


def test_newest_revision_before_each_tip_wins(tmp_path):
    early, late, never = '2026-11-18T00:00Z', '2026-11-18T03:30Z', '2026-11-17T15:00Z'
    save(tmp_path, f'{D}.json', '2026-11-17T20:24:00+00:00',                       # 12:24 pm PT, pre-guard
         [g('1', early, 'LEAN'), g('2', late, 'LEAN'), g('3', never, 'LEAN')])
    save(tmp_path, f'{D}.revision-20261117T233000Z.json', '2026-11-17T23:30:00+00:00',   # rerun before 1st tip
         [g('1', early, 'MONITOR'), g('2', late, 'MONITOR'), g('3', never, 'MONITOR')])
    save(tmp_path, f'{D}.revision-20261118T010000Z.json', '2026-11-18T01:00:00+00:00',   # after game 1 tipped
         [g('1', early, 'STRONG'), g('2', late, 'STRONG'), g('3', never, 'STRONG')])
    picks, skipped, used = G.pretip_picks(tmp_path, D)
    by = {p['game_id']: p['tier'] for p in picks}
    assert by == {'1': 'MONITOR', '2': 'STRONG'}            # 1: last pre-tip rerun; 2: its tip is later
    assert [p['game_id'] for p in skipped] == ['3']         # every revision was after its tip: not graded
    assert used == {f'{D}.revision-20261117T233000Z.json', f'{D}.revision-20261118T010000Z.json'}


def test_single_original_file_still_grades(tmp_path):
    save(tmp_path, f'{D}.json', '2026-11-17T20:24:00+00:00', [g('1', '2026-11-18T00:00Z', 'LEAN')])
    picks, skipped, used = G.pretip_picks(tmp_path, D)
    assert [p['tier'] for p in picks] == ['LEAN'] and skipped == [] and used == {f'{D}.json'}


def test_missing_tip_time_or_generated_at_is_tolerated(tmp_path):
    (tmp_path / f'{D}.json').write_text(json.dumps({'picks': [{'game_id': '1', 'tier': 'LEAN'}]}))
    picks, skipped, _ = G.pretip_picks(tmp_path, D)
    assert [p['tier'] for p in picks] == ['LEAN'] and skipped == []
