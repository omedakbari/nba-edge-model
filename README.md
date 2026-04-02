# NBA Edge Model

End-to-end NBA player props prediction system: data ingestion → feature engineering → ML models → probability calibration → daily picks output.

## Methodology

**1. Data pipeline** — pulls 5+ seasons of player game logs from the NBA API into SQLite. ~5,000 player-game rows across 2019-2025.

**2. Feature engineering** — rolling windows (L5/L10/L20), hit-rate frequencies, pace/matchup context, rest and travel flags, opponent defensive ratings. ~125 features per player-game.

**3. Model training** — `HistGradientBoostingRegressor` per stat (PTS, REB, AST, FG3M, PRA, REB_AST). Trained on 2019-23, validated on 2023-24, tested on 2024-25. Key hyperparameters: `max_depth=3`, `min_samples_leaf=30`, `l2_regularization=5.0`, `max_features=0.6`. Feature selection drops inter-correlated features (r > 0.92) and keeps the top 40 by target correlation.

**4. Probability calibration** — predictions are blended 65% L10-baseline / 35% model, then mapped to P(over line) via a Gaussian CDF using the player's rolling standard deviation. A minimum-minutes filter (min_L10 ≥ 20) removes garbage-time noise before confidence scoring.

**5. Backtest** — held-out 2024-25 season. Hit rates at P ≥ 70% are reported per stat. Composite markets (PRA, REB_AST) use summed component model predictions rather than a single composite model.

## Backtest Results — 2024-25 Season (P ≥ 70% picks)

| Threshold | Stat | Picks | Hit Rate |
|-----------|------|------:|---------:|
| P ≥ 70%   | PTS  |    14 | 71.4% (n=14) |
| P ≥ 70%   | REB  |    37 | 67.6% |
| P ≥ 70%   | AST  |    27 | 66.7% |
| P ≥ 65%   | REB  |    60 | 60.0% |
| P ≥ 65%   | AST  |    44 | 63.6% |

FG3M excluded: confidence scores don't correlate with outcomes (42.9% hit rate at P ≥ 70% after minutes filter).

## Tech Stack

Python 3.11 · scikit-learn · pandas · SQLite · nba_api · scipy

## How to Run

```bash
# 1. Pull and build features (run once; full backfill takes ~2h due to API rate limits)
python scripts/pull_season_data.py --backfill
python -m src.features.pipeline

# 2. Train models
python -m src.models.player_props

# 3. Generate today's picks (pass player IDs for tonight's slate)
python scripts/generate_todays_picks.py --date 2025-03-15 --players 2544 1629029 203999
```

## License

MIT
