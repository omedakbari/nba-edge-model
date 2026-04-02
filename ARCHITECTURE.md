# NBA Edge Model — Architecture Blueprint

## What We're Building

A quantitative NBA prediction system that:
- Ingests historical and live player/team data
- Engineers features that capture the real drivers of NBA stat production
- Trains ML models to predict player stat lines and game outcomes
- Compares predictions against sportsbook lines to surface +EV edges
- Presents daily picks via a clean dashboard
- Tracks performance over time with honest backtesting

---

## Tech Stack

| Layer | Tool | Why |
|-------|------|-----|
| Language | Python 3.11+ | Industry standard for DS, huge ecosystem |
| Data Storage | SQLite → PostgreSQL | SQLite for dev speed, Postgres for production |
| Data Source | `nba_api` (free) | Official NBA.com stats, 20+ years of history |
| Odds Data | `the-odds-api` (free tier) | Real sportsbook lines for edge calculation |
| Data Processing | pandas, numpy | Core data manipulation |
| Feature Engineering | pandas, custom modules | Rolling stats, contextual features |
| Modeling | scikit-learn, XGBoost, LightGBM | Proven for tabular prediction tasks |
| Evaluation | Custom backtester | Simulated bankroll, hit rates, calibration |
| Dashboard | Streamlit | Fast to build, interactive, looks professional |
| Version Control | Git + GitHub | Portfolio visibility, proper engineering |
| Environment | venv + pip | Simple, reliable dependency management |

---

## Project Structure

```
nba-edge-model/
├── README.md                    # Portfolio-facing overview
├── ARCHITECTURE.md              # This document
├── requirements.txt             # Python dependencies
├── .gitignore                   # Keep repo clean
├── .env.example                 # Template for API keys
│
├── config/
│   └── settings.py              # Central configuration
│
├── data/
│   ├── raw/                     # Raw API responses (gitignored)
│   ├── processed/               # Cleaned datasets (gitignored)
│   ├── nba_edge.db              # SQLite database (gitignored)
│   └── schema.sql               # Database schema (tracked)
│
├── src/
│   ├── __init__.py
│   ├── ingestion/               # PHASE 1: Data pipelines
│   │   ├── __init__.py
│   │   ├── game_logs.py         # Pull player game logs
│   │   ├── team_stats.py        # Pull team-level stats
│   │   ├── odds.py              # Pull odds/lines data
│   │   └── injuries.py          # Injury report ingestion
│   │
│   ├── features/                # PHASE 2: Feature engineering
│   │   ├── __init__.py
│   │   ├── player_features.py   # Rolling stats, trends, splits
│   │   ├── team_features.py     # Pace, DRTG, matchup context
│   │   ├── context_features.py  # Rest, home/away, B2B, blowout
│   │   └── pipeline.py          # Full feature pipeline
│   │
│   ├── models/                  # PHASE 2-3: ML models
│   │   ├── __init__.py
│   │   ├── player_props.py      # Predict individual stat lines
│   │   ├── game_model.py        # Spread/total predictions
│   │   └── edge_finder.py       # Compare predictions vs lines
│   │
│   ├── evaluation/              # PHASE 4: Backtesting
│   │   ├── __init__.py
│   │   ├── backtester.py        # Historical simulation
│   │   ├── calibration.py       # Are probabilities accurate?
│   │   └── metrics.py           # Hit rate, ROI, Brier score
│   │
│   └── utils/
│       ├── __init__.py
│       ├── db.py                # Database connection helpers
│       └── logging_config.py    # Structured logging
│
├── notebooks/                   # Exploration and analysis
│   ├── 01_data_exploration.ipynb
│   ├── 02_feature_analysis.ipynb
│   ├── 03_model_development.ipynb
│   └── 04_backtest_results.ipynb
│
├── dashboard/                   # PHASE 4: Streamlit app
│   ├── app.py
│   └── components/
│
├── scripts/                     # Runnable entry points
│   ├── pull_season_data.py      # Backfill historical data
│   ├── daily_update.py          # Daily pipeline run
│   └── generate_picks.py        # Today's predictions
│
└── tests/                       # Unit tests (recruiter signal)
    ├── test_features.py
    └── test_models.py
```

---

## Phase Breakdown

### Phase 1 — Data Foundation (Weeks 1-3)

**Goal:** Build a reliable, incremental data pipeline and understand the data deeply.

**Tasks:**
1. Environment setup (Python, VS Code, Git, virtual environment)
2. Database schema design and creation
3. Player game log ingestion (2019-20 through current season)
4. Team stats ingestion (pace, ratings, etc.)
5. Exploratory data analysis notebook
6. Basic data quality checks

**Deliverable:** A SQLite database with 5+ seasons of player game logs, a clean EDA notebook, and a working incremental pipeline.

**Skills you'll learn:** Python basics, pandas, SQL, API consumption, data quality thinking.

