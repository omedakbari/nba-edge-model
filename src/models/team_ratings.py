"""
Team Power Rating Engine.

Produces a per-team adjusted margin rating that can be used to predict
game spreads and win probabilities.

Design:
  - Full-season component: SRS (Simple Rating System) — iteratively solved
    opponent-quality-adjusted margin. Replaces raw NET_RTG as the primary
    season signal. Rating[i] = avg_adj_margin[i] + avg(rating[opponents_of_i]).
    Converges in ~40 iterations; zero-mean normalized.
  - Recent-form component: rolling L10 adjusted margin from actual game results.
    Margins are home-court-neutralized before averaging.
  - Per-team HCA: historical home-road split from last 3 seasons. DEN, MIL, OKC
    significantly above 2.5pt league average; WAS, SAC, GSW below.
  - Combined: 0.65 * SRS + 0.35 * L10 form. Current-season strength is
    shrunk toward prior-season NET_RTG (regressed 20% toward average), with
    a 20-game prior to avoid opening-week one-game outliers.

Predicted spread (from home team perspective):
    model_spread = home_rating - away_rating + team_hca[home_team]

Playoff adjustments:
    HCA raised to PLAYOFF_HCA (3.0 pts) — per-team HCA not used in playoffs
    due to limited sample size and different crowd dynamics.
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
from config.settings import TEAM_PRIOR_GAMES, TEAM_PRIOR_RETENTION

warnings.filterwarnings("ignore")


# ── Constants ─────────────────────────────────────────────────────────────────

HOME_COURT_ADJ  = 2.5   # league-avg regular season HCA (fallback when no team HCA)
PLAYOFF_HCA     = 3.0   # HCA in playoffs (flat — less historical data per team)
PACE_BASELINE   = 100.0 # per-100-poss → per-game conversion denominator
REST_BONUS      = 0.75  # pts per extra rest day vs opponent (capped at 2 days diff)
B2B_PENALTY     = 2.3   # pts deducted for a team on back-to-back
SEASON_WEIGHT   = 0.65  # blend: SRS (full-season) share
FORM_WEIGHT     = 0.35  # blend: recent L10 share
L10_MIN_GAMES   = 5     # minimum games for L10 form to carry weight
SPREAD_STD      = 11.0  # historical NBA spread std-dev (points) — for P(cover) calc

# Win-probability calibration: fitted from 4-season backtest (4,604 games).
# Raw logistic probabilities are over-confident for heavy favorites.
# p_cal = sigmoid(WIN_PROB_SCALE * logit(p_raw))
# Fitted value 0.664 minimizes Brier score: 0.2186 → 0.2147.
# Effect: raw 70% → calibrated 63.7%, raw 80% → calibrated 71.5%.
WIN_PROB_SCALE  = 0.664
# Legacy scale predates SRS and the opening-season prior. Retain the diagnostic
# estimate for reproducibility, but it is not validated for betting decisions.
PROBABILITY_VALIDATED = False

# ── SRS / HCA tuning ──────────────────────────────────────────────────────────

SRS_ITER        = 40    # iterations for SRS convergence (stable after ~25)
SRS_MIN_GAMES   = 10   # min current-season games before SRS trusted over net_rtg
HCA_HIST_N      = 3    # past seasons to use for per-team HCA computation
HCA_MIN_SPLIT   = 20   # min home (or away) games for team HCA to be used
HCA_CAP_LO      = 0.0  # per-team HCA floor (pts) — prevent sub-zero home advantages
HCA_CAP_HI      = 5.5  # per-team HCA ceiling (pts) — prevent outlier inflation

# All known seasons in chronological order (extend as new seasons are added)
_ALL_SEASONS = [
    '2019-20', '2020-21', '2021-22', '2022-23', '2023-24', '2024-25', '2025-26', '2026-27',
]


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


# ── Shared HCA helpers ──────────────────────────────────────────────────────
# Module-level so both TeamRatingEngine and scripts/backtest_team_model.py's
# lightweight _InlineEngine can share one implementation instead of each
# maintaining its own (the backtest's copy previously never applied per-team
# or playoff HCA — see CODE_REVIEW_FINDINGS.md finding #2).

def _past_seasons(season: str, n: int) -> list[str]:
    """Return the N seasons immediately before `season`."""
    try:
        idx = _ALL_SEASONS.index(season)
    except ValueError:
        idx = len(_ALL_SEASONS)
    return _ALL_SEASONS[max(0, idx - n):idx]


def compute_team_hca(
    conn: sqlite3.Connection, season: str, hist_n: int = HCA_HIST_N
) -> dict[str, float]:
    """
    Per-team home-court advantage from the `hist_n` seasons before `season`.

    Method: HCA_i = (avg_home_margin_i - avg_road_margin_i) / 2
    Margins are from each team's perspective, so this cleanly isolates
    venue effect from team quality.

    Returns {team_abbr: hca_pts}. A team is simply absent (caller falls back
    to HOME_COURT_ADJ) if fewer than HCA_MIN_SPLIT home or away games found.
    """
    past = _past_seasons(season, hist_n)
    if not past:
        return {}

    hist = _load_team_games(conn, past, ['Regular Season'])
    if hist.empty:
        return {}

    hca: dict[str, float] = {}
    for team, grp in hist.groupby('team'):
        home_g = grp[grp['is_home'] == 1]['margin']
        away_g = grp[grp['is_home'] == 0]['margin']
        if len(home_g) < HCA_MIN_SPLIT or len(away_g) < HCA_MIN_SPLIT:
            continue
        raw = float((home_g.mean() - away_g.mean()) / 2)
        hca[team] = float(np.clip(raw, HCA_CAP_LO, HCA_CAP_HI))

    return hca


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

    probability_validated = PROBABILITY_VALIDATED

    def __init__(
        self,
        conn: sqlite3.Connection,
        season: str,
        through_date: str | None = None,
        include_playoffs: bool = False,
        as_of: str | None = None,
        aggregate_mode: str = 'snapshot',
    ):
        self.conn = conn
        self.season = season
        self.through_date = through_date
        self.as_of = as_of
        if aggregate_mode not in ('snapshot', 'game_logs'):
            raise ValueError('Unknown aggregate mode')
        self.aggregate_mode = aggregate_mode

        stypes = ['Regular Season', 'Playoffs', 'PlayIn'] if include_playoffs else ['Regular Season']

        # Load game results for the current season
        self._games = _load_team_games(conn, [season], stypes, through_date)

        # Load season-level NET_RTG from team_stats (cold-start fallback)
        self._season_ratings = self._load_season_ratings(season)

        # Per-team historical HCA from past seasons
        self._team_hca = self._load_team_hca()

        # SRS: opponent-quality-adjusted full-season rating
        self._srs = self._compute_srs()

        # Rolling L10 form
        self._form = self._compute_form()

        # Previous-season strength supplies an opening-week prior. Require
        # actual prior-season game history rather than trusting a missing rating.
        prior_seasons = self._get_past_seasons(1)
        self._prior_ratings = self._load_season_ratings(prior_seasons[0]) if prior_seasons else {}
        prior_games = _load_team_games(conn, prior_seasons, ['Regular Season'], through_date) if prior_seasons else pd.DataFrame()
        self._prior_counts = prior_games.groupby('team')['game_id'].nunique().to_dict() if not prior_games.empty else {}

        # Combined ratings dict: team → adjusted points rating
        self._ratings = self._build_combined_ratings()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get_past_seasons(self, n: int) -> list[str]:
        """Return the N seasons immediately before self.season."""
        return _past_seasons(self.season, n)

    def _load_team_hca(self) -> dict[str, float]:
        """Per-team home-court advantage from historical seasons. See compute_team_hca()."""
        return compute_team_hca(self.conn, self.season)

    def _compute_srs(self) -> dict[str, float]:
        """
        Simple Rating System: iteratively solve opponent-adjusted season ratings.

        Formula (converges to self-consistent solution):
            rating[i] = avg_adj_margin[i] + avg(rating[opponents_of_i])

        Where adj_margin neutralizes each game's HCA using the team's historical
        per-team HCA (falls back to HOME_COURT_ADJ when not available).

        Result is zero-mean normalized across all teams with data.
        Returns {} when insufficient season data (early-season cold start).
        """
        df = self._games.copy()
        if df.empty or len(df['game_id'].unique()) < 15:
            return {}

        teams = df['team'].unique().tolist()
        srs: dict[str, float] = {t: 0.0 for t in teams}

        for _ in range(SRS_ITER):
            new_srs: dict[str, float] = {}
            for team in teams:
                t_games = df[df['team'] == team]
                if len(t_games) < 5:
                    new_srs[team] = 0.0
                    continue

                # Neutralize home-court using per-team HCA when available
                hca_val = self._team_hca.get(team, HOME_COURT_ADJ)
                adj = np.where(
                    t_games['is_home'].values == 1,
                    t_games['margin'].values - hca_val,
                    t_games['margin'].values + hca_val,
                )

                # Strength of schedule: avg SRS of opponents faced
                opp_srs = t_games['opp'].map(srs).fillna(0.0).values
                new_srs[team] = float(np.mean(adj)) + float(np.mean(opp_srs))

            # Zero-mean normalize each iteration
            mean_v = float(np.mean(list(new_srs.values())))
            srs = {t: v - mean_v for t, v in new_srs.items()}

        return srs

    def _load_season_ratings(self, season: str) -> dict[str, float]:
        """
        Load season NET_RTG from team_stats (cold-start fallback only).
        Scale from per-100-poss to points-per-game using average pace.
        Returns {team_abbr: net_rtg_per_game}.
        """
        if getattr(self, 'aggregate_mode', 'snapshot') == 'game_logs':
            # Versioned cold-start alternative, reconstructible at every date.
            # Mean scoring margin is in points/game; it is not represented as
            # an archived official NET_RTG snapshot.
            games = _load_team_games(self.conn, [season], ['Regular Season'], self.through_date)
            return games.groupby('team')['margin'].mean().to_dict()
        df = pd.read_sql(
            "SELECT team_abbr, net_rtg, pace, updated_at FROM team_stats WHERE season=?",
            self.conn, params=(season,)
        )
        # team_stats is a mutable season aggregate, not an as-of history. A
        # historical run must not consume a later current-season snapshot.
        if self.through_date and season == self.season and not df.empty:
            from src.utils.slate import PACIFIC, slate_today
            if getattr(self, 'as_of', None):
                cutoff = pd.Timestamp(self.as_of)
                if cutoff.tzinfo is None:
                    raise ValueError('as_of requires an explicit timezone')
            elif self.through_date == slate_today().isoformat():
                cutoff = pd.Timestamp.now(tz='UTC')
            else:
                cutoff = pd.Timestamp(self.through_date, tz=PACIFIC)
            def instant(value):
                t = pd.Timestamp(value)
                return t.tz_localize(PACIFIC).tz_convert('UTC') if t.tzinfo is None else t.tz_convert('UTC')
            updated = df['updated_at'].map(instant)
            df = df[updated < cutoff]
        if df.empty:
            return {}
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
                form[team] = None
            else:
                form[team] = float(recent['adj_margin'].mean())
        return form

    def _build_combined_ratings(self) -> dict[str, float]:
        """
        Blend SRS (opponent-adjusted full-season) with L10 form.

        Primary path (>= SRS_MIN_GAMES): SEASON_WEIGHT * srs + FORM_WEIGHT * L10
        Cold-start current signal uses net_rtg + L10 blend. The result is
        shrunk toward prior-season strength with a 20-game prior.

        Returns {team_abbr: combined_rating} in points-per-game units.
        """
        all_teams = (set(self._srs.keys()) | set(self._season_ratings.keys())
                     | set(self._form.keys()) | set(self._prior_ratings.keys()))
        ratings: dict[str, float] = {}

        for team in all_teams:
            srs_r    = self._srs.get(team)
            season_r = self._season_ratings.get(team)
            form_r   = self._form.get(team)

            # Count how many games this team played in current season
            t_games = self._games[self._games['team'] == team]
            n_games = len(t_games['game_id'].unique()) if not t_games.empty else 0

            if srs_r is not None and n_games >= SRS_MIN_GAMES:
                # SRS available and reliable: use as primary full-season signal
                primary = srs_r
            elif season_r is not None:
                # Cold start: fall back to official net_rtg
                primary = season_r
            elif srs_r is not None:
                primary = srs_r
            else:
                primary = 0.0

            if form_r is not None:
                ratings[team] = SEASON_WEIGHT * primary + FORM_WEIGHT * form_r
            else:
                ratings[team] = primary

        for team in ratings:
            if self._prior_counts.get(team, 0) >= TEAM_PRIOR_GAMES and team in self._prior_ratings:
                n = self._games.loc[self._games['team'] == team, 'game_id'].nunique()
                weight = n / (n + TEAM_PRIOR_GAMES)
                ratings[team] = weight * ratings[team] + (1 - weight) * TEAM_PRIOR_RETENTION * self._prior_ratings[team]
        return ratings

    def has_rating_history(self, team):
        current = self._games.loc[self._games['team'] == team, 'game_id'].nunique()
        return current >= SRS_MIN_GAMES or (self._prior_counts.get(team, 0) >= TEAM_PRIOR_GAMES
                                          and team in self._prior_ratings)

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

    def get_team_hca(self, team: str) -> float:
        """Return per-team HCA (pts). Falls back to HOME_COURT_ADJ."""
        return self._team_hca.get(team, HOME_COURT_ADJ)

    def predict_spread(
        self,
        home_team: str,
        away_team: str,
        game_date: str | None = None,
        is_playoffs: bool = False,
        series_g1_winner: str | None = None,
        series_context: dict | None = None,
    ) -> float:
        """
        Predict point spread from home team's perspective.
        Positive = home favored, negative = away favored.

        Args:
            home_team: abbreviation of home team
            away_team: abbreviation of away team
            game_date: YYYY-MM-DD (used for rest adjustment; optional)
            is_playoffs: use flat PLAYOFF_HCA (3.0 pts) — per-team HCA not
                         applied in playoffs due to limited sample size.
            series_context: auto-derived series state dict; takes priority when
                            game_num > 1.  Momentum scaled by series lead:
                            +0.75 pts per game led (home perspective), capped ±2.0.
            series_g1_winner: legacy ±1.5 pt uplift; used when series_context is None.
        """
        home_r = self.get_rating(home_team)
        away_r = self.get_rating(away_team)

        if is_playoffs:
            hca = PLAYOFF_HCA
        else:
            # Per-team HCA: home team's historically measured venue advantage
            hca = self._team_hca.get(home_team, HOME_COURT_ADJ)

        spread = (home_r - away_r) + hca

        # Rest/B2B adjustments
        if game_date:
            home_rest = self._rest_adjustment(home_team, game_date)
            away_rest = self._rest_adjustment(away_team, game_date)
            spread += (home_rest - away_rest)

        # Series momentum — prefer richer series_context over legacy g1_winner
        if series_context and series_context.get('game_num', 1) > 1:
            lead = series_context.get('home_wins', 0) - series_context.get('away_wins', 0)
            spread += max(-2.0, min(2.0, lead * 0.75))
        elif series_g1_winner == home_team:
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
        series_context: dict | None = None,
    ) -> float:
        """
        Return UNVALIDATED legacy-scaled diagnostic P(home team wins).
        Step 1: raw logistic from spread.
        Step 2: Platt-style scaling to correct over-confidence (fitted from backtest).
        """
        from scipy.special import expit, logit
        spread = self.predict_spread(
            home_team, away_team, game_date, is_playoffs,
            series_g1_winner, series_context,
        )
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
            srs_r    = self._srs.get(team)
            hca_v    = self._team_hca.get(team)
            rows.append({
                'team':      team,
                'rating':    round(rating, 2),
                'srs':       round(srs_r, 2) if srs_r is not None else None,
                'season_pg': round(season_r, 2) if season_r is not None else None,
                'form_l10':  round(form_r, 2) if form_r is not None else None,
                'hca':       round(hca_v, 2) if hca_v is not None else None,
                'games':     len(self._games[self._games['team'] == team]),
            })
        return pd.DataFrame(rows)
