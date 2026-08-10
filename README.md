# NBA Edge Model

End-to-end NBA player props prediction system: data ingestion → feature engineering → ML models → probability calibration → daily picks output.

## Methodology

**1. Data pipeline** — pulls 7 seasons of player game logs from the NBA API into SQLite. 129,534 player-game rows across 1,178 players (2019–2026). Incremental daily update mode re-pulls the current season to pick up new games (`--update`).

**2. Feature engineering** — rolling windows (L5/L10/L20), hit-rate frequencies, pace/matchup context, rest and travel flags, opponent defensive ratings. ~125 features per player-game.

**3. Model training** — `HistGradientBoostingRegressor` per stat (PTS, REB, AST, FG3M, PRA, REB_AST). Trained on 2019–2024 (5 seasons, 59,950 rows), validated on 2024-25, tested on 2025-26. Key hyperparameters: `max_depth=3`, `min_samples_leaf=30`, `l2_regularization=5.0`, `max_features=0.6`. Feature selection drops inter-correlated features (r > 0.92) and keeps the top 40 by target correlation.

**4. Probability calibration** — predictions are blended 40% L10-baseline / 60% model (`config.settings.BLEND_L10_WEIGHT`), then mapped to P(over line) via a Gaussian CDF using the player's rolling standard deviation. A minimum-minutes filter (min_L10 ≥ 20) removes garbage-time noise before confidence scoring. The 40% weight is a single value unified across every prediction path in the codebase (previously three different, undocumented ratios — 0%, 50%, and 65% — were hardcoded independently in different scripts). It was derived empirically: pooling all 112,422 picks across all 6 stats from a full calibration audit and grid-searching L10 weight in [0.0, 1.0] for the value minimizing pooled Brier score (see `config/settings.py`'s `BLEND_L10_WEIGHT` comment and `data/diagnostics/blend_audit.csv` for the full derivation, including per-stat optimal weights, which vary from 0.15 to 0.65 — this single pooled value is a compromise across stats, not independently optimal for each one).

**5. Live picks** — auto-fetches today's NBA schedule and team rosters from NBA.com, builds current-form features for each player, and outputs two SAFE Flex cards. No manual player ID input required.

**6. Backtest** — held-out 2025-26 season. Hit rates reported per stat at multiple confidence thresholds. Composite markets (PRA, REB_AST) use summed component model predictions rather than a single composite model.

## Backtest Results — 2025-26 Season (held-out test set)

| Stat    | Threshold | Picks  | Hit Rate |
|---------|-----------|-------:|---------:|
| PTS     | P ≥ 55%   |  4,237 | 56.3%    |
| PTS     | P ≥ 60%   |  1,336 | 61.4%    |
| PTS     | P ≥ 65%   |    348 | 64.4%    |
| PTS     | P ≥ 70%   |     88 | 69.3%    |
| PTS     | P ≥ 75%   |     20 | 75.0%    |
| REB     | P ≥ 55%   |  4,196 | 56.6%    |
| REB     | P ≥ 60%   |  1,457 | 60.5%    |
| REB     | P ≥ 65%   |    523 | 64.2%    |
| REB     | P ≥ 70%   |    193 | 64.8%    |
| REB     | P ≥ 75%   |     87 | 65.5%    |
| AST     | P ≥ 55%   |  5,058 | 55.7%    |
| AST     | P ≥ 60%   |  1,789 | 60.4%    |
| AST     | P ≥ 65%   |    641 | 64.4%    |
| AST     | P ≥ 70%   |    254 | 63.8%    |
| AST     | P ≥ 75%   |    111 | 67.6%    |
| PRA     | P ≥ 55%   |  4,262 | 58.2%    |
| PRA     | P ≥ 60%   |  1,206 | 62.7%    |
| PRA     | P ≥ 65%   |    299 | 57.9%    |
| PRA     | P ≥ 70%   |     61 | 57.4%    |
| PRA     | P ≥ 75%   |     17 | 52.9%    |
| REB_AST | P ≥ 55%   |  4,647 | 57.3%    |
| REB_AST | P ≥ 60%   |  1,487 | 62.1%    |
| REB_AST | P ≥ 65%   |    425 | 64.5%    |
| REB_AST | P ≥ 70%   |    129 | 69.0%    |
| REB_AST | P ≥ 75%   |     27 | 74.1%    |

FG3M excluded from daily picks: hit rate stays flat around 53-56% through P ≥ 70% before a small-sample jump at P ≥ 75% (n=87) — doesn't scale with confidence the way the other stats do.

Calibration is monotonic for PTS, REB, and REB_AST — higher confidence thresholds consistently produce higher hit rates. AST is monotonic through P ≥ 65% with a small dip at P ≥ 70% (n=254) before recovering at P ≥ 75%. **PRA is the exception**: it peaks at P ≥ 60% (62.7%) and *declines* at higher thresholds, down to 52.9% at P ≥ 75% (n=17) — a direct consequence of the unified 40% blend weight being well above PRA's own independently-optimal weight (0.15, see `blend_audit.csv`). Daily picks use stat-specific floors: REB/AST ≥ 60%, PRA/REB_AST ≥ 65%, PTS ≥ 70%.

## Dashboard

Interactive Streamlit dashboard for backtests, calibration curves, and player lookups:

```bash
streamlit run dashboard/app.py
```

## Tech Stack

Python 3.12 · scikit-learn · pandas · SQLite · nba_api · scipy · Streamlit

## How to Run

```bash
# 1. Pull data (full backfill, run once — ~2h due to API rate limits)
python scripts/pull_season_data.py --backfill

# Or pull a single season
python scripts/pull_season_data.py --season 2025-26

# Daily update (re-pulls current season to pick up new games)
python scripts/pull_season_data.py --update

# 2. Build features
python -m src.features.pipeline

# 3. Train models
python -m src.models.player_props

# 4. Generate today's picks (auto-fetches schedule and rosters — no args needed)
python scripts/generate_todays_picks.py

# 5. Run backtest report
python -m src.models.edge_finder
```

## Design Notes

- **Ingestion skip logic fix** — the original ingestion pipeline skipped players already logged, making `--update` a no-op for the current season. Fixed by adding a `force_update` flag that bypasses the skip check and relies on SQLite's `ON CONFLICT DO NOTHING` to handle duplicates safely.
- **Forward-looking picks** — the picks generator now auto-fetches today's schedule and current rosters from NBA.com rather than requiring manual player IDs or a pre-existing feature row for today's date.
- **Temporal integrity** — all rolling features use `shift(1)` after groupby to ensure no data leakage: a game's features are computed exclusively from games played before it.

## License

MIT