---

### Phase 2 — Player Props Model (Weeks 4-8)

**Goal:** Predict individual player stat lines and find edges against prop lines.

**Tasks:**
1. Feature engineering
   - Rolling averages (L5, L10, L20, season)
   - Opponent defensive ratings (vs position, vs stat)
   - Rest days, home/away, back-to-back
   - Minutes context (recent avg, variance, trend)
   - Role indicators (usage rate, shot share changes)
   - Injury-adjusted features (teammate absence impact)
2. Target definition (points, rebounds, assists, 3PM, combos)
3. Model training (XGBoost regression for stat prediction)
4. Probability calibration (predicted stat → P(over line))
5. Edge calculation (model prob vs implied odds prob)
6. Initial backtesting on held-out data

**Deliverable:** A trained model that outputs calibrated probabilities for player props, with initial backtest showing model vs naive baselines.

**Key concept:** You're not predicting "will this go over?" directly. You're predicting the stat value, then comparing to the line. This is more robust and gives you flexibility across any line.

**Skills you'll learn:** Feature engineering, XGBoost/LightGBM, probability calibration, backtesting methodology.

---

### Phase 3 — Game-Level Model (Weeks 9-12)

**Goal:** Predict spreads and totals. Feed game context back into player props.

**Tasks:**
1. Team-level feature engineering
   - Elo ratings (or similar power ratings)
   - Pace and efficiency metrics
   - Home court advantage modeling
   - Rest and travel features
   - Roster strength adjustments
2. Spread prediction model
3. Total prediction model
4. Integration: game model outputs become features for player model
   - Predicted pace → affects stat ceilings
   - Predicted blowout → affects minutes projection
   - Predicted game script → affects role emphasis

**Deliverable:** Game-level predictions that improve the player props model's accuracy.

**Skills you'll learn:** Elo systems, team-level modeling, model stacking.

---

### Phase 4 — Integration, Dashboard & Deployment (Weeks 13-16)

**Goal:** Daily automated pipeline + live dashboard + performance tracking.

**Tasks:**
1. Daily pipeline: pull data → engineer features → generate predictions → surface picks
2. Streamlit dashboard showing:
   - Today's top edges with confidence tiers
   - Model vs actual tracking (running accuracy)
   - Calibration curves
   - Historical performance charts
3. Bankroll simulation and ROI tracking
4. Automated backtest reporting
5. Polish GitHub README with screenshots, methodology, results

**Deliverable:** A deployed, functional dashboard that runs daily predictions with tracked performance.

**Skills you'll learn:** Streamlit, deployment, dashboard design, portfolio presentation.

---

## Key Modeling Decisions

### Why regression, not classification?
Predicting "25.3 points" is more useful than predicting "over 24.5 points."
- Lines change — your prediction doesn't need to retrain.
- You can evaluate against ANY line from ANY book.
- You get a natural uncertainty estimate.

### Why XGBoost/LightGBM?
- Best-in-class for tabular data (which this is).
- Handles missing values natively.
- Feature importance is interpretable.
- Fast to train, fast to iterate.

### Why calibration matters
A model that says "70% chance of hitting" should hit ~70% of the time.
If it doesn't, your edge calculations are wrong and your bankroll bleeds.
We'll use Platt scaling or isotonic regression to calibrate.

### Why backtesting is non-negotiable
- Train on seasons 2019-2023
- Validate on 2023-24
- Test on 2024-25
- Never let future data leak into past predictions
- Track: hit rate, ROI, Brier score, calibration curve

---

## What Makes This Recruiter-Impressive

1. **End-to-end pipeline** — data engineering → ML → deployment
2. **Proper methodology** — train/test splits, calibration, backtesting
3. **Clean code** — modular, documented, tested
4. **Domain expertise** — you understand NBA context, not just math
5. **Honest evaluation** — showing calibration curves and real ROI, not cherry-picked wins
6. **Live product** — a working dashboard, not just notebooks
7. **GitHub presence** — clean README, commit history showing iteration

---

## Data Sources Reference

| Source | What It Gives You | Cost |
|--------|-------------------|------|
| `nba_api` | Player game logs, box scores, advanced stats, team stats | Free |
| Basketball Reference | Historical reference, advanced metrics | Free (scraping) |
| `the-odds-api` | Live odds, historical lines (limited on free tier) | Free tier: 500 requests/month |
| NBA injury reports | Official injury designations | Free (NBA.com) |

---

## North Star Metrics

Track these to know if the model is actually good:

- **Calibration:** Do 70% predictions hit at ~70%?
- **Brier Score:** Overall probability accuracy (lower is better)
- **Hit Rate at SAFE threshold:** % of picks above your confidence floor that hit
- **ROI:** Simulated return on $1 flat bets on all picks above threshold
- **Edge Stability:** Does the model find consistent edges or is it noisy?
