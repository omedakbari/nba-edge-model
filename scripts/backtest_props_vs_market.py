"""
Player-props model backtest against REAL sportsbook lines (historical_props,
from scripts/backfill_espn_props.py).

The original backtest (src/models/edge_finder.py) set each line to the
player's L10 average — a naive line no book would post. This replays the same
production prediction path (component models + BLEND_L10_WEIGHT blend +
Gaussian P(over) on rolling std) at the books' actual OPENING lines, and
compares it to the market's own no-vig probability.

Questions answered:
  1. Is the model better calibrated than the market? (Brier / log loss)
  2. Does model-vs-market disagreement predict outcomes? (logit regression)
  3. Would the production pick rules have made money at real prices,
     and would they clear pick'em breakeven?

Usage:
    python scripts/backtest_props_vs_market.py                  # 2025-26 (held-out test season)
    python scripts/backtest_props_vs_market.py --seasons 2024-25 2025-26
"""

import argparse
import re
import sqlite3
import sys
import unicodedata
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from config.settings import DB_PATH, PROCESSED_DIR, BLEND_L10_WEIGHT
from src.models.edge_finder import load_model, COMPOSITE_STATS

STATS = ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']
# Production pick floors (README "Daily picks use stat-specific floors")
PROD_FLOORS = {'reb': 0.60, 'ast': 0.60, 'pra': 0.65, 'reb_ast': 0.65, 'pts': 0.70}
PICKEM_BREAKEVEN = 0.542   # ~5/6-leg flex pick'em per-leg breakeven
OUT_CSV = PROCESSED_DIR / 'backtest_props_vs_market.csv'


def _norm_name(s: str) -> str:
    s = unicodedata.normalize('NFKD', str(s)).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^a-z ]', '', s.lower().replace('-', ' '))
    s = re.sub(r'\b(jr|sr|ii|iii|iv|v)\b', '', s)
    return re.sub(r'\s+', ' ', s).strip()


# ── Market side ───────────────────────────────────────────────────────────────

def load_market(conn, seasons_dates) -> pd.DataFrame:
    """Consensus opening line + no-vig P(over) per (game, player, stat)."""
    lo, hi = seasons_dates
    p = pd.read_sql("""
        SELECT p.espn_event_id, p.game_date, p.provider, p.espn_athlete_id, a.full_name,
               p.stat_key, p.side, p.line_open AS line, p.price_open AS price
        FROM historical_props p JOIN espn_athletes a USING (espn_athlete_id)
        WHERE p.game_date BETWEEN ? AND ? AND p.line_open IS NOT NULL AND p.price_open > 1
    """, conn, params=(lo, hi))
    if p.empty:
        return p
    key = ['espn_event_id', 'game_date', 'provider', 'espn_athlete_id', 'full_name', 'stat_key', 'line']
    w = p.pivot_table(index=key, columns='side', values='price', aggfunc='first').reset_index()
    w = w.dropna(subset=['over', 'under'])
    io, iu = 1 / w['over'], 1 / w['under']
    w['p_over_nv'] = io / (io + iu)
    w['hold'] = io + iu - 1
    w = w[(w['hold'] > 0) & (w['hold'] < 0.15)]          # drop stale/broken pairs

    # Consensus: modal line across books, then average no-vig prob at that line
    g = ['espn_event_id', 'game_date', 'espn_athlete_id', 'full_name', 'stat_key']
    mode = (w.groupby(g + ['line']).size().reset_index(name='n')
             .sort_values('n', ascending=False).drop_duplicates(g))
    cons = w.merge(mode[g + ['line']], on=g + ['line'])
    out = cons.groupby(g + ['line']).agg(
        mkt_p_over=('p_over_nv', 'mean'), n_books=('provider', 'nunique'),
        best_over=('over', 'max'), best_under=('under', 'max'),
        dk_over=('over', lambda s: s[cons.loc[s.index, 'provider'].str.contains('Draft')].max()),
    ).reset_index()
    out['name_key'] = out['full_name'].map(_norm_name)
    out['game_date'] = pd.to_datetime(out['game_date'])
    return out


# ── Model side (same path as production / edge_finder) ────────────────────────

