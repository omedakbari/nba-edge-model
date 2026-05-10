# NBA Edge Model — Full Diagnostic Audit
**Date:** 2026-05-09  
**DB max game date:** 2026-05-08  
**Status:** 2025-26 playoffs in progress

---

## a) Script Inventory & Dependency Graph

```
config/settings.py                      # paths, CURRENT_SEASON, model config
    ↑
src/utils/db.py                         # get_connection(), init_db(), table_row_count()
    ↑
src/ingestion/game_logs.py              # fetch_player_game_log(), ingest_season()
src/ingestion/team_stats.py             # team NET_RTG/pace/ORTG/DRTG ingestion
    ↑
scripts/pull_season_data.py             # manual CLI: --backfill / --season / --update / --playoffs / --play-in
scripts/update_data.py                  # daily refresh: game_logs + team_stats + features pipeline (--playoffs flag gates features)
    ↑
src/features/pipeline.py               # build_feature_dataset() → features.parquet
  ├── src/features/player_features.py  # rolling L5/L10/L20, hit rates, trends
  ├── src/features/context_features.py # rest, B2B, home/away, month
  ├── src/features/team_features.py    # opponent DRTG, pace
  └── src/features/playoff_features.py # is_playoff, series_game_num
    ↑
data/processed/features.parquet        # training dataset + live feature source
    ↑
src/models/player_props.py             # HistGradientBoostingRegressor per stat — run once to train
    ↓
data/processed/models/*.pkl            # pts, reb, ast, fg3m, pra, reb_ast models (last trained 2026-04-16)
    ↑ (both)
scripts/generate_todays_picks.py       # live picks: loads features.parquet + models, fetches Sleeper/PP lines
    ↓
data/picks/YYYY-MM-DD.json             # saved picks
    ↑
scripts/log_pick_results.py            # grading
scripts/log_team_results.py

nba.py                                  # CLI wrapper: nba picks / nba grade / nba daily / nba refresh / nba status / nba ratings
  ├── detect_phase()                    # DB-based phase detection (used by nba CLI only)
  └── refreshed_today()

Backtest:
  src/backtest/playoff_backtest.py      # chronological replay harness
  scripts/run_playoff_backtest.py       # report generator
  scripts/backtest_playoffs.py          # lightweight playoff backtest
```

---

## b) Data Freshness

