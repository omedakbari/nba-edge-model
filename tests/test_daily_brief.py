"""daily_brief: four sections, INJURY_BLIND shown, missing inputs don't fail, stays under 8 KB."""
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'scripts')]
import daily_brief as B

DAY = date(2026, 11, 17)
REPORT = """BETS REPORT last 7 day(s) through 2026-11-17  (research-only; ROI = flat 1u where payout known)
                     bets     W-L   hit push/void open      ROI (n)
  ALL                  52   9-10    47%         0   33   -56.3% (9)
 by platform
  kalshi               24   3-7     30%         0   14   -56.3% (9)
 by stat
  reb                  22   8-7     53%         0    7   -34.5% (6)
 by p bucket
  .60-.70              20   0-0     -           0   20            -
 by placed
  placed                2   0-2      0%         0    0            -
 calibration: no graded bets with a p yet"""
HEADER = ['grade_date', 'game_date', 'game_id', 'home', 'away', 'tier', 'spread_pick_side', 'spread_pick_team',
          'market_spread', 'actual_margin', 'spread_result', 'ou_tier', 'ou_direction', 'over_under', 'actual_total',
          'ou_result']


def fake_run(stdout=REPORT, rc=0):
    return lambda *a, **k: SimpleNamespace(returncode=rc, stdout=stdout, stderr='connection refused')


def world(tmp_path, card_rows=3, blind=True):
    picks = tmp_path / 'data/team_picks'
    cards = tmp_path / 'data/cards'
    picks.mkdir(parents=True)
    cards.mkdir(parents=True)
    rows = [HEADER,
            ['2026-11-17', '2026-11-16', '1', 'NOP', 'GSW', 'LEAN', 'away', 'GSW', '3.5', '21', 'LOSS',
             'STRONG', 'OVER', '223.5', '209', 'LOSS'],
            ['2026-11-17', '2026-11-16', '2', 'BOS', 'ORL', 'NONE', '', '', '-8.5', '-10', 'NO_PICK',
             'LEAN', 'OVER', '212.5', '220', 'WIN'],
            ['2026-11-16', '2026-11-15', '3', 'LAC', 'CHA', 'LEAN', 'home', 'LAC', '1.5', '8', 'WIN',
             'NONE', '', '', '', 'NO_PICK']]
    (picks / 'results_log.csv').write_text('\n'.join(','.join(r) for r in rows) + '\n')
    game = {'home': 'SAS', 'away': 'DET', 'tier': 'MONITOR', 'ou_tier': 'NONE', 'market_spread': -9.5,
            'spread_pick_team': None, 'spread_pick_side': None}
    if blind:
        game['injury_blind'] = {'teams': {'DET': {'reason': 'Duncan Robinson top-2 scorer; 29% of team points',
                                                  'players': [{'name': 'Duncan Robinson'}, {'name': 'Taurean Prince'}]}},
                                'source': 'worker injuries', 'before_guard': {'tier': 'LEAN', 'ou_tier': 'NONE'}}
    ok = {'home': 'TOR', 'away': 'NYK', 'tier': 'NONE', 'ou_tier': 'LEAN', 'ou_direction': 'OVER', 'over_under': 214.5}
    (picks / f'{DAY}.json').write_text(json.dumps({'picks': [ok, game]}))
    body = [f'4:30p  NYK@TOR Player {i:<15} REB  YES     10+ 0.84   78c/79c' for i in range(card_rows)]
    card = ['CARD 2026-11-17  RESEARCH-ONLY (UNVALIDATED)', 'p_blend ...', 'tip PT game ...', '-' * 76, *body,
            f'{card_rows} shown; hidden: ...', 'price/max: ...']
    (cards / f'{DAY}.txt').write_text('\n'.join(card) + '\n')
    return tmp_path


def test_brief_has_all_four_sections(tmp_path):
    text = B.build(DAY, root=world(tmp_path), run=fake_run())
    assert text.index('== YESTERDAY 2026-11-16') < text.index('== BETS') < text.index('== TODAY 2026-11-17') \
        < text.index('== CARD ==')
    assert 'GSW@NOP  LEAN GSW -3.5 LOSS (home by 21) | STRONG OVER 223.5 LOSS (209)' in text
    assert 'ORL@BOS  LEAN OVER 212.5 WIN (220)' in text
    assert 'LAC' not in text.split('== BETS')[0]                      # 11/15 game is not yesterday
    assert 'record: spread 0-1, total 1-1 (2 games graded)' in text
    assert 'RESEARCH-ONLY (UNVALIDATED)' in text and len(text.encode()) < B.MAX_BYTES


def test_bets_section_keeps_overall_platform_p_bucket_placed_and_calibration_only():
    keep = '\n'.join(B.bets_summary(fake_run()))
    for wanted in ('BETS REPORT', 'ALL', ' by platform', 'kalshi', ' by p bucket', ' by placed', 'calibration'):
        assert wanted in keep
    assert ' by stat' not in keep and 'reb ' not in keep
    assert 'bets report failed' in '\n'.join(B.bets_summary(fake_run(rc=1)))


def test_injury_blind_and_downgrade_are_shown(tmp_path):
    text = B.build(DAY, root=world(tmp_path), run=fake_run())
    assert 'NYK@TOR  LEAN OVER 214.5' in text
    assert 'DET@SAS  MONITOR  INJURY_BLIND DET: Duncan Robinson, Taurean Prince (Duncan Robinson top-2 scorer) ' \
           '-> spread now MONITOR' in text
    assert '1 pick(s), 1 game(s) INJURY_BLIND' in text


def test_missing_inputs_say_so(tmp_path):
    text = B.build(DAY, root=tmp_path, run=fake_run())
    assert 'no results_log.csv yet' in text and 'no team picks file' in text and 'no card for 2026-11-17' in text


def test_brief_stays_under_8kb_by_trimming_the_card(tmp_path):
    text = B.build(DAY, root=world(tmp_path, card_rows=400), run=fake_run())
    assert len(text.encode()) <= B.MAX_BYTES
    assert 'card trimmed to fit' in text and text.rstrip().endswith('price/max: ...')
    assert 'RESEARCH-ONLY (UNVALIDATED)' in text
