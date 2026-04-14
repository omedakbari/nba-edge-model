"""
Team Power Rating Engine.

Produces a per-team adjusted margin rating that can be used to predict
game spreads and win probabilities.

Design:
  - Season-level component: NET_RTG from team_stats (ORTG - DRTG), scaled to
    points-per-game. Uses pace to convert per-100-poss to per-game.
  - Recent-form component: rolling L10 adjusted margin from actual game results.
    Margins are home-court-neutralized before averaging, so ratings are
    "true strength" independent of schedule home/away balance.
  - Combined: 0.65 * season + 0.35 * L10 form  (season dominates early,
    form gets weight once L10 games are available)

Predicted spread (from home team perspective):
    model_spread = home_rating - away_rating + HOME_COURT_ADJ

Playoff adjustments:
    HOME_COURT_ADJ raised to PLAYOFF_HCA (3.0 pts).
    Series momentum: G1 winner gets small uplift in G2.

Usage:
    from src.models.team_ratings import TeamRatingEngine
    engine = TeamRatingEngine(conn, season='2024-25')
    spread = engine.predict_spread('BOS', 'NYK', is_playoffs=False)
    win_p  = engine.win_probability('BOS', 'NYK', is_playoffs=False)
"""

import sqlite3
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

warnings.filterwarnings("ignore")


# ── Constants ─────────────────────────────────────────────────────────────────

HOME_COURT_ADJ  = 2.5   # regular season avg HCA in points
PLAYOFF_HCA     = 3.0   # HCA is worth slightly more in high-stakes series
PACE_BASELINE   = 100.0 # per-100-poss → per-game conversion denominator
REST_BONUS      = 0.75  # pts per extra rest day vs opponent (capped at 2 days diff)
B2B_PENALTY     = 2.3   # pts deducted for a team on back-to-back
SEASON_WEIGHT   = 0.65  # blend: season NET_RTG share
FORM_WEIGHT     = 0.35  # blend: recent L10 share
L10_MIN_GAMES   = 5     # minimum games for L10 form to carry weight
SPREAD_STD      = 11.0  # historical NBA spread std-dev (points) — for P(cover) calc

# Win-probability calibration: fitted from 4-season backtest (4,604 games).
# Raw logistic probabilities are over-confident for heavy favorites.
# p_cal = sigmoid(WIN_PROB_SCALE * logit(p_raw))
# Fitted value 0.664 minimizes Brier score: 0.2186 → 0.2147.
# Effect: raw 70% → calibrated 63.7%, raw 80% → calibrated 71.5%.
WIN_PROB_SCALE  = 0.664


# ── SQL ───────────────────────────────────────────────────────────────────────

_TEAM_GAMES_SQL = """
WITH team_scores AS (
    SELECT game_id, team_abbr, SUM(pts) AS team_pts
    FROM game_logs
    GROUP BY game_id, team_abbr
),
game_meta AS (
    SELECT DISTINCT game_id, game_date, season, season_type,
                    team_abbr, opp_abbr, is_home, wl
    FROM game_logs
)
SELECT
    m.game_id,
    m.game_date,
    m.season,
    m.season_type,
    m.team_abbr  AS team,
    m.opp_abbr   AS opp,
    m.is_home,
    ts.team_pts,
    opp_ts.team_pts AS opp_pts,
    ts.team_pts - opp_ts.team_pts AS margin,
    m.wl
FROM game_meta m
JOIN team_scores ts     ON m.game_id = ts.game_id AND m.team_abbr  = ts.team_abbr
JOIN team_scores opp_ts ON m.game_id = opp_ts.game_id AND m.opp_abbr = opp_ts.team_abbr
{where_clause}
ORDER BY m.game_date, m.game_id
"""


