"""Today dashboard page: log parsing and game-card derivation (no Streamlit server needed)."""
from pathlib import Path
import pandas as pd
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dashboard')]
import today as T


LOG = """Live evidence: /x
────
  Market props — yesterday
  Team picks — yesterday
  Player props — market prices and EV
  Team spread / ML picks

  STEP STATUS SUMMARY
  OK (exit 0) — Market props — yesterday
  FAILED (exit 9) — Team spread / ML picks
── card (research-only) ──
Card failed (exit 1); nothing written
"""


def test_stage_and_steps_parse_run_log():
    assert T.stage_of('') == -1
    assert T.STAGES[T.stage_of(LOG)][1] == 'Building prop card'
    assert T.step_results(LOG) == [('OK', 0, 'Market props — yesterday', ''),
                                   ('FAILED', 9, 'Team spread / ML picks', ''),
                                   ('FAILED', 1, 'Prop card', '')]


NEW_LOG = """  STEP STATUS SUMMARY
  OK (exit 0) — Market props — yesterday
  WARN (exit 0) — Player props — market prices and EV :: DK via ESPN: 0/506 passed (406 stale, 100 future-skew): 0 sportsbook lines
── card (research-only) ──
Card: /x/2026-11-24.txt
  WARN (exit 0) — Prop card :: card: 1 prop(s) excluded for team mismatch
Brief (attach this file to chat): /x
  OK (exit 0) — Brief
"""


def test_warn_steps_parse_and_render_yellow():
    steps = T.step_results(NEW_LOG)
    assert [(s, label) for s, _, label, _ in steps] == [
        ('OK', 'Market props — yesterday'), ('WARN', 'Player props — market prices and EV'),
        ('WARN', 'Prop card'), ('OK', 'Brief')]                 # no duplicate legacy Card:/Brief entries
    assert steps[1][3].startswith('DK via ESPN: 0/506 passed')
    html = T.step_chips(steps)
    assert html.count('class="step warn"') == 2 and html.count('class="step ok"') == 2


def test_job_chips_name_the_red_validate_player():
    health = {'ok': True, 'jobs': [
        {'job': 'validate', 'level': 'red', 'status': 'red',
         'reasons': ['1 mismatch(es) vs PropLine on 2026-11-23: AJ Green (MIL): minutes ESPN None vs PropLine 0.15']},
        {'job': 'kalshi', 'level': 'green', 'status': 'green', 'reasons': []}]}
    html, notes = T.job_chips(health)
    assert 'class="step fail"' in html and 'class="step ok"' in html
    assert notes == ['validate (red): 1 mismatch(es) vs PropLine on 2026-11-23: AJ Green (MIL): minutes ESPN None '
                     'vs PropLine 0.15']


def test_excluded_frame_orders_injuries_first():
    card = {'excluded': [
        {'reason': 'stale quote', 'player': 'B', 'team': 'PHX', 'game': 'g', 'stat': 'REB', 'venue': 'Kalshi',
         'line': '4+', 'detail': 'quote 50 min older than newest'},
        {'reason': 'injury', 'player': 'Cade Cunningham', 'team': 'DET', 'game': 'DET@ORL', 'stat': 'REB',
         'venue': '', 'line': None, 'detail': 'GTD (Day-To-Day, espn as of T; official Probable)'},
        {'reason': 'team mismatch', 'player': 'Isaiah Joe', 'team': 'DET', 'game': 'BOS@WAS', 'stat': '3PM',
         'venue': '', 'line': None, 'detail': 'team mismatch: roster DET, last game log DET, game BOS@WAS'}]}
    df = T.excluded_frame(card)
    assert list(df['reason']) == ['injury', 'team mismatch', 'stale quote']
    assert T.excluded_frame({}).empty


def game(**kw):
    base = dict(home='OKC', away='NOP', tier='MONITOR', ou_tier='NONE', sp_edge=5.6, market_spread=-11.5,
                ou_edge=-0.8, roster_continuity={})
    return {**base, **kw}


def test_injury_capped_game_reports_what_it_was_and_sorts_first():
    capped = game(injury_blind={'teams': {'NOP': {'players': [], 'reason': 'x'}},
                                'before_guard': {'tier': 'STRONG', 'ou_tier': 'NONE'}})
    v = T.game_view(capped)
    assert v['best'] == 'MONITOR' and v['was'] == ['spread STRONG']
    assert v['side'] == 'OKC' and v['spread_txt'] == 'OKC -11.5'
    plain = T.game_view(game(sp_edge=2.0))
    assert v['sort'] < plain['sort']
    html = T.game_card(capped, v)
    assert 'was spread STRONG' in html and 'NOP out' in html


def test_away_side_and_pass_label():
    v = T.game_view(game(tier='NONE', sp_edge=-3.2, market_spread=-7.5))
    assert v['side'] == 'NOP' and v['spread_txt'] == 'NOP +7.5' and v['best'] == 'PASS'


def test_html_is_escaped():
    a = game(home_full='<script>', injury_blind={'teams': {'NOP': {'players': [
        {'name': '<b>x</b>', 'ppg': 1, 'mpg': 1}], 'reason': '"q"'}}, 'before_guard': {}})
    out = T.game_card(a, T.game_view(a))
    assert '<script>' not in out and '<b>x</b>' not in out


def test_calibration_view_frames():
    import calibration_view as V
    art = {'report': {'kalshi_test': [{'venue': 'kalshi', 'subset': 'card selected (raw rules)', 'stat': 'all',
                                       'raw': {'overall': {'n': 106, 'hit': .67, 'pred': .756, 'brier': .2323, 'logloss': .66},
                                               'buckets': [{'lo': .7, 'hi': .75, 'n': 40, 'pred': .72, 'hit': .6,
                                                            'brier': .24, 'logloss': .67}]},
                                       'calibrated': {'overall': {'n': 106, 'hit': .67, 'pred': .635, 'brier': .2238,
                                                                  'logloss': .64}, 'buckets': []},
                                       'market_mid_brier': .2241, 'pnl_per_contract_after_fee': -.036,
                                       'clears_card_rules_raw': 106, 'clears_card_rules_calibrated': 0}],
                      'ledger': [{'venue': 'kalshi', 'subset': 'graded card ledger (live; no stored mid, raw only)',
                                  'stat': 'all', 'raw': {'overall': {'n': 124, 'hit': .702, 'pred': .771,
                                                                     'brier': .2143, 'logloss': .6}, 'buckets': []},
                                  'calibrated': None}]}}
    df = V.summary_frame(art)
    assert list(df['n']) == [106, 124] and df.loc[0, 'cal p'] == .635 and pd.isna(df.loc[1, 'cal p'])
    assert df.loc[0, 'pass rules cal'] == 0
    b = V.bucket_frame(art['report']['kalshi_test'][0])
    assert list(b['p']) == ['raw'] and b.loc[0, 'bucket'] == '0.70-0.75'
    assert V.load(Path('/nonexistent.json')) is None
