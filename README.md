# NBA Edge Model

End-to-end NBA player props prediction system: data ingestion → feature engineering → ML models → probability calibration → daily picks output.

## Methodology

**1. Data pipeline** — pulls 6 seasons of player game logs from the NBA API into SQLite. 104,760 player-game rows across 1,079 players (2019–2025). Incremental daily update mode re-pulls the current season to pick up new games (`--update`).

**2. Feature engineering** — rolling windows (L5/L10/L20), hit-rate frequencies, pace/matchup context, rest and travel flags, opponent defensive ratings. ~125 features per player-game.

**3. Model training** — `HistGradientBoostingRegressor` per stat (PTS, REB, AST, FG3M, PRA, REB_AST). Trained on 2019–2024, validated on 2024-25, tested on 2025-26. Key hyperparameters: `max_depth=3`, `min_samples_leaf=30`, `l2_regularization=5.0`, `max_features=0.6`. Feature selection drops inter-correlated features (r > 0.92) and keeps the top 40 by target correlation.

**4. Probability calibration** — predictions are blended 65% L10-baseline / 35% model, then mapped to P(over line) via a Gaussian CDF using the player's rolling standard deviation. A minimum-minutes filter (min_L10 ≥ 20) removes garbage-time noise before confidence scoring.

**5. Live picks** — auto-fetches today's NBA schedule and team rosters from NBA.com, builds current-form features for each player, and outputs two SAFE Flex cards. No manual player ID input required.

**6. Backtest** — held-out 2024-25 season. Hit rates reported per stat at multiple confidence thresholds. Composite markets (PRA, REB_AST) use summed component model predictions rather than a single composite model.

## Backtest Results — 2024-25 Season

| Stat | Threshold | Picks | Hit Rate |
|------|-----------|------:|---------:|
| PTS  | P ≥ 55%   | 1,345 | 57.4% |
| PTS  | P ≥ 65%   |   324 | 60.2% |
| PTS  | P ≥ 70%   |   162 | 67.3% |
| PTS  | P ≥ 75%   |    75 | 72.0% |
| REB  | P ≥ 55%   | 3,454 | 55.5% |
| REB  | P ≥ 65%   | 1,226 | 58.5% |
| REB  | P ≥ 75%   |   389 | 62.5% |
| AST  | P ≥ 55%   | 3,448 | 55.4% |
| AST  | P ≥ 65%   | 1,173 | 58.3% |
| PRA  | P ≥ 55%   | 1,085 | 57.8% |
| PRA  | P ≥ 65%   |   227 | 60.4% |
| PRA  | P ≥ 70%   |    93 | 63.4% |

FG3M excluded: calibrated confidence scores don't correlate with outcomes at any threshold (≤56.7% hit rate).

Daily picks use stat-specific floors: REB/AST ≥ 60%, PRA/REB_AST ≥ 65%, PTS ≥ 70%.

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
