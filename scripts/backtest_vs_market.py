"""
Team model backtest against REAL market lines (historical_odds, from
scripts/backfill_espn_odds.py).

Unlike scripts/backtest_team_model.py — which had no market lines and used
end-of-season team_stats NET_RTG (lookahead) — this replays the production
TeamRatingEngine point-in-time (through_date = game date) and grades every
spread / moneyline / total signal against the market line at real prices.

Leakage controls:
  * Ratings use only games strictly before the game date.
  * A game is scored only once BOTH teams have >= SRS_MIN_GAMES prior games,
    so the engine never falls back to end-of-season team_stats NET_RTG.
  * Totals use ORTG/DRTG/pace rebuilt from prior box scores (possessions ≈
    FGA + 0.44*FTA - OREB + TOV), not season-aggregate team_stats.

Usage:
    python scripts/backtest_vs_market.py
    python scripts/backtest_vs_market.py --seasons 2023-24 2024-25 --save
"""

import argparse
import sqlite3
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from config.settings import DB_PATH
import src.models.team_ratings as tr

# Production tier thresholds (generate_todays_team_picks.py / README)
SPREAD_TIERS = [('MONITOR', 2.0), ('LEAN', 3.5), ('STRONG', 5.5)]
ML_TIERS = [('LEAN', 0.07), ('STRONG', 0.12)]
OU_TIERS = [('LEAN', 4.0), ('STRONG', 7.0)]
PLAYOFF_TOTAL_FACTOR = 0.955
PROVIDER_PREF = ['Draft Kings', 'DraftKings', 'ESPN BET', 'consensus']
OUT_CSV = Path('data/processed/backtest_vs_market.csv')


# ── Speed: serve the production engine from in-memory season frames ──────────
# TeamRatingEngine re-runs a heavy aggregate SQL (and 3 seasons of HCA history)
# per instantiation; across ~1,300 dates that dominates runtime. Same data and
# same code paths, just sliced in memory instead of re-queried.

_orig_load = tr._load_team_games
_orig_hca = tr.compute_team_hca
_load_cache: dict = {}
_hca_cache: dict = {}


def _cached_load(conn, seasons, season_types=None, through_date=None):
    key = (tuple(seasons), tuple(season_types or ()))
    if key not in _load_cache:
        _load_cache[key] = _orig_load(conn, list(seasons), season_types)
    df = _load_cache[key]
    if through_date:
        df = df[df['game_date'] < pd.to_datetime(through_date)]
    return df.copy()


def _cached_hca(conn, season, hist_n=tr.HCA_HIST_N):
    if (season, hist_n) not in _hca_cache:
        _hca_cache[(season, hist_n)] = _orig_hca(conn, season, hist_n)
    return _hca_cache[(season, hist_n)]


tr._load_team_games = _cached_load
tr.compute_team_hca = _cached_hca


# ── Market lines ──────────────────────────────────────────────────────────────

def load_market(conn) -> pd.DataFrame:
    """One pregame line per game: preferred provider, closing values when exposed."""
    df = pd.read_sql("SELECT * FROM historical_odds", conn)
    if df.empty:
        return df
    df['spread_mkt'] = df['close_spread_home'].fillna(df['spread_home'])
    df['total_mkt'] = df['close_total'].fillna(df['total'])
    df['home_ml_mkt'] = df['close_home_ml'].fillna(df['home_ml'])
    df['away_ml_mkt'] = df['close_away_ml'].fillna(df['away_ml'])
    rank = {p: i for i, p in enumerate(PROVIDER_PREF)}
    df['_rank'] = df['provider'].map(rank).fillna(len(rank))
    df['_has'] = df['spread_mkt'].notna().astype(int) + df['total_mkt'].notna().astype(int)
    df = df.sort_values(['espn_event_id', '_has', '_rank'], ascending=[True, False, True])
    df = df.drop_duplicates('espn_event_id')
    return df[['game_date', 'home_abbr', 'away_abbr', 'home_score', 'away_score',
               'provider', 'spread_mkt', 'total_mkt',
               'home_ml_mkt', 'away_ml_mkt', 'open_spread_home', 'open_total']]


# ── Point-in-time totals inputs ───────────────────────────────────────────────

