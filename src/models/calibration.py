"""Card calibration: raw card p -> calibrated p (AUDIT_2026-11-24.md item 4; DECISIONS.md D9-D13).

Fit:     ./venv/bin/python -m src.models.calibration --fit [--replay data/calibration/kalshi_replay.csv]
         (build the replay first: cd worker && ../venv/bin/python -m prop_worker.replay)
Report:  ./venv/bin/python -m src.models.calibration --report

Kalshi (venue 'kalshi'): fitted on the no-lookahead replay of the card's own Kalshi formula
  (worker/prop_worker/replay.py). Each strike gives two candidate sides (YES at p = pk, price = yes ask;
  NO at 1 - pk, no ask). Only sides in the CARD REGION (raw p - ask > 0, where the card's rule could ever
  pick) are used: the card selects on disagreement with the market, and outside that region the raw p is
  already calibrated, so a map fitted on all rows barely moves and misses the card's bias.
  Method: logistic regression ('logit2') of the outcome on two inputs the card already has,
      logit(market mid for the side)  and  dev = (raw p - mid) / W_HIST  (= hit-rate history - mid),
  per stat when the stat has >= MIN_FIT fit rows, else pooled. Isotonic on raw p alone is reported for
  comparison but NOT used: within the card region it cannot tell a high p that agrees with the market from
  one that comes from history disagreeing with it, and on the held-out dates its legs that still cleared the
  card's rules hit 71% at an 80c average price (DECISIONS.md D10). Time split: fit on dates < TEST_FROM, evaluate on dates >= TEST_FROM; the
  shipped map is then refit on all dates (n and dates recorded).
Sleeper (venue 'sleeper'): no replay yet. Additive offset from graded card ledger bets (Supabase, read-only),
  shrunk toward 0: offset = mean(hit - p) * n / (n + SHRINK_K).
The artifact data/processed/models/card_calibration.json carries the maps, fit date, windows, n and the
reliability report shown on the dashboard's Calibration page. The worker applies it with
worker/prop_worker/calibration.py (same JSON, no sklearn needed).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
REPLAY = ROOT / 'data/calibration/kalshi_replay.csv'
ARTIFACT = ROOT / 'data/processed/models/card_calibration.json'
TEST_FROM = '2026-11-18'          # ~6 most recent slates; only 13 slates of Kalshi history exist
MIN_FIT = 5000                    # fit rows per stat for its own map; else pooled (D11: per-stat maps were unstable at ~1-2k rows)
SHRINK_K = 100                    # Sleeper ledger offset shrink: n / (n + K)
BUCKETS = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 1.0001]
STATS = ('pts', 'reb', 'ast', 'fg3m', 'pra')
EPS = 1e-4


# ── data ────────────────────────────────────────────────────────────────────

def sides(replay: pd.DataFrame) -> pd.DataFrame:
    """One row per (strike, side) with the card's raw p, price, market mid, outcome and selection flag."""
    out = []
    for side in ('YES', 'NO'):
        x = replay.copy()
        yes = side == 'YES'
        won = (x['actual'] > x['line']).astype(int)
        x['side_c'] = side
        x['p'] = x['pk'] if yes else 1 - x['pk']
        x['price_s'] = x['yes_ask'] if yes else x['no_ask']
        x['mid_s'] = x['mid'] if yes else 1 - x['mid']
        x['y'] = won if yes else 1 - won
        x['selected_s'] = (x['selected'] == 1) & (x['sel_side'] == side)
        out.append(x)
    a = pd.concat(out, ignore_index=True)
    a = a[a['price_s'].notna()].copy()
    a['edge_raw'] = a['p'] - a['price_s']
    return a


def card_region(a: pd.DataFrame) -> pd.DataFrame:
    return a[a['edge_raw'] > 0]


# ── maps ────────────────────────────────────────────────────────────────────

def fit_isotonic(p, y) -> dict:
    from sklearn.isotonic import IsotonicRegression
    iso = IsotonicRegression(out_of_bounds='clip', y_min=0.01, y_max=0.99).fit(np.asarray(p), np.asarray(y))
    return {'method': 'isotonic', 'x': [round(float(v), 6) for v in iso.X_thresholds_],
            'y': [round(float(v), 6) for v in iso.y_thresholds_], 'n': int(len(p))}