def model_frame(seasons) -> pd.DataFrame:
    df = pd.read_parquet(PROCESSED_DIR / 'features.parquet')
    df = df[df['season'].isin(seasons)].copy()
    raw = {}
    for s in ['pts', 'reb', 'ast', 'fg3m']:
        m = load_model(s)
        raw[s] = m['model'].predict(df[m['feature_cols']])
    for stat in STATS:
        comps = COMPOSITE_STATS.get(stat)
        r = sum(raw[c] for c in comps) if comps else raw[stat]
        l10 = df[f'{stat}_L10']
        df[f'{stat}_pred'] = (1 - BLEND_L10_WEIGHT) * r + BLEND_L10_WEIGHT * l10
        std_col = f'{stat}_std_L10'
        std = df[std_col] if std_col in df.columns else pd.Series(np.nan, index=df.index)
        df[f'{stat}_std'] = std.fillna(df[f'{stat}_pred'] * 0.2)
    df['name_key'] = df['full_name'].map(_norm_name)
    df['game_date'] = pd.to_datetime(df['game_date'])
    return df


def build(seasons) -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    feats = model_frame(seasons)
    lo, hi = feats['game_date'].min().strftime('%Y-%m-%d'), feats['game_date'].max().strftime('%Y-%m-%d')
    mkt = load_market(conn, (lo, hi))
    conn.close()
    print(f"  market props: {len(mkt):,}   feature rows: {len(feats):,}")

    rows = []
    for stat in STATS:
        m = mkt[mkt['stat_key'] == stat]
        cols = ['player_id', 'name_key', 'game_date', 'season', 'season_type', 'team_abbr',
                'min_L10', f'{stat}_L10', f'{stat}_pred', f'{stat}_std', stat]
        j = m.merge(feats[cols], on=['name_key', 'game_date'], how='inner')
        j = j.rename(columns={f'{stat}_L10': 'l10', f'{stat}_pred': 'pred',
                              f'{stat}_std': 'std', stat: 'actual'})
        rows.append(j)
        print(f"  {stat:<8} matched {len(j):,} / {len(m):,} market props")
    d = pd.concat(rows, ignore_index=True)

    std = np.where((d['std'] <= 0) | d['std'].isna(), np.maximum(d['pred'].abs() * 0.2, 1.0), d['std'])
    d['model_p_over'] = np.clip(1 - scipy_stats.norm.cdf((d['line'] - d['pred']) / std), 0.01, 0.99)
    d['over_hit'] = (d['actual'] > d['line']).astype(int)
    d['push'] = (d['actual'] == d['line'])
    d = d[~d['push']]
    return d


# ── Evaluation ────────────────────────────────────────────────────────────────

def _brier(p, y):
    return float(np.mean((p - y) ** 2))


def _logit(p):
    p = np.clip(p, 0.01, 0.99)
    return np.log(p / (1 - p))


def _logit_fit(X, y, iters=25):
    """Unpenalized logistic regression via Newton-Raphson; returns (coefs, t-stats)."""
    b = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ b))
        H = X.T @ (X * (p * (1 - p))[:, None])
        b = b + np.linalg.solve(H, X.T @ (y - p))
    p = 1 / (1 + np.exp(-X @ b))
    se = np.sqrt(np.diag(np.linalg.inv(X.T @ (X * (p * (1 - p))[:, None]))))
    return b, b / se