BOX_SQL = """
SELECT game_id, game_date, team_abbr AS team, opp_abbr AS opp,
       SUM(pts) pts, SUM(fga) fga, SUM(fta) fta, SUM(oreb) oreb, SUM(tov) tov
FROM game_logs WHERE season = ? AND season_type IN ('Regular Season','Playoffs','PlayIn')
GROUP BY game_id, team_abbr
"""


def load_box(conn, season) -> pd.DataFrame:
    b = pd.read_sql(BOX_SQL, conn, params=(season,))
    b['game_date'] = pd.to_datetime(b['game_date'])
    b['poss'] = b['fga'] + 0.44 * b['fta'] - b['oreb'] + b['tov']
    opp = b[['game_id', 'team', 'pts', 'poss']].rename(
        columns={'team': 'opp', 'pts': 'opp_pts', 'poss': 'opp_poss'})
    b = b.merge(opp, on=['game_id', 'opp'])
    b['game_poss'] = (b['poss'] + b['opp_poss']) / 2
    return b


def pit_team_stats(box: pd.DataFrame, through_date) -> dict:
    prior = box[box['game_date'] < through_date]
    g = prior.groupby('team').agg(pts=('pts', 'sum'), opp_pts=('opp_pts', 'sum'),
                                  poss=('game_poss', 'sum'), n=('game_id', 'count'))
    g['off_rtg'] = g['pts'] / g['poss'] * 100
    g['def_rtg'] = g['opp_pts'] / g['poss'] * 100
    g['pace'] = g['poss'] / g['n']
    return g[['off_rtg', 'def_rtg', 'pace']].to_dict('index')


def predict_total(h, a, is_playoffs):
    pace = (h['pace'] + a['pace']) / 2
    t = ((h['off_rtg'] + a['def_rtg']) / 2 + (a['off_rtg'] + h['def_rtg']) / 2) * pace / 100
    return t * PLAYOFF_TOTAL_FACTOR if is_playoffs else t


# ── Replay ────────────────────────────────────────────────────────────────────

def no_vig(home_ml, away_ml):
    def imp(ml):
        return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)
    if pd.isna(home_ml) or pd.isna(away_ml):
        return None
    h, a = imp(home_ml), imp(away_ml)
    return h / (h + a)


def ml_profit(ml):
    return ml / 100 if ml > 0 else 100 / -ml


def replay_season(season: str) -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    market = load_market(conn)
    games = _orig_load(conn, [season], ['Regular Season', 'Playoffs', 'PlayIn'])
    games = games[games['is_home'] == 1]
    box = load_box(conn, season)
    market['game_date'] = pd.to_datetime(market['game_date'])
    games = games.merge(market, left_on=['game_date', 'team', 'opp'],
                        right_on=['game_date', 'home_abbr', 'away_abbr'], how='left')

    rows = []
    for gdate, day in games.groupby('game_date'):
        ds = gdate.strftime('%Y-%m-%d')
        stypes = set(day['season_type'])
        engines = {}
        pit = pit_team_stats(box, gdate)
        for _, g in day.iterrows():
            po = g['season_type'] != 'Regular Season'
            if po not in engines:
                engines[po] = tr.TeamRatingEngine(conn, season, through_date=ds, include_playoffs=po)
            eng = engines[po]
            n_home = eng._games[eng._games['team'] == g['team']]['game_id'].nunique()
            n_away = eng._games[eng._games['team'] == g['opp']]['game_id'].nunique()
            if min(n_home, n_away) < tr.SRS_MIN_GAMES:
                continue
            # Grade on ESPN official finals; game_logs sums miss players in older seasons
            if pd.notna(g['home_score']) and pd.notna(g['away_score']):
                margin = g['home_score'] - g['away_score']
                total_pts = g['home_score'] + g['away_score']
            else:
                margin, total_pts = g['margin'], g['team_pts'] + g['opp_pts']
            pred_spread = eng.predict_spread(g['team'], g['opp'], ds, is_playoffs=po)
            pred_wp = eng.win_probability(g['team'], g['opp'], ds, is_playoffs=po)
            h, a = pit.get(g['team']), pit.get(g['opp'])
            pred_total = predict_total(h, a, po) if h and a else np.nan
            rows.append({
                'season': season, 'game_date': ds, 'season_type': g['season_type'],
                'home': g['team'], 'away': g['opp'], 'margin': margin,
                'total_pts': total_pts, 'home_win': int(margin > 0),
                'provider': g['provider'], 'spread_mkt': g['spread_mkt'],
                'open_spread': g['open_spread_home'], 'total_mkt': g['total_mkt'],
                'open_total': g['open_total'],
                'home_ml': g['home_ml_mkt'], 'away_ml': g['away_ml_mkt'],
                'pred_spread': pred_spread, 'pred_wp': pred_wp, 'pred_total': pred_total,
            })
    conn.close()
    print(f"  {season}: {len(rows):,} games scored", flush=True)
    return pd.DataFrame(rows)