def _logit(p):
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def fit_logit2(p, mid, y, w_hist: float) -> dict:
    from sklearn.linear_model import LogisticRegression
    X = np.c_[_logit(mid), (np.asarray(p, float) - np.asarray(mid, float)) / w_hist]
    lr = LogisticRegression(C=10.0, max_iter=1000).fit(X, np.asarray(y))
    return {'method': 'logit2', 'b0': round(float(lr.intercept_[0]), 6), 'b_mid': round(float(lr.coef_[0][0]), 6),
            'b_dev': round(float(lr.coef_[0][1]), 6), 'w_hist': w_hist, 'n': int(len(p))}


def apply_map(m: dict, p, mid=None) -> np.ndarray:
    """Same math as worker/prop_worker/calibration.py (kept in sync by tests). logit2 needs the side's mid."""
    p = np.asarray(p, dtype=float)
    if m['method'] == 'logit2':
        z = m['b0'] + m['b_mid'] * _logit(mid) + m['b_dev'] * (p - np.asarray(mid, float)) / m['w_hist']
        return 1 / (1 + np.exp(-z))
    if m['method'] == 'isotonic':
        return np.interp(p, m['x'], m['y'])
    if m['method'] == 'offset':
        return np.clip(p + m['offset'], 0.01, 0.99)
    if m['method'] == 'identity':
        return p
    raise ValueError(f"unknown calibration method {m['method']!r}")


def w_hist() -> float:
    from config.model_config import WEIGHTS
    return WEIGHTS['card']['hist_weight']


def fit_kalshi(region: pd.DataFrame, method: str = 'logit2') -> dict:
    def one(g):
        if method == 'isotonic':
            return fit_isotonic(g['p'], g['y'])
        return fit_logit2(g['p'], g['mid_s'], g['y'], w_hist())
    maps = {'_pooled': one(region)}
    for stat in STATS:
        g = region[region['stat'] == stat]
        maps[stat] = one(g) if len(g) >= MIN_FIT else {'method': 'pooled', 'n': int(len(g))}
    return maps


def _cal(maps: dict, g: pd.DataFrame) -> np.ndarray:
    """Calibrated p for every row of g (any mix of stats), in g's row order."""
    out = np.empty(len(g))
    for stat in g['stat'].unique():
        k = (g['stat'] == stat).values
        out[k] = apply_map(kalshi_map(maps, stat), g['p'].values[k], g['mid_s'].values[k])
    return out


def kalshi_map(maps: dict, stat: str) -> dict:
    m = maps.get(stat) or {'method': 'pooled'}
    return maps['_pooled'] if m['method'] == 'pooled' else m


def fit_sleeper(ledger: pd.DataFrame) -> dict:
    """ledger: graded card Sleeper bets with columns p, y. Shrunk additive offset."""
    n = len(ledger)
    if not n:
        return {'method': 'identity', 'n': 0, 'note': 'no graded Sleeper ledger bets'}
    raw = float((ledger['y'] - ledger['p']).mean())
    return {'method': 'offset', 'offset': round(raw * n / (n + SHRINK_K), 5), 'raw_offset': round(raw, 5),
            'n': int(n), 'shrink_k': SHRINK_K}


# ── metrics ─────────────────────────────────────────────────────────────────

def _logloss(p, y):
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    y = np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def reliability(p, y) -> dict:
    """{'overall': {...}, 'buckets': [{lo, hi, n, pred, hit, brier, logloss}]} over p in [0.5, 1]."""
    p, y = np.asarray(p, float), np.asarray(y, float)
    rows = []
    for lo, hi in zip(BUCKETS[:-1], BUCKETS[1:]):
        k = (p >= lo) & (p < hi)
        if k.sum():
            rows.append({'lo': lo, 'hi': min(hi, 1.0), 'n': int(k.sum()), 'pred': round(float(p[k].mean()), 4),
                         'hit': round(float(y[k].mean()), 4), 'brier': round(float(np.mean((p[k] - y[k]) ** 2)), 4),
                         'logloss': round(_logloss(p[k], y[k]), 4)})
    overall = {'n': int(len(p)), 'pred': round(float(p.mean()), 4) if len(p) else None,
               'hit': round(float(y.mean()), 4) if len(p) else None,
               'brier': round(float(np.mean((p - y) ** 2)), 4) if len(p) else None,
               'logloss': round(_logloss(p, y), 4) if len(p) else None}
    return {'overall': overall, 'buckets': rows}


