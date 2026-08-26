"""
Team Spread / Win-Probability Backtest.

Replays historical NBA seasons game by game. For each game, the rating engine
is built from all prior games in that season (strict no-leakage). We then
compare predicted spread to actual margin, and predicted win probability to
actual outcome.

Key metrics:
  - Spread MAE / calibration (model spread vs actual margin)
  - ATS (against the spread) % — does the model's favored team cover more than 50%?
  - Win probability calibration (Brier score, reliability diagram buckets)
  - Performance by confidence tier (strong / lean / no-edge)
  - Per-season consistency

Note on the "ATS without market line" limitation:
  We don't have historical market lines in the DB. When the model says BOS -6
  and the actual margin is BOS +3, that's a miss — but we can't say whether it
  was a miss vs the closing Vegas line (which might have been BOS -3, making
  it a cover). What we CAN measure:
    1. Model spread accuracy (residuals vs actual)
    2. Directional accuracy (model picks correct winner, equivalent to ML)
    3. "Beat-own-spread" coverage: team identified as favored covers its own
       predicted spread (lower bar but internally consistent)

Usage:
    python scripts/backtest_team_model.py [--seasons 2021-22 2022-23 ...]
    python scripts/backtest_team_model.py --save
"""

import argparse
import sqlite3
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from config.settings import DB_PATH
from src.models.team_ratings import _load_team_games, compute_team_hca

SEASONS = ['2021-22', '2022-23', '2023-24', '2024-25']
LEAN_THRESHOLD   = 3.5   # pts gap to qualify as a "lean"
STRONG_THRESHOLD = 5.5   # pts gap to qualify as "strong"


# ── Per-game prediction ───────────────────────────────────────────────────────

def predict_game(
    conn: sqlite3.Connection,
    row: pd.Series,
    season_games: pd.DataFrame,
    team_hca: dict[str, float],
) -> dict | None:
    """
    Predict spread for a single game using only games that precede it.
    Returns None if insufficient data (<5 prior games for either team).

    team_hca: precomputed via compute_team_hca(conn, season) — depends only on
    `season` (past-3-seasons history), not on this game's date, so the caller
    computes it once per season rather than re-querying the DB per game.
    """
    game_date = str(row['game_date'])[:10]
    season = row['season']
    stype  = row['season_type']
    home   = row['team']
    away   = row['opp']

    # Only process home-team row (each game appears twice — home and away)
    if not row['is_home']:
        return None

    # Filter to games before this date
    prior = season_games[season_games['game_date'] < pd.to_datetime(game_date)]
    home_games = prior[prior['team'] == home]
    away_games = prior[prior['team'] == away]

    if len(home_games) < 5 or len(away_games) < 5:
        return None  # cold start — skip first few games

    # Build lightweight engine from prior data only
    # Instead of full TeamRatingEngine (which hits the DB), use pre-loaded data
    engine = _InlineEngine(prior, conn, season, team_hca=team_hca)
    is_playoffs_row = (stype == 'Playoffs')
    pred_spread = engine.predict_spread(home, away, game_date, is_playoffs=is_playoffs_row)
    pred_wp     = engine.win_prob(pred_spread)

    actual_margin = int(row['margin'])   # home margin (positive = home won)
    actual_win    = 1 if row['wl'] == 'W' else 0

    return {
        'game_id':      row['game_id'],
        'game_date':    game_date,
        'season':       season,
        'season_type':  stype,
        'home':         home,
        'away':         away,
        'pred_spread':  pred_spread,
        'pred_wp':      pred_wp,
        'actual_margin':actual_margin,
        'actual_win':   actual_win,
        'residual':     pred_spread - actual_margin,   # positive = model over-predicted home
        'direction_correct': int((pred_spread > 0) == (actual_margin > 0)),
        'home_covered_pred': int(actual_margin > pred_spread - 0.5),  # home covered own predicted line
    }


