##
## NBA Edge Model — daily workflow targets
##
## Usage:
##   make pull                 pull current season data + rebuild features
##   make freshness            DB freshness check only (no writes)
##   make picks                generate today's player-props picks (fails if stale or offseason)
##   make game-picks           generate today's game-props picks (spread/ML/O-U) + snapshot lines
##   make capture-closing-odds snapshot closing lines for CLV tracking (run near tip-off)
##   make backtest              run backtest on current season
##   make train                retrain all per-stat models
##   make doctor                freshness + phase + model staleness — one-screen status
##   make dash                  launch Streamlit dashboard
##

PYTHON := $(shell [ -f "$(CURDIR)/venv/bin/python" ] && echo "$(CURDIR)/venv/bin/python" || echo python3)

.PHONY: pull freshness picks game-picks capture-closing-odds backtest train doctor dash

## ── pull ──────────────────────────────────────────────────────────────────────
pull:
	@echo ""
	@echo "================================================================"
	@echo "  NBA PULL  —  refresh current season + rebuild features"
	@echo "================================================================"
	$(PYTHON) scripts/update_data.py --playoffs
	@echo ""
	$(PYTHON) scripts/_make_helpers.py freshness

## ── freshness ─────────────────────────────────────────────────────────────────
freshness:
	$(PYTHON) scripts/_make_helpers.py freshness

## ── picks ─────────────────────────────────────────────────────────────────────
picks:
	@echo ""
	@echo "================================================================"
	@echo "  PICKS  —  pre-flight check"
	@echo "================================================================"
	$(PYTHON) scripts/_make_helpers.py preflight
	@echo ""
	@echo "================================================================"
	@echo "  PICKS  —  generating"
	@echo "================================================================"
	$(PYTHON) nba.py picks --props-only --save

## ── game-picks ────────────────────────────────────────────────────────────────
game-picks:
	@echo ""
	@echo "================================================================"
	@echo "  GAME PICKS  —  spread / ML / O-U"
	@echo "================================================================"
	$(PYTHON) scripts/generate_todays_team_picks.py --save

## ── capture-closing-odds ──────────────────────────────────────────────────────
capture-closing-odds:
	$(PYTHON) scripts/capture_closing_odds.py

## ── backtest ──────────────────────────────────────────────────────────────────
backtest:
	@echo ""
	@echo "================================================================"
	@echo "  BACKTEST  —  current season playoffs"
	@echo "================================================================"
	$(PYTHON) scripts/run_playoff_backtest.py

## ── train ─────────────────────────────────────────────────────────────────────
train:
	@echo ""
	@echo "================================================================"
	@echo "  TRAIN  —  rebuild feature dataset + retrain all models"
	@echo "================================================================"
	$(PYTHON) -m src.features.pipeline
	$(PYTHON) -m src.models.player_props
	@echo ""
	@echo "  Models saved to data/processed/models/"

## ── doctor ────────────────────────────────────────────────────────────────────
doctor:
	$(PYTHON) scripts/_make_helpers.py doctor

## ── dash ──────────────────────────────────────────────────────────────────────
dash:
	@echo ""
	@echo "================================================================"
	@echo "  DASHBOARD  —  launching Streamlit"
	@echo "================================================================"
	$(PYTHON) -m streamlit run dashboard/app.py