def card_rules(p, price) -> np.ndarray:
    """The card's Kalshi rule (worker/prop_worker/card.py): edge >= 5c, price <= 85c, p >= .68."""
    p, price = np.asarray(p, float), np.asarray(price, float)
    return (p - price >= 0.05 - 1e-9) & (price <= 0.85) & (p >= 0.68)


def kalshi_report(a: pd.DataFrame, maps: dict, iso_maps: dict | None = None) -> list[dict]:
    """Reliability on the TEST split: raw vs calibrated (+ isotonic-on-p and market-mid baselines), per stat,
    on the card region, the legs the RAW rules selected, and the legs the CALIBRATED rules would select."""
    test = card_region(a[a['date'] >= TEST_FROM]).copy()
    test['cal'] = _cal(maps, test)
    if iso_maps:
        test['iso'] = _cal(iso_maps, test)
    subsets = (('card region (raw p - ask > 0)', test),
               ('card selected (raw rules)', test[test['selected_s']]),
               ('card selected (calibrated rules)', test[card_rules(test['cal'], test['price_s'])]))
    out = []
    for subset, frame in subsets:
        for stat in ('all',) + STATS:
            g = frame if stat == 'all' else frame[frame['stat'] == stat]
            if g.empty:
                continue
            y, raw, cal, price = g['y'].values, g['p'].values, g['cal'].values, g['price_s'].values
            fee = 0.07 * price * (1 - price)
            out.append({'venue': 'kalshi', 'subset': subset, 'stat': stat,
                        'raw': reliability(raw, y), 'calibrated': reliability(cal, y),
                        'isotonic_p_only_brier': (round(float(np.mean((g['iso'].values - y) ** 2)), 4)
                                                  if 'iso' in g else None),
                        'market_mid_brier': round(float(np.mean((g['mid_s'].values - y) ** 2)), 4),
                        'mean_price': round(float(price.mean()), 4),
                        'pnl_per_contract_after_fee': round(float(np.mean(y - price - fee)), 4),
                        'clears_card_rules_raw': int(card_rules(raw, price).sum()),
                        'clears_card_rules_calibrated': int(card_rules(cal, price).sum())})
    return out


def ledger_report(ledger: pd.DataFrame, kalshi_maps: dict, sleeper: dict) -> list[dict]:
    out = []
    for venue, g in ledger.groupby('platform'):
        raw, y = g['p'].values, g['y'].values
        if venue == 'kalshi':
            if g['mid'].isna().any():
                continue                    # legacy ledger rows carry no market mid: report raw only below
            gg = g.assign(mid_s=g['mid'])
            cal = _cal(kalshi_maps, gg)
        else:
            cal = apply_map(sleeper, raw)
        out.append({'venue': venue, 'subset': 'graded card ledger (live, in-sample for Sleeper)', 'stat': 'all',
                    'raw': reliability(raw, y), 'calibrated': reliability(cal, y)})
    k = ledger[ledger['platform'] == 'kalshi']
    if len(k) and k['mid'].isna().any():
        out.append({'venue': 'kalshi', 'subset': 'graded card ledger (live; no stored mid, raw only)',
                    'stat': 'all', 'raw': reliability(k['p'].values, k['y'].values), 'calibrated': None})
    return out


# ── ledger (Supabase, read-only) ──────────────────────────────────────────────

LEDGER_STAT = {'PTS': 'pts', 'REB': 'reb', 'AST': 'ast', '3PM': 'fg3m', 'PRA': 'pra', 'RA': 'ra'}


def load_ledger(conn=None) -> pd.DataFrame:
    """Graded card bets: platform, stat, p (raw p at logging), y (1 win / 0 loss). Read-only."""
    if conn is None:
        sys.path.insert(0, str(ROOT / 'worker'))
        from prop_worker.db import connect
        with connect() as c:
            return load_ledger(c)
    conn.read_only = True
    rows = conn.execute(
        """select platform, stat, p_at_placement, result, note from bets
           where sport = 'nba' and source = 'card' and result in ('win', 'loss')
             and p_at_placement is not null""").fetchall()
    recs = []
    for platform, stat, p, result, note in rows:
        p_raw = _p_raw_from_note(note)
        recs.append({'platform': platform, 'stat': LEDGER_STAT.get(stat, stat).lower(),
                     'p': float(p_raw if p_raw is not None else p), 'y': int(result == 'win'),
                     'mid': _note_float(note, 'mid=')})
    return pd.DataFrame(recs, columns=['platform', 'stat', 'p', 'y', 'mid'])