class _InlineEngine:
    """
    Lightweight version of TeamRatingEngine that works from a pre-filtered
    DataFrame (avoids re-querying the DB for every game in the backtest).

    Applies the same per-team and playoff HCA as the real TeamRatingEngine
    (via the shared compute_team_hca() helper) — a prior version of this
    class always used the flat HOME_COURT_ADJ regardless of playoff mode or
    team, which meant any playoff backtest numbers it produced didn't match
    production behavior (see CODE_REVIEW_FINDINGS.md finding #2).
    """
    from src.models.team_ratings import HOME_COURT_ADJ, PLAYOFF_HCA, REST_BONUS, B2B_PENALTY
    from src.models.team_ratings import SEASON_WEIGHT, FORM_WEIGHT, L10_MIN_GAMES, PACE_BASELINE

    def __init__(
        self, prior_games: pd.DataFrame, conn: sqlite3.Connection, season: str,
        team_hca: dict[str, float] | None = None,
    ):
        self._games = prior_games
        self._season_ratings = self._load_season_ratings(conn, season)
        # Precomputed by the caller when available (see predict_game()) since
        # this depends only on `season`, not on prior_games/game_date — the
        # same value for every game in a season's backtest. Falls back to
        # computing it here for standalone use.
        self._team_hca = team_hca if team_hca is not None else compute_team_hca(conn, season)
        self._form = self._compute_form(prior_games)
        self._ratings = self._build_ratings()

    def _load_season_ratings(self, conn, season):
        df = pd.read_sql(
            "SELECT team_abbr, net_rtg, pace FROM team_stats WHERE season=?",
            conn, params=(season,)
        )
        if df.empty:
            return {}
        from src.models.team_ratings import PACE_BASELINE
        df['net_rtg_pg'] = df['net_rtg'] * (df['pace'] / PACE_BASELINE)
        return dict(zip(df['team_abbr'], df['net_rtg_pg']))

    def _compute_form(self, games):
        from src.models.team_ratings import HOME_COURT_ADJ, L10_MIN_GAMES
        if games.empty:
            return {}
        df = games.copy()
        df['adj_margin'] = np.where(
            df['is_home'] == 1,
            df['margin'] - HOME_COURT_ADJ,
            df['margin'] + HOME_COURT_ADJ,
        )
        form = {}
        for team, grp in df.groupby('team'):
            recent = grp.sort_values('game_date').tail(10)
            if len(recent) >= L10_MIN_GAMES:
                form[team] = float(recent['adj_margin'].mean())
        return form

    def _build_ratings(self):
        from src.models.team_ratings import SEASON_WEIGHT, FORM_WEIGHT
        all_teams = set(self._season_ratings) | set(self._form)
        r = {}
        for t in all_teams:
            sr = self._season_ratings.get(t)
            fr = self._form.get(t)
            if sr is None and fr is None:
                r[t] = 0.0
            elif sr is None:
                r[t] = fr
            elif fr is None:
                r[t] = sr
            else:
                r[t] = SEASON_WEIGHT * sr + FORM_WEIGHT * fr
        return r

    def _rest_adj(self, team, game_date):
        from src.models.team_ratings import REST_BONUS, B2B_PENALTY
        g = self._games[self._games['team'] == team]
        if g.empty:
            return 0.0
        prior = g[g['game_date'] < pd.to_datetime(game_date)].sort_values('game_date')
        if prior.empty:
            return 0.0
        days = (pd.to_datetime(game_date) - prior.iloc[-1]['game_date']).days
        if days == 1:
            return -B2B_PENALTY
        elif days == 2:
            return 0.0
        elif days == 3:
            return REST_BONUS
        return REST_BONUS * 1.5

    def predict_spread(self, home, away, game_date=None, is_playoffs=False):
        hr = self._ratings.get(home, 0.0)
        ar = self._ratings.get(away, 0.0)
        hca = self.PLAYOFF_HCA if is_playoffs else self._team_hca.get(home, self.HOME_COURT_ADJ)
        s = (hr - ar) + hca
        if game_date:
            s += self._rest_adj(home, game_date) - self._rest_adj(away, game_date)
        return round(s, 2)

    def win_prob(self, spread, std=11.0):
        p = float(scipy_stats.norm.cdf(spread / std))
        return round(float(np.clip(p, 0.02, 0.98)), 4)


# ── Metrics ───────────────────────────────────────────────────────────────────

def _pct(x):
    return f"{x:.1%}" if x is not None and not np.isnan(x) else " N/A"

def _f(x, d=2):
    return f"{x:.{d}f}" if x is not None and not np.isnan(x) else " N/A"


def compute_metrics(df: pd.DataFrame) -> dict:
    """Compute full suite of metrics from backtest results."""
    n = len(df)
    if n == 0:
        return {}

    mae       = df['residual'].abs().mean()
    mean_err  = df['residual'].mean()
    dir_acc   = df['direction_correct'].mean()
    cover_own = df['home_covered_pred'].mean()

    # Brier score for win probability
    brier = ((df['pred_wp'] - df['actual_win']) ** 2).mean()

    # By confidence tier
    tiers = {}
    for label, lo, hi in [('lean', LEAN_THRESHOLD, STRONG_THRESHOLD),
                           ('strong', STRONG_THRESHOLD, 99)]:
        mask = df['pred_spread'].abs().between(lo, hi) if label == 'lean' else df['pred_spread'].abs() >= hi
        sub = df[mask]
        if len(sub) > 5:
            fav_wins = (
                ((sub['pred_spread'] > 0) & (sub['actual_margin'] > 0)) |
                ((sub['pred_spread'] < 0) & (sub['actual_margin'] < 0))
            ).mean()
            tiers[label] = {
                'n':       len(sub),
                'dir_acc': fav_wins,
                'cover':   sub['home_covered_pred'].mean(),
                'mae':     sub['residual'].abs().mean(),
            }

    # Reliability by pred_wp decile
    reliability = []
    for lo, hi in [(0.0,0.5),(0.5,0.6),(0.6,0.7),(0.7,0.8),(0.8,1.0)]:
        sub = df[df['pred_wp'].between(lo, hi) if lo < 0.5 else df['pred_wp'].between(lo, hi)]
        if len(sub) > 5:
            reliability.append({
                'bucket':  f"[{lo:.0%},{hi:.0%})",
                'n':       len(sub),
                'pred_wp': sub['pred_wp'].mean(),
                'actual':  sub['actual_win'].mean(),
                'err':     sub['actual_win'].mean() - sub['pred_wp'].mean(),
            })

    return {
        'n':        n,
        'mae':      mae,
        'mean_err': mean_err,
        'dir_acc':  dir_acc,
        'cover_own':cover_own,
        'brier':    brier,
        'tiers':    tiers,
        'reliability': reliability,
    }


