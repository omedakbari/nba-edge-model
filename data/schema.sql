-- NBA Edge Model — Database Schema
-- Run with: sqlite3 data/nba_edge.db < data/schema.sql

-- Players table: canonical player info
CREATE TABLE IF NOT EXISTS players (
    player_id   INTEGER PRIMARY KEY,
    full_name   TEXT NOT NULL,
    first_name  TEXT,
    last_name   TEXT,
    is_active   INTEGER DEFAULT 1,  -- 1 = active, 0 = retired/inactive
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- Player game logs: one row per player per game
-- This is the core table — everything else builds from this
CREATE TABLE IF NOT EXISTS game_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER NOT NULL,
    game_id         TEXT NOT NULL,        -- NBA game ID (e.g., '0022400123')
    game_date       TEXT NOT NULL,        -- 'YYYY-MM-DD'
    season          TEXT NOT NULL,        -- '2024-25' format
    season_type     TEXT NOT NULL,        -- 'Regular Season' or 'Playoffs'
    matchup         TEXT NOT NULL,        -- 'LAL vs. DEN' or 'LAL @ DEN'
    is_home         INTEGER NOT NULL,     -- 1 = home, 0 = away
    team_abbr       TEXT NOT NULL,        -- Player's team
    opp_abbr        TEXT NOT NULL,        -- Opponent team
    wl              TEXT,                 -- 'W' or 'L'
    min             REAL,                 -- Minutes played (decimal)
    pts             INTEGER DEFAULT 0,
    reb             INTEGER DEFAULT 0,
    ast             INTEGER DEFAULT 0,
    stl             INTEGER DEFAULT 0,
    blk             INTEGER DEFAULT 0,
    tov             INTEGER DEFAULT 0,
    pf              INTEGER DEFAULT 0,    -- Personal fouls
    fgm             INTEGER DEFAULT 0,    -- Field goals made
    fga             INTEGER DEFAULT 0,    -- Field goals attempted
    fg3m            INTEGER DEFAULT 0,    -- 3-pointers made
    fg3a            INTEGER DEFAULT 0,    -- 3-pointers attempted
    ftm             INTEGER DEFAULT 0,    -- Free throws made
    fta             INTEGER DEFAULT 0,    -- Free throws attempted
    oreb            INTEGER DEFAULT 0,    -- Offensive rebounds
    dreb            INTEGER DEFAULT 0,    -- Defensive rebounds
    plus_minus      REAL DEFAULT 0,
    -- Combo stats (precomputed for convenience)
    pts_reb         INTEGER GENERATED ALWAYS AS (pts + reb) STORED,
    pts_ast         INTEGER GENERATED ALWAYS AS (pts + ast) STORED,
    reb_ast         INTEGER GENERATED ALWAYS AS (reb + ast) STORED,
    pra             INTEGER GENERATED ALWAYS AS (pts + reb + ast) STORED,
    inserted_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(player_id, game_id),
    FOREIGN KEY (player_id) REFERENCES players(player_id)
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_game_logs_player_date
    ON game_logs(player_id, game_date DESC);

CREATE INDEX IF NOT EXISTS idx_game_logs_date
    ON game_logs(game_date);

CREATE INDEX IF NOT EXISTS idx_game_logs_season
    ON game_logs(season);

CREATE INDEX IF NOT EXISTS idx_game_logs_opp
    ON game_logs(player_id, opp_abbr);

CREATE INDEX IF NOT EXISTS idx_game_logs_game_id
    ON game_logs(game_id);

-- Team stats: one row per team per season (updated periodically)
-- Used for pace, defensive rating, etc.
CREATE TABLE IF NOT EXISTS team_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    team_abbr       TEXT NOT NULL,
    season          TEXT NOT NULL,
    pace            REAL,            -- Possessions per 48 min
    off_rtg         REAL,            -- Offensive rating
    def_rtg         REAL,            -- Defensive rating
    net_rtg         REAL,            -- Net rating
    reb_rate        REAL,            -- Team rebound rate
    ast_rate        REAL,            -- Team assist rate
    updated_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(team_abbr, season)
);

-- Ingestion log: track what we've already pulled (for incremental updates)
CREATE TABLE IF NOT EXISTS ingestion_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id       INTEGER NOT NULL,
    season          TEXT NOT NULL,
    season_type     TEXT NOT NULL,
    games_pulled    INTEGER DEFAULT 0,
    last_game_date  TEXT,
    pulled_at       TEXT DEFAULT (datetime('now')),
    UNIQUE(player_id, season, season_type)
);

-- Pipeline runs: track each time we run the pipeline
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type        TEXT NOT NULL,     -- 'full_backfill', 'daily_update', 'team_stats'
    season          TEXT,
    started_at      TEXT DEFAULT (datetime('now')),
    finished_at     TEXT,
    rows_inserted   INTEGER DEFAULT 0,
    rows_skipped    INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running'  -- 'running', 'completed', 'failed'
);
