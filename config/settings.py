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

# === Team Abbreviation Mapping ===
# nba_api sometimes uses different abbreviations than what we want.
# This maps to our canonical set.
TEAM_ABBR_MAP = {
    # These are the ones that sometimes differ
    "PHX": "PHX",   # Phoenix Suns
    "BKN": "BKN",   # Brooklyn Nets
    "CHA": "CHA",   # Charlotte Hornets
    "NOP": "NOP",   # New Orleans Pelicans (sometimes 'NO')
    "NYK": "NYK",   # New York Knicks
    "SAS": "SAS",   # San Antonio Spurs
    "GSW": "GSW",   # Golden State Warriors
    "OKC": "OKC",   # Oklahoma City Thunder
    "UTA": "UTA",   # Utah Jazz (sometimes 'UTH')
}

# === Model Settings (Phase 2 — placeholder) ===
SAFE_FLOOR_LOW_VAR = 0.58
SAFE_FLOOR_MED_VAR = 0.60
SAFE_FLOOR_HIGH_VAR = 0.62

# Rolling window sizes for feature engineering
ROLLING_WINDOWS = [5, 10, 20]