def _note_float(note, key):
    """Legs logged after calibration carry 'p_raw=0.8443' and 'mid=0.7100' in the note."""
    if not note or key not in note:
        return None
    try:
        return float(note.split(key, 1)[1].split(',', 1)[0].split()[0])
    except (ValueError, IndexError):
        return None


def _p_raw_from_note(note):
    return _note_float(note, 'p_raw=')


# ── fit / save ──────────────────────────────────────────────────────────────

def fit_all(replay: pd.DataFrame, ledger: pd.DataFrame) -> dict:
    a = sides(replay)
    region = card_region(a)
    fit_part = region[region['date'] < TEST_FROM]
    held_out_maps = fit_kalshi(fit_part)              # evaluated on the test split
    held_out_iso = fit_kalshi(fit_part, 'isotonic')   # comparison only
    shipped = fit_kalshi(region)                      # refit on every date, what the card uses
    sleeper = fit_sleeper(ledger[ledger['platform'] == 'sleeper'])
    dates = sorted(a['date'].unique())
    return {
        'fit_date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'kalshi': {'maps': shipped, 'source': 'replay (worker/prop_worker/replay.py), card region only',
                   'dates': [dates[0], dates[-1]], 'n_region': int(len(region)),
                   'n_players_games': int(replay.groupby(['game_id', 'player_id']).ngroups),
                   'time_split': {'fit': [dates[0], max(d for d in dates if d < TEST_FROM)],
                                  'test': [TEST_FROM, dates[-1]], 'n_fit': int(len(fit_part)),
                                  'n_test_region': int(len(card_region(a[a['date'] >= TEST_FROM])))}},
        'sleeper': sleeper,
        'report': {'kalshi_test': kalshi_report(a, held_out_maps, held_out_iso),
                   'ledger': ledger_report(ledger, shipped, sleeper)},
    }


def save(art: dict, path: Path = ARTIFACT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(art, indent=1))
    return path


def print_report(art: dict) -> None:
    k = art['kalshi']
    print(f"card calibration fit {art['fit_date']}  Kalshi replay {k['dates'][0]}..{k['dates'][1]} "
          f"(card region n={k['n_region']}, {k['n_players_games']} player-games); "
          f"time split fit {k['time_split']['fit']} test {k['time_split']['test']}")
    for s, m in k['maps'].items():
        coef = f" b0={m['b0']:+.3f} b_mid={m['b_mid']:.3f} b_dev={m['b_dev']:+.3f}" if m['method'] == 'logit2' else ''
        print(f"  kalshi {s:8s} {m['method']:9s} n={m['n']}{coef}")
    sl = art['sleeper']
    print(f"  sleeper  {sl['method']:9s} n={sl['n']} offset={sl.get('offset')} (raw {sl.get('raw_offset')})")
    for r in art['report']['kalshi_test'] + art['report']['ledger']:
        raw, cal = r['raw']['overall'], (r['calibrated'] or {}).get('overall')
        c = f"cal p {cal['pred']:.3f} B {cal['brier']:.4f}" if cal else 'cal -'
        extra = (f" | iso B {r['isotonic_p_only_brier']} mid B {r['market_mid_brier']} | price {r['mean_price']:.3f} "
                 f"pnl/contract {r['pnl_per_contract_after_fee']:+.3f} | pass rules raw {r['clears_card_rules_raw']}"
                 f" cal {r['clears_card_rules_calibrated']}") if 'market_mid_brier' in r else ''
        print(f"  {r['venue']:7s} {r['stat']:5s} {r['subset'][:32]:32s} n={raw['n']:5d} hit {raw['hit']:.3f} | "
              f"raw p {raw['pred']:.3f} B {raw['brier']:.4f} | {c}{extra}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--fit', action='store_true', help='fit from the replay CSV + ledger and save the artifact')
    ap.add_argument('--report', action='store_true', help='print the saved artifact report')
    ap.add_argument('--replay', type=Path, default=REPLAY)
    args = ap.parse_args(argv)
    if args.fit:
        art = fit_all(pd.read_csv(args.replay), load_ledger())
        print(f'saved {save(art)}')
        print_report(art)
        return 0
    if args.report:
        print_report(json.loads(ARTIFACT.read_text()))
        return 0
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