def report(d: pd.DataFrame) -> None:
    line = '─' * 76
    print(f"\n{line}\n  Calibration vs real opening lines  (lower = better)\n{line}")
    print(f"  {'Stat':<8} {'N':>7} {'Brier model':>12} {'Brier mkt':>10} {'Line MAE model':>15}"
          f" {'Line-vs-L10 gap':>16}")
    for stat, g in d.groupby('stat_key'):
        print(f"  {stat:<8} {len(g):>7,} {_brier(g.model_p_over, g.over_hit):>12.4f}"
              f" {_brier(g.mkt_p_over, g.over_hit):>10.4f}"
              f" {np.mean(np.abs(g.pred - g.actual)):>7.2f} vs {np.mean(np.abs(g.line - g.actual)):.2f}"
              f" {np.mean(np.abs(g.line - g.l10)):>12.2f}")

    # Information test: does model disagreement add anything beyond the market?
    print(f"\n{line}\n  Does the model add info beyond the market?  logit(over) ~ mkt + (model - mkt)\n{line}")
    for stat, g in list(d.groupby('stat_key')) + [('ALL', d)]:
        X = np.c_[np.ones(len(g)), _logit(g.mkt_p_over), _logit(g.model_p_over) - _logit(g.mkt_p_over)]
        try:
            b, t = _logit_fit(X, g.over_hit.values.astype(float))
            print(f"  {stat:<8} n={len(g):>7,}  mkt coef {b[1]:+.2f}  "
                  f"model-disagreement coef {b[2]:+.3f}  (t={t[2]:+.2f})")
        except Exception as e:
            print(f"  {stat:<8} fit failed: {e}")

    # Production rule replay: pick side where model prob >= stat floor, minutes filter
    print(f"\n{line}\n  Production pick rules replayed at real lines  (min_L10 >= 20)\n{line}")
    print(f"  pick'em breakeven ≈ {PICKEM_BREAKEVEN:.1%}/leg; book ROI uses best available opening price")
    print(f"  {'Stat':<8} {'Picks':>7} {'Model P':>8} {'Mkt P':>7} {'Hit%':>7} {'Book ROI':>9}")
    d = d.copy()
    d['side_over'] = d['model_p_over'] >= 0.5
    d['model_p'] = np.where(d['side_over'], d['model_p_over'], 1 - d['model_p_over'])
    d['mkt_p'] = np.where(d['side_over'], d['mkt_p_over'], 1 - d['mkt_p_over'])
    d['hit'] = np.where(d['side_over'], d['over_hit'], 1 - d['over_hit'])
    d['price'] = np.where(d['side_over'], d['best_over'], d['best_under'])
    d['units'] = np.where(d['hit'] == 1, d['price'] - 1, -1.0)
    base = d[d['min_L10'] >= 20]
    tot = []
    for stat, floor in PROD_FLOORS.items():
        g = base[(base['stat_key'] == stat) & (base['model_p'] >= floor)]
        tot.append(g)
        if len(g):
            print(f"  {stat:<8} {len(g):>7,} {g.model_p.mean():>8.1%} {g.mkt_p.mean():>7.1%}"
                  f" {g.hit.mean():>7.1%} {g.units.mean():>+9.1%}")
    g = pd.concat(tot)
    print(f"  {'ALL':<8} {len(g):>7,} {g.model_p.mean():>8.1%} {g.mkt_p.mean():>7.1%}"
          f" {g.hit.mean():>7.1%} {g.units.mean():>+9.1%}")

    # Edge-vs-market rule: bet only where model beats the market's no-vig prob
    print(f"\n{line}\n  Edge-vs-market rule: model P − market P ≥ threshold  (min_L10 >= 20)\n{line}")
    print(f"  {'Edge ≥':<8} {'Bets':>7} {'Model P':>8} {'Mkt P':>7} {'Hit%':>7} {'Book ROI':>9}")
    for t in [0.0, 0.03, 0.05, 0.08, 0.12, 0.20]:
        g = base[(base['model_p'] - base['mkt_p']) >= t]
        if len(g):
            print(f"  {t:<8.0%} {len(g):>7,} {g.model_p.mean():>8.1%} {g.mkt_p.mean():>7.1%}"
                  f" {g.hit.mean():>7.1%} {g.units.mean():>+9.1%}")

    # Is the market itself calibrated? (sanity check on the data)
    print(f"\n{line}\n  Market calibration sanity check (favored side by no-vig P)\n{line}")
    fav = np.maximum(d['mkt_p_over'], 1 - d['mkt_p_over'])
    fav_hit = np.where(d['mkt_p_over'] >= 0.5, d['over_hit'], 1 - d['over_hit'])
    for lo, hi in [(0.5, 0.55), (0.55, 0.6), (0.6, 0.65), (0.65, 0.7), (0.7, 1.0)]:
        m = (fav >= lo) & (fav < hi)
        if m.sum() > 50:
            print(f"  mkt P {lo:.0%}-{hi:.0%}: n={m.sum():>6,}  mean P {fav[m].mean():.1%}  hit {fav_hit[m].mean():.1%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seasons', nargs='+', default=['2025-26'])
    ap.add_argument('--save', action='store_true')
    args = ap.parse_args()
    d = build(args.seasons)
    if args.save:
        d.to_csv(OUT_CSV, index=False)
        print(f"  saved → {OUT_CSV}")
    report(d)


if __name__ == '__main__':
    main()