# ── Grading ───────────────────────────────────────────────────────────────────

def grade(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Spread: model expected home margin = pred_spread; market home line = spread_mkt
    # (neg = home fav). Home covers iff margin + spread_mkt > 0.
    df['sp_gap'] = df['pred_spread'] + df['spread_mkt']          # >0 → bet home
    cover = df['margin'] + df['spread_mkt']
    side = np.sign(df['sp_gap'])
    df['sp_res'] = np.where(cover == 0, 0, np.where(np.sign(cover) == side, 1, -1))
    df['sp_units'] = np.select([df['sp_res'] == 1, df['sp_res'] == -1], [100 / 110, -1.0], 0.0)

    df['mkt_wp'] = [no_vig(h, a) for h, a in zip(df['home_ml'], df['away_ml'])]
    df['mkt_wp'] = df['mkt_wp'].astype(float)
    df['ml_edge_home'] = df['pred_wp'] - df['mkt_wp']
    bet_home = df['ml_edge_home'] > 0
    df['ml_edge'] = df['ml_edge_home'].abs()
    won = np.where(bet_home, df['home_win'] == 1, df['home_win'] == 0)
    price = np.where(bet_home, df['home_ml'], df['away_ml'])
    df['ml_units'] = [ml_profit(p) if w else -1.0 if not pd.isna(p) else np.nan
                      for p, w in zip(price, won)]

    df['ou_gap'] = df['pred_total'] - df['total_mkt']             # >0 → bet over
    diff = df['total_pts'] - df['total_mkt']
    df['ou_res'] = np.where(diff == 0, 0, np.where(np.sign(diff) == np.sign(df['ou_gap']), 1, -1))
    df['ou_units'] = np.select([df['ou_res'] == 1, df['ou_res'] == -1], [100 / 110, -1.0], 0.0)
    return df


def _summ(sub, res_col, units_col):
    n = len(sub)
    if n == 0:
        return None
    if res_col:
        w, l = (sub[res_col] == 1).sum(), (sub[res_col] == -1).sum()
        hr = w / (w + l) if w + l else np.nan
    else:
        w = (sub[units_col] > 0).sum()
        hr = w / n
    return n, hr, sub[units_col].sum(), sub[units_col].mean()


def report(df: pd.DataFrame) -> None:
    line = '─' * 72
    print(f"\n{line}\n  Line accuracy (lower MAE = sharper)\n{line}")
    sp = df.dropna(subset=['spread_mkt'])
    print(f"  Spread MAE  model {np.mean(np.abs(sp['pred_spread'] - sp['margin'])):.2f}"
          f"   market {np.mean(np.abs(-sp['spread_mkt'] - sp['margin'])):.2f}   (n={len(sp):,})")
    to = df.dropna(subset=['total_mkt', 'pred_total'])
    print(f"  Total  MAE  model {np.mean(np.abs(to['pred_total'] - to['total_pts'])):.2f}"
          f"   market {np.mean(np.abs(to['total_mkt'] - to['total_pts'])):.2f}   (n={len(to):,})")
    ml = df.dropna(subset=['mkt_wp'])
    print(f"  Win-prob Brier  model {np.mean((ml['pred_wp'] - ml['home_win'])**2):.4f}"
          f"   market {np.mean((ml['mkt_wp'] - ml['home_win'])**2):.4f}   (n={len(ml):,})")

    print(f"\n{line}\n  Betting results at real prices  (spread/total @ -110, breakeven 52.4%)\n{line}")
    print(f"  {'Market':<8} {'Tier':<9} {'Bets':>6} {'Win%':>7} {'Units':>9} {'ROI':>8}")
    for name, gap, res, units, tiers in [
        ('SPREAD', 'sp_gap', 'sp_res', 'sp_units', SPREAD_TIERS),
        ('TOTAL', 'ou_gap', 'ou_res', 'ou_units', OU_TIERS),
        ('ML', 'ml_edge', None, 'ml_units', ML_TIERS),
    ]:
        d = df.dropna(subset=[gap, units])
        bounds = [t for _, t in tiers] + [np.inf]
        for (tier, lo), hi in zip(tiers, bounds[1:]):
            s = _summ(d[(d[gap].abs() >= lo) & (d[gap].abs() < hi)], res, units)
            if s:
                print(f"  {name:<8} {tier:<9} {s[0]:>6,} {s[1]:>7.1%} {s[2]:>+9.1f} {s[3]:>+8.1%}")
        s = _summ(d[d[gap].abs() >= tiers[0][1] if name != 'SPREAD' else d[gap].abs() >= 3.5],
                  res, units)
        if s:
            print(f"  {name:<8} {'ALL PICKS':<9} {s[0]:>6,} {s[1]:>7.1%} {s[2]:>+9.1f} {s[3]:>+8.1%}")

    print(f"\n{line}\n  Per-season — production picks (spread LEAN+, total LEAN+, ML LEAN+)\n{line}")
    print(f"  {'Season':<9} {'Spr n':>6} {'Spr%':>7} {'SprROI':>8} {'Tot n':>6} {'Tot%':>7}"
          f" {'TotROI':>8} {'ML n':>6} {'MLROI':>8}")
    for season, g in df.groupby('season'):
        a = _summ(g[g['sp_gap'].abs() >= 3.5].dropna(subset=['sp_gap']), 'sp_res', 'sp_units')
        b = _summ(g[g['ou_gap'].abs() >= 4.0].dropna(subset=['ou_gap']), 'ou_res', 'ou_units')
        c = _summ(g[g['ml_edge'] >= 0.07].dropna(subset=['ml_units']), None, 'ml_units')
        f = lambda s, i: s[i] if s else np.nan
        print(f"  {season:<9} {f(a,0):>6.0f} {f(a,1):>7.1%} {f(a,3):>+8.1%} {f(b,0):>6.0f}"
              f" {f(b,1):>7.1%} {f(b,3):>+8.1%} {f(c,0):>6.0f} {f(c,3):>+8.1%}")

    # CLV proxy: did the line move toward the model between open and close?
    clv = df.dropna(subset=['open_spread', 'spread_mkt'])
    clv = clv[clv['sp_gap'].abs() >= 3.5]
    if len(clv) > 30:
        open_gap = clv['pred_spread'] + clv['open_spread']
        moved_toward = np.sign(open_gap) * (clv['open_spread'] - clv['spread_mkt'])
        print(f"\n  Spread CLV (picks with open+close, n={len(clv):,}): line moved toward model "
              f"{(moved_toward > 0).mean():.1%}, away {(moved_toward < 0).mean():.1%}, "
              f"avg {moved_toward.mean():+.2f} pts")


def main():
    ap = argparse.ArgumentParser()
    # game_logs only has complete team box scores from 2023-24 on (earlier seasons
    # miss 10-200 pts/game of player rows), so ratings before then are corrupt.
    ap.add_argument('--seasons', nargs='+', default=['2023-24', '2024-25', '2025-26'])
    ap.add_argument('--save', action='store_true')
    ap.add_argument('--from-csv', action='store_true', help='re-grade saved replay without recomputing')
    args = ap.parse_args()

    if args.from_csv:
        df = pd.read_csv(OUT_CSV)
    else:
        print(f"\n  Replaying {len(args.seasons)} seasons point-in-time...")
        with ProcessPoolExecutor(max_workers=min(4, len(args.seasons))) as pool:
            df = pd.concat(list(pool.map(replay_season, args.seasons)), ignore_index=True)
        if args.save:
            OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(OUT_CSV, index=False)
            print(f"  saved → {OUT_CSV}")

    print(f"  Market line coverage: {df['spread_mkt'].notna().mean():.1%} of {len(df):,} games")
    report(grade(df))


if __name__ == '__main__':
    main()