def print_report(label: str, m: dict) -> None:
    sep = '─' * 66
    print(f"\n{sep}")
    print(f"  {label}  (n={m['n']:,} games)")
    print(f"{sep}")
    print(f"  Spread MAE:              {_f(m['mae'])} pts")
    print(f"  Spread mean error:       {_f(m['mean_err'],2)} pts  (+ = over-predicted home)")
    print(f"  Directional accuracy:    {_pct(m['dir_acc'])}  (% correct winner)")
    print(f"  Home covers own spread:  {_pct(m['cover_own'])}")
    print(f"  Win-prob Brier score:    {_f(m['brier'],4)}  (lower = better; 0.25 = coin flip)")

    if m.get('tiers'):
        print(f"\n  By confidence tier (|model spread| threshold):")
        for tier, t in m['tiers'].items():
            print(f"    {tier.upper():<8}: {_pct(t['dir_acc'])} dir acc  "
                  f"{_pct(t['cover'])} own-cover  MAE {_f(t['mae'])}  n={t['n']:,}")

    if m.get('reliability'):
        print(f"\n  Win-probability reliability:")
        print(f"    {'Bucket':<14} {'N':>5} {'Pred P':>7} {'Actual':>7} {'Error':>8}")
        for r in m['reliability']:
            print(f"    {r['bucket']:<14} {r['n']:>5} {r['pred_wp']:>7.1%} "
                  f"{r['actual']:>7.1%} {r['err']:>+8.1%}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_backtest(seasons: list[str], season_types: list[str] | None = None) -> pd.DataFrame:
    """
    Run the full backtest.  Returns a DataFrame of all game predictions.
    """
    if season_types is None:
        season_types = ['Regular Season']

    conn = sqlite3.connect(str(DB_PATH))
    all_results = []

    for season in seasons:
        print(f"  Processing {season} ({', '.join(season_types)})...", end='', flush=True)
        season_games = _load_team_games(conn, [season], season_types)
        if season_games.empty:
            print(" no data")
            continue

        # Computed once per season (depends only on season, not per-game date)
        team_hca = compute_team_hca(conn, season)

        # Only process one row per game (home team row)
        home_rows = season_games[season_games['is_home'] == 1]
        count = 0
        for _, row in home_rows.iterrows():
            result = predict_game(conn, row, season_games, team_hca)
            if result:
                all_results.append(result)
                count += 1

        print(f" {count:,} games predicted")

    conn.close()

    if not all_results:
        return pd.DataFrame()

    return pd.DataFrame(all_results)


def main():
    parser = argparse.ArgumentParser(description="Backtest NBA team spread/ML model")
    parser.add_argument('--seasons', nargs='+', default=SEASONS)
    parser.add_argument('--playoffs', action='store_true',
                        help='Include playoff games')
    parser.add_argument('--save', action='store_true',
                        help='Save results to data/processed/team_backtest.csv')
    args = parser.parse_args()

    stypes = ['Regular Season']
    if args.playoffs:
        stypes += ['Playoffs', 'PlayIn']

    print(f"\n{'='*66}")
    print(f"  TEAM MODEL BACKTEST — {', '.join(args.seasons)}")
    print(f"  Season types: {', '.join(stypes)}")
    print(f"  Lean threshold: |spread| >= {LEAN_THRESHOLD}  Strong: >= {STRONG_THRESHOLD}")
    print(f"{'='*66}\n")

    df = run_backtest(args.seasons, stypes)
    if df.empty:
        print("No results.")
        return

    # Overall
    m = compute_metrics(df)
    print_report(f"ALL SEASONS COMBINED ({', '.join(args.seasons)})", m)

    # Per season
    print(f"\n\n{'='*66}")
    print(f"  PER-SEASON BREAKDOWN")
    print(f"{'='*66}")
    for season in args.seasons:
        sub = df[df['season'] == season]
        if len(sub) == 0:
            continue
        m = compute_metrics(sub)
        print_report(season, m)

    # Per season-type (if mixed)
    if args.playoffs:
        print(f"\n\n{'='*66}")
        print(f"  BY SEASON TYPE")
        print(f"{'='*66}")
        for st in ['Regular Season', 'Playoffs', 'PlayIn']:
            sub = df[df['season_type'] == st]
            if len(sub) < 10:
                continue
            m = compute_metrics(sub)
            print_report(st, m)

    if args.save:
        out = Path(__file__).parent.parent / 'data' / 'processed' / 'team_backtest.csv'
        df.to_csv(out, index=False)
        print(f"\n  Saved to {out}")

    print(f"\n{'='*66}\n")
    return df


if __name__ == '__main__':
    main()
