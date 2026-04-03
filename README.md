# NBA Edge Model

End-to-end NBA player props prediction system: data ingestion → feature engineering → ML models → probability calibration → daily picks output.

## Methodology

**1. Data pipeline** — pulls 7 seasons of player game logs from the NBA API into SQLite. 129,534 player-game rows across 1,178 players (2019–2026). Incremental daily update mode re-pulls the current season to pick up new games (`--update`).

**2. Feature engineering** — rolling windows (L5/L10/L20), hit-rate frequencies, pace/matchup context, rest and travel flags, opponent defensive ratings. ~125 features per player-game.

**3. Model training** — `HistGradientBoostingRegressor` per stat (PTS, REB, AST, FG3M, PRA, REB_AST). Trained on 2019–2024 (5 seasons, 59,950 rows), validated on 2024-25, tested on 2025-26. Key hyperparameters: `max_depth=3`, `min_samples_leaf=30`, `l2_regularization=5.0`, `max_features=0.6`. Feature selection drops inter-correlated features (r > 0.92) and keeps the top 40 by target correlation.

**4. Probability calibration** — predictions are blended 65% L10-baseline / 35% model, then mapped to P(over line) via a Gaussian CDF using the player's rolling standard deviation. A minimum-minutes filter (min_L10 ≥ 20) removes garbage-time noise before confidence scoring.

**5. Live picks** — auto-fetches today's NBA schedule and team rosters from NBA.com, builds current-form features for each player, and outputs two SAFE Flex cards. No manual player ID input required.

**6. Backtest** — held-out 2024-25 season. Hit rates reported per stat at multiple confidence thresholds. Composite markets (PRA, REB_AST) use summed component model predictions rather than a single composite model.

## Backtest Results — 2025-26 Season (held-out test set)

| Stat    | Threshold | Picks  | Hit Rate |
|---------|-----------|-------:|---------:|
| PTS     | P ≥ 55%   |  4,829 | 54.1%    |
| PTS     | P ≥ 65%   |  1,200 | 60.8%    |
| PTS     | P ≥ 75%   |    193 | 65.3%    |
| REB     | P ≥ 55%   |  4,491 | 55.8%    |
| REB     | P ≥ 65%   |  1,193 | 62.1%    |
| REB     | P ≥ 75%   |    335 | 65.7%    |
| AST     | P ≥ 55%   |  5,322 | 53.4%    |
| AST     | P ≥ 65%   |  1,359 | 60.9%    |
| AST     | P ≥ 75%   |    330 | 63.3%    |
| PRA     | P ≥ 55%   |  4,563 | 55.2%    |
| PRA     | P ≥ 65%   |    932 | 60.6%    |
| REB_AST | P ≥ 65%   |  3,290 | 58.1%    |
| REB_AST | P ≥ 70%   |  2,165 | 61.1%    |

FG3M excluded from daily picks: hit rates don't scale with confidence (≤58.6% even at P ≥ 75%).

Calibration is monotonic across all kept stats — higher confidence thresholds consistently produce higher hit rates. Daily picks use stat-specific floors: REB/AST ≥ 60%, PRA/REB_AST ≥ 65%, PTS ≥ 70%.

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
