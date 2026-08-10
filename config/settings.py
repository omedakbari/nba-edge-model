"""
Central configuration for the NBA Edge Model.
All magic numbers and paths live here — never hardcode them in other files.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# === Paths ===
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
DB_PATH = DATA_DIR / "nba_edge.db"
SCHEMA_PATH = DATA_DIR / "schema.sql"

# Ensure directories exist
RAW_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# === API Keys ===
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

# === NBA API Settings ===
# NBA.com rate limits aggressively. 0.6s between calls is safe.
NBA_API_DELAY = 0.6

# Seasons to backfill (most recent first)
BACKFILL_SEASONS = [
    "2025-26",
    "2024-25",
    "2023-24",
    "2022-23",
    "2021-22",
    "2020-21",
    "2019-20",
]

# Current season (update this at season start)
CURRENT_SEASON = "2025-26"

# === Model Settings (Phase 2 — placeholder) ===
SAFE_FLOOR_LOW_VAR = 0.58
SAFE_FLOOR_MED_VAR = 0.60
SAFE_FLOOR_HIGH_VAR = 0.62

# === Prop prediction blend weight ===
# Weight given to the player's L10 rolling average when blending with the raw
# model prediction: pred = (1 - BLEND_L10_WEIGHT) * model + BLEND_L10_WEIGHT * L10.
#
# Derived 2026-08-10 by pooling all 112,422 picks across all 6 stats from
# scripts/phase1_calibration_audit.py --season 2025-26 (data/diagnostics/
# per_pick_log.csv) and grid-searching L10 weight in [0.0, 1.0] step 0.05 for
# the value minimizing pooled Brier score. Result: 0.40 (Brier 0.24568),
# beating every ratio previously hardcoded ad hoc across the codebase:
#   0%  (unblended, previously used for live regular-season picks): 0.24709
#   50% (previously used for live playoff picks):                   0.24581
#   65% (previously used by backtest/dashboard scripts):            0.24643
#
# Per-stat optimal weights vary meaningfully (PRA 0.15 - FG3M 0.65 — see
# data/diagnostics/blend_audit.csv) — this single value is a pooled compromise,
# not independently optimal for every stat. Re-derive by re-running
# phase1_calibration_audit.py whenever the season/models change meaningfully.
BLEND_L10_WEIGHT = 0.40