def _load_team_games(
    conn: sqlite3.Connection,
    seasons: list[str],
    season_types: list[str] | None = None,
    through_date: str | None = None,
) -> pd.DataFrame:
    """Load team-game records (one row per team per game) for given seasons."""
    where_parts = [f"m.season IN ({','.join('?' * len(seasons))})"]
    params: list = list(seasons)

    if season_types:
        where_parts.append(f"m.season_type IN ({','.join('?' * len(season_types))})")
        params += season_types

    if through_date:
        where_parts.append("m.game_date < ?")
        params.append(through_date)

    where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
    sql = _TEAM_GAMES_SQL.format(where_clause=where)
    df = pd.read_sql(sql, conn, params=params)
    df['game_date'] = pd.to_datetime(df['game_date'])
    return df


# ── Rating Engine ─────────────────────────────────────────────────────────────

class TeamRatingEngine:
    """
    Computes team power ratings and spread/win-probability predictions.

    Args:
        conn: open sqlite3 Connection
        season: current season string e.g. '2024-25'
        through_date: only use games before this date (YYYY-MM-DD) — prevents
                      leakage when backtesting. None = use all available data.
        include_playoffs: include playoff games in rolling-form calculation.
                          False for regular-season predictions (default).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        season: str,
        through_date: str | None = None,
        include_playoffs: bool = False,
    ):
        self.conn = conn
        self.season = season
        self.through_date = through_date

        stypes = ['Regular Season', 'Playoffs', 'PlayIn'] if include_playoffs else ['Regular Season']

        # Load game results for the current season
        self._games = _load_team_games(conn, [season], stypes, through_date)

        # Load season-level NET_RTG from team_stats
        self._season_ratings = self._load_season_ratings(season)

        # Compute rolling form for each team
        self._form = self._compute_form()

        # Combined ratings dict: team → adjusted points rating
        self._ratings = self._build_combined_ratings()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _load_season_ratings(self, season: str) -> dict[str, float]:
        """
        Load season NET_RTG from team_stats.
        Scale from per-100-poss to points-per-game using average pace.
        Returns {team_abbr: net_rtg_per_game}.
        """
        df = pd.read_sql(
            "SELECT team_abbr, net_rtg, pace FROM team_stats WHERE season=?",
            self.conn, params=(season,)
        )
        if df.empty:
            return {}
        # Convert per-100-poss to per-game: net_rtg_pg = net_rtg * (pace / 100)
        # pace = possessions per team per 48 min.
        df['net_rtg_pg'] = df['net_rtg'] * (df['pace'] / PACE_BASELINE)
        return dict(zip(df['team_abbr'], df['net_rtg_pg']))

    def _compute_form(self) -> dict[str, float]:
        """
        Compute rolling L10 adjusted margin per team.
        Adjust: home team margin -= HCA, away team margin += HCA.
        This neutralizes schedule home/away balance.
        Returns {team_abbr: L10_adjusted_margin}.
        """
        df = self._games.copy()
        if df.empty:
            return {}

        # Neutralize home-court: measure "true" performance
        df['adj_margin'] = np.where(
            df['is_home'] == 1,
            df['margin'] - HOME_COURT_ADJ,
            df['margin'] + HOME_COURT_ADJ,
        )

        form = {}
        for team, grp in df.groupby('team'):
            recent = grp.sort_values('game_date').tail(10)
            n = len(recent)
            if n < L10_MIN_GAMES:
                form[team] = None   # not enough games — fall back to season rating
            else:
                form[team] = float(recent['adj_margin'].mean())
        return form

    def _build_combined_ratings(self) -> dict[str, float]:
        """
        Blend season NET_RTG (per-game scaled) with L10 adjusted margin.
        Returns {team_abbr: combined_rating} in points-per-game units.
        """
        all_teams = set(self._season_ratings.keys()) | set(self._form.keys())
        ratings = {}
        for team in all_teams:
            season_r = self._season_ratings.get(team)
            form_r   = self._form.get(team)

            if season_r is None and form_r is None:
                ratings[team] = 0.0
            elif season_r is None:
                ratings[team] = form_r
            elif form_r is None:
                ratings[team] = season_r
            else:
                ratings[team] = SEASON_WEIGHT * season_r + FORM_WEIGHT * form_r

        return ratings

    # ── Rest / B2B adjustment ─────────────────────────────────────────────────

    def _rest_adjustment(self, team: str, game_date: str) -> float:
        """
        Return rest-days adjustment for a team on game_date.
        Negative = on B2B, positive = extra rest.
        """
        df = self._games[self._games['team'] == team].copy()
        if df.empty:
            return 0.0
        df = df.sort_values('game_date')
        prior = df[df['game_date'] < pd.to_datetime(game_date)]
        if prior.empty:
            return 0.0
        last_game = prior.iloc[-1]['game_date']
        days = (pd.to_datetime(game_date) - last_game).days

        if days == 1:    # back-to-back
            return -B2B_PENALTY
        elif days == 2:  # normal rest
            return 0.0
        elif days == 3:  # one extra day
            return REST_BONUS
        else:            # 2+ extra days (cap)
            return REST_BONUS * 1.5

    # ── Public API ────────────────────────────────────────────────────────────

    def get_rating(self, team: str) -> float:
        """Return combined power rating for a team (higher = stronger)."""
        return self._ratings.get(team, 0.0)

    def predict_spread(
        self,
        home_team: str,
        away_team: str,
        game_date: str | None = None,
        is_playoffs: bool = False,
        series_g1_winner: str | None = None,
    ) -> float:
        """
        Predict point spread from home team's perspective.
        Positive = home favored, negative = away favored.

        Args:
            home_team: abbreviation of home team
            away_team: abbreviation of away team
            game_date: YYYY-MM-DD (used for rest adjustment; optional)
            is_playoffs: use playoff HCA if True
            series_g1_winner: if G2+, abbreviation of team that won G1
        """
        home_r = self.get_rating(home_team)
        away_r = self.get_rating(away_team)
        hca = PLAYOFF_HCA if is_playoffs else HOME_COURT_ADJ

        spread = (home_r - away_r) + hca

        # Rest/B2B adjustments
        if game_date:
            home_rest = self._rest_adjustment(home_team, game_date)
            away_rest = self._rest_adjustment(away_team, game_date)
            spread += (home_rest - away_rest)

        # Series momentum (G2+): G1 winner gets ~1.5 pt uplift
        if series_g1_winner == home_team:
            spread += 1.5
        elif series_g1_winner == away_team:
            spread -= 1.5

        return round(spread, 2)

    def win_probability(
        self,
        home_team: str,
        away_team: str,
        game_date: str | None = None,
        is_playoffs: bool = False,
        series_g1_winner: str | None = None,
    ) -> float:
        """
        Return calibrated model P(home team wins).
        Step 1: raw logistic from spread.
        Step 2: Platt-style scaling to correct over-confidence (fitted from backtest).
        """
        from scipy.special import expit, logit
        spread = self.predict_spread(home_team, away_team, game_date, is_playoffs, series_g1_winner)
        p_raw = float(scipy_stats.norm.cdf(spread / SPREAD_STD))
        p_raw = float(np.clip(p_raw, 0.02, 0.98))
        p_cal = float(expit(WIN_PROB_SCALE * logit(p_raw)))
        return round(float(np.clip(p_cal, 0.02, 0.98)), 4)

    def all_ratings(self) -> pd.DataFrame:
        """Return DataFrame of all team ratings for inspection."""
        rows = []
        for team, rating in sorted(self._ratings.items(), key=lambda x: -x[1]):
            season_r = self._season_ratings.get(team)
            form_r   = self._form.get(team)
            rows.append({
                'team':      team,
                'rating':    round(rating, 2),
                'season_pg': round(season_r, 2) if season_r is not None else None,
                'form_l10':  round(form_r, 2) if form_r is not None else None,
                'games':     len(self._games[self._games['team'] == team]),
            })
        return pd.DataFrame(rows)
