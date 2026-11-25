"""Props board (4b) and Kalshi ladders (4d) frames."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dashboard')]
import board_view as B  # noqa: E402

LEG = {'tip_pt': '7:00p', 'game': 'DAL@DEN', 'player': 'Nikola Jokić', 'team': 'DEN', 'opp': 'DAL', 'stat': 'AST',
       'platform': 'kalshi', 'side': 'NO', 'line': '12+', 'market': 'AST 12+', 'p': 0.70, 'p_raw': 0.79,
       'edge': -0.01, 'price': 0.71, 'max_price': 0.65, 'flags': ['on the line'], 'l5': '5/5', 'l10': '10/10',
       'l20': '18/20', 'season': '19/21', 'h2h': '1/1', 'l10_values': [9, 10, 8, 11, 9, 7, 10, 8, 12, 9],
       'min_l10': 37.1}


def test_board_has_every_column_in_order():
    df = B.board_frame([LEG], {('nikola jokic', 'AST'): 9.6})
    assert list(df.columns) == B.BOARD_COLUMNS
    r = df.iloc[0]
    assert (r['Team'], r['Opp'], r['Market'], r['Venue'], r['Projection']) == ('DEN', 'DAL', 'AST 12+', 'Kalshi', 9.6)
    assert (r['P raw'], r['P calibrated'], r['Edge']) == (0.79, 0.70, -0.01)
    assert (r['L5'], r['L20'], r['H2H']) == ('5/5', '18/20', '1/1')
    assert r['L10 values'] == '9 10 8 11 9 7 10 8 12 9' and r['MIN L10'] == 37.1
    assert r['Price'] == '71¢ / 65¢' and r['Flags'] == 'on the line'


def test_old_card_without_new_fields_still_renders():
    old = {k: LEG[k] for k in ('tip_pt', 'game', 'player', 'stat', 'platform', 'side', 'line', 'p', 'price', 'flags')}
    df = B.board_frame([old])
    assert list(df.columns) == B.BOARD_COLUMNS and df.iloc[0]['Market'] == 'AST 12+' and df.iloc[0]['L10'] == ''
    assert B.board_frame([]).empty and list(B.board_frame([]).columns) == B.BOARD_COLUMNS


def test_sleeper_price_and_csv_roundtrip():
    df = B.board_frame([dict(LEG, platform='sleeper', side='UNDER', line='2.5', price=None, market='AST UNDER 2.5')])
    assert df.iloc[0]['Price'] == "pick'em" and df.iloc[0]['Venue'] == 'Sleeper'
    assert df.to_csv(index=False).splitlines()[0].split(',')[:3] == ['Tip', 'Game', 'Player']


def test_fair_projections_normalize_names(tmp_path):
    (tmp_path / '2026-11-24.json').write_text(json.dumps([
        {'player': 'Nikola Jokić', 'stat': 'ast', 'pred': 9.63}, {'player': 'Jaren Jackson Jr.', 'stat': 'pts', 'pred': 21.0},
        {'player': 'X', 'stat': 'blk', 'pred': 1.0}]))
    proj = B.fair_projections('2026-11-24', tmp_path)
    assert proj == {('nikola jokic', 'AST'): 9.6, ('jaren jackson', 'PTS'): 21.0}
    assert B.fair_projections('2026-11-25', tmp_path) == {}


def test_ladder_frame_every_strike_with_edges():
    lad = [{'player': 'Nikola Jokić', 'team': 'DEN', 'game': 'DAL@DEN', 'stat': 'AST', 'tip': '2026-11-25T03:00:00+00:00',
            'strikes': [{'strike': 10, 'yes_ask': 0.55, 'no_ask': 0.47, 'mid': 0.54, 'l10': '6/10', 'p_raw_yes': 0.56,
                         'p_cal_yes': 0.53, 'stale': False},
                        {'strike': 12, 'yes_ask': 0.30, 'no_ask': 0.72, 'mid': 0.29, 'l10': '1/10', 'p_raw_yes': 0.21,
                         'p_cal_yes': None, 'stale': True}]}]
    df = B.ladder_frame(lad)
    assert list(df.columns) == B.LADDER_COLUMNS and list(df['Market']) == ['AST 10+', 'AST 12+']
    assert df.iloc[0]['Tip'] == '7:00pm' and df.iloc[0]['Edge YES'] == pytest.approx(-0.02)
    assert df.iloc[0]['Edge NO'] == pytest.approx(0.0)
    assert pd.isna(df.iloc[1]['Edge YES']) and bool(df.iloc[1]['Stale']) is True