| Metric | Value |
|---|---|
| DB max game_date | 2026-05-08 |
| Today | 2026-05-09 |
| Staleness | 1 day (yesterday's games — normal) |
| features.parquet max date | 2026-05-08 |
| Model file mtimes | 2026-04-16 (23 days ago) |
| 2025-26 Regular Season rows | 26,651 |
| 2025-26 Playoffs rows | 1,296 |
| 2025-26 PlayIn rows | 126 |

**DB is current.** Daily update pipeline is running correctly. Models are 23 days old but this is expected — models are retrained periodically, not daily.

---

## c) Season Detection Audit

### All phase-decision sites

| Location | Logic | Issue |
|---|---|---|
| `nba.py:44-66` | `detect_phase()` — queries recent game_logs by season_type | ✅ Correct. DB-based, queries last 14 days. Used by `nba` CLI only. |
| `nba.py:95-96` | `_flags(phase)` translates phase → `['--playoffs']` etc. | ✅ Correct. |
| `scripts/generate_todays_picks.py:1747-1768` | Auto-corrects play-in vs playoffs *only when user already passed `--playoffs` or `--play-in`* | ⚠️ **Gap**: if user forgets both flags, no detection at all. Regular-season thresholds apply. |
| `scripts/generate_todays_picks.py:1797` | `sleeper_season_type = 'post' if (is_playoffs or is_play_in) else 'regular'` | Downstream of the same gap. |
| `scripts/update_data.py:60-61` | `if args.playoffs:` gates playoff game-log pull | ✅ Correct when used via `nba refresh` which passes `--playoffs` based on `detect_phase()`. |
| `src/playoffs/adjustments.py:258` | `elev_seasons = ['2022-23', '2023-24', '2024-25']` — hardcoded | ⚠️ **Won't include 2025-26 data after playoffs end.** |
| `src/backtest/playoff_backtest.py:459` | `seasons = ['2024-25']` default | ⚠️ **Won't include current 2025-26 playoffs in backtest.** |

**Summary:** `nba picks` / `nba daily` auto-detect phase correctly. Direct invocation of `scripts/generate_todays_picks.py` without `--playoffs` produces regular-season output during playoffs. There is no single `utils/season.py` source of truth — `detect_phase()` lives only in `nba.py`.

---

## d) Ingestion Path Trace (`pull_season_data.py --season 2025-26`)

### Full trace

1. **Argument parse** (`pull_season_data.py:177-179`): `--season` branch calls `ingest_season(args.season)` — **`force_update` defaults to `False`**.
2. **ingest_season** (`game_logs.py:219-234`): checks `ingestion_log` table. For any `player_id` already in `ingestion_log` for this season+season_type, sets `summary["skipped"] += 1` and calls `continue` — no API call, no DB write.
3. **Silent skip condition**: A player logged with `games_pulled=0` (checked but no games found on first run) is treated identically to a player logged with `games_pulled=50`. Both are skipped on subsequent `force_update=False` runs.
4. **Effect**: After the first ingest of 2025-26 Regular Season (2026-04-02 per pipeline_runs), any subsequent `pull_season_data.py --season 2025-26` inserts 0 rows. New games played since that date are never fetched.

### Mitigation in daily workflow
`update_data.py --update` → `game_logs.py --update` → `update_current_season()` → `ingest_season(CURRENT_SEASON, force_update=True)`. This path correctly re-fetches all players. The `nba refresh` and `nba picks` commands use this path.

### Verdict
**HIGH** (not BLOCKER since daily workflow is unaffected). The `--season` flag in `pull_season_data.py` is a confusing false friend: it looks like "fetch data for this season" but behaves as "resume an incomplete initial ingest." Running it during the active season produces 0 rows for all previously-ingested players with no error.

---

## e) Picks Generator Trace

1. **Season/mode input** (`main():1693-1748`): date/season from args. Phase from `--playoffs`/`--play-in` flags.
2. **Auto-correct** (`1750-1767`): Only triggers if user passed one of the playoff flags. Detects play-in vs playoffs mismatch from NBA API. **Does not auto-enable playoff mode from nothing.**
3. **Sleeper lines** (`1797-1812`): `fetch_sleeper_lines(season_type=...)` → `_fetch_sleeper_proj_one(pid)` → hardcoded `season=2025` in URL.
4. **Feature loading** (`1854-1863`): reads `features.parquet`, checks staleness vs target date (warns if >5 days stale, exits on sys.exit(0) if no players found — but does not exit on stale data).
5. **Schedule** (`1865-1870`): `get_todays_player_contexts()` — three fallbacks (NBA.com → ESPN → Sleeper). All failures swallowed with empty list return + `sys.exit(0)`.
6. **Feature build** (`1872-1878`): `build_todays_features()` → uses most recent pre-date row per player from features.parquet.
7. **Candidate scoring** (`1892-1897`): `build_candidates()` — loads all models, scores each (player, stat) pair.
8. **Card building** (`1961-1983`): strict Sleeper mode, DTD handling.
9. **Output** + optional `--save`.

### Season phase issue in direct invocation
If user runs `python scripts/generate_todays_picks.py` without `--playoffs` on a playoff game night:
- `is_playoffs = False`
- Regular-season thresholds apply (57% floor vs 60-62% playoffs floors)
- No tier-based adjustments (STAR/CORE/ROTATION multipliers skip)
- No playoff elevation ratios loaded
- Output labeled "Regular Season"

This produces picks miscalibrated for the actual game type. No warning is shown unless the user notices the mode label.

---

## f) Backtest Sanity

**Line construction**: `get_player_reg_season_context()` uses `game_date < ?` (strict less-than) — no leakage. L10 is computed from the 10 most recent regular-season games before the game date being evaluated. ✅

**Feature pipeline leakage check**: `player_features.py` uses `shift(1)` before rolling windows. Feature for game N uses games N-1 through N-10. ✅

**`build_todays_features`**: takes `features_df[game_date < target_dt].groupby('player_id').last()` — uses most recent pre-game row. ✅

**Verdict**: Backtest is clean. No data leakage found.

---

## g) Model Staleness

| Model | Last trained | Note |
|---|---|---|
| pts_model.pkl | 2026-04-16 | 23 days old |
| reb_model.pkl | 2026-04-16 | 23 days old |
| ast_model.pkl | 2026-04-16 | 23 days old |
| fg3m_model.pkl | 2026-04-16 | 23 days old |
| pra_model.pkl | 2026-04-16 | 23 days old |
| reb_ast_model.pkl | 2026-04-16 | 23 days old |

Models were trained with `TRAIN_SEASONS = ['2019-20', ..., '2023-24']`, `VAL_SEASON = '2024-25'`, `TEST_SEASON = '2025-26'`. Feature definitions in `player_props.py` match `player_features.py` — no drift detected. `CONTEXT_FEATURES` are force-included in feature selection.

Models are 23 days old during playoffs. This is expected — playoff games are being used as live test data, not for retraining. **No staleness issue.**

---

## h) Lint Pass

### Bare `except: pass` / swallowed exceptions

| File | Line | Block | What's swallowed | Severity |
|---|---|---|---|---|
| `scripts/generate_todays_picks.py` | 332 | `except Exception: pass` | Sleeper player cache JSON parse fail (falls through to re-fetch) | LOW |
| `scripts/generate_todays_picks.py` | 362-363 | `except Exception: pass` | Cache write to disk fails silently | LOW |
| `scripts/generate_todays_picks.py` | 423-424 | `except Exception: pass` | Single Sleeper projection fetch — error dropped, player skipped | MEDIUM |
| `scripts/generate_todays_picks.py` | 473-474 | `except Exception: pass` | ThreadPool future result — error dropped, player's lines silently lost | MEDIUM |
| `scripts/generate_todays_picks.py` | 1619 | `except Exception: return {}` | `load_saved_card_lines` — JSON parse fails silently | LOW |
| `scripts/generate_todays_picks.py` | 1824 | `except Exception as e` | Lines-file read error logged, then re-raises via sys.exit(1) | ✅ OK |
| `nba.py` | 59-60 | `except Exception: return 'regular'` | DB phase detection fails, returns default | LOW |
| `src/ingestion/team_stats.py` | 123 | `except Exception as e` | Team stats fetch error — printed and continues | ✅ OK (logs) |

### Hardcoded values

| File | Line | Value | Issue |
|---|---|---|---|
| `scripts/generate_todays_picks.py` | 413 | `season=2025` | Sleeper API URL — hardcoded start year, breaks next season |
| `scripts/generate_todays_picks.py` | 523 | `season=2025` | Same, in debug path |
| `src/playoffs/adjustments.py` | 258 | `elev_seasons = ['2022-23', '2023-24', '2024-25']` | Won't include 2025-26 after current playoffs end |
| `src/backtest/playoff_backtest.py` | 459 | `seasons = ['2024-25']` | Default backtest won't include 2025-26 |
| `src/models/team_ratings.py` | 25 | `season='2024-25'` | Example only in module-level test code (dead path) |

### Dead code

- `dashboard/app_before_claude_fix.py`, `dashboard/app.before_*.py` — 7 backup files
- `src/models/player_props_v1_backup.py`
- `scripts/pull_season_data.py.bak`

### Tests

14 passed, 2 skipped. All pass. No test covers the ingestion `force_update` bug or season phase detection.

---

## Finding Summary

### BLOCKER
*(none — daily workflow is functional)*

### HIGH

**H1 — `pull_season_data.py --season` silently skips all previously-ingested players**  
- **File:** `scripts/pull_season_data.py:179`, `src/ingestion/game_logs.py:233`  
- **Current:** `ingest_season(args.season)` uses `force_update=False`. Players in `ingestion_log` are permanently skipped, regardless of new games.  
- **Expected:** `--season CURRENT_SEASON` should behave identically to `--update` (force_update=True). Historical seasons should remain resumable (force_update=False).  
- **Proposed fix:** `force_update = (args.season == CURRENT_SEASON)` in `pull_season_data.py:179`.

**H2 — No auto season-phase detection in direct script invocation**  
- **File:** `scripts/generate_todays_picks.py:1747`, `nba.py:44` (detect_phase lives here only)  
- **Current:** `detect_phase()` exists in `nba.py` and is used by `nba picks`/`nba daily`. Direct invocation of `generate_todays_picks.py` without `--playoffs` produces regular-season output during playoffs.  
- **Expected:** Single `utils/season.py` source of truth. `generate_todays_picks.py` auto-detects phase when flag not explicitly given.  
- **Proposed fix:** Extract `detect_phase()` to `utils/season.py`. Import and call in `generate_todays_picks.py` main() when no playoff flag is passed.

**H3 — Hardcoded `season=2025` in Sleeper API URL**  
- **File:** `scripts/generate_todays_picks.py:413, 523`  
- **Current:** `_fetch_sleeper_proj_one` builds URL with literal `season=2025`. Will silently return wrong/empty projections when `CURRENT_SEASON` advances.  
- **Expected:** Derive from `CURRENT_SEASON.split('-')[0]`.  
- **Proposed fix:** Module-level `_SLEEPER_API_YEAR = CURRENT_SEASON.split('-')[0]`, used in both URL formats.

**H4 — `update_data.py` without `--playoffs` never rebuilds `features.parquet`**  
- **File:** `scripts/update_data.py:88-91`  
- **Current:** Feature pipeline rebuild is gated on `if args.playoffs:`. Regular-season daily updates leave `features.parquet` stale after each game-log update.  
- **Expected:** Features always rebuilt after any game-log update. (The picks generator uses features.parquet for all rolling windows — a 1-day-stale parquet means yesterday's games aren't in L10 features.)  
- **Proposed fix:** Remove the `if args.playoffs:` gate — always rebuild features at end of `update_data.py`.

### MEDIUM

**M1 — Multiple `except Exception: pass` silently drop data in hot paths**  
- **File:** `scripts/generate_todays_picks.py:423, 473`  
- **Current:** Sleeper per-player fetch errors (line 423) and ThreadPool future failures (line 473) are silently discarded. A Sleeper API outage or rate-limit response drops entire players without any log output.  
- **Proposed fix:** `print(f"  [Sleeper] fetch error for {norm_name}: {e}", file=sys.stderr)` before continuing.

**M2 — Playoff elevation seasons hardcoded, won't auto-include 2025-26**  
- **File:** `src/playoffs/adjustments.py:258`  
- **Current:** `elev_seasons = ['2022-23', '2023-24', '2024-25']`  
- **Proposed fix:** Derive dynamically from `CURRENT_SEASON` as `[s for s in BACKFILL_SEASONS if s <= CURRENT_SEASON][-3:]` or equivalent.

**M3 — Backtest default season hardcoded**  
- **File:** `src/backtest/playoff_backtest.py:459`  
- **Current:** `if seasons is None: seasons = ['2024-25']`  
- **Proposed fix:** `if seasons is None: seasons = [CURRENT_SEASON]` or derive from available data.

### LOW

**L1 — Dead backup files** (`dashboard/app.before_*.py`, `src/models/player_props_v1_backup.py`, `scripts/pull_season_data.py.bak`). Not a functional issue but clutters the repo.

**L2 — `team_ratings.py:25`** has dead `season='2024-25'` in module-level test block.

---
