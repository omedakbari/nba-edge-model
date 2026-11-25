# Fix report: 2026-11-24 audit (Phases 0–4)

Branch `fix/integrity-calibration`. Audit: `AUDIT_2026-11-24.md`. Every design choice: `DECISIONS.md` (D1–D19).
Supabase stayed read-only throughout. The only schema change (migration 011) was applied by the user.
Final suites: root **725 passed, 2 skipped**; worker **444 passed** (baseline before the work: 586 / 380).

## The 10 issues

| # | Issue | Status | Where (file:line) | Tests |
|---|---|---|---|---|
| 1 | Sportsbook feed silently dead | **Fixed** (root cause: stale DK data, not parsing; see D1) | `src/betting/quote_quality.py:37` `rejection_detail`; `config/settings.py:89` window and skew config; `src/data/espn_props.py:280` summary and WARN | `tests/test_quote_freshness.py` (8) |
| 2 | Injury filtering incomplete / invisible | **Fixed** | `worker/prop_worker/injury_status.py:53` stricter-of-two; `slate.py:152` `injury_status`; `slate.py:173` unmatched names; `card.py:219` named exclusions; `dashboard/today.py:759` Excluded expander | `worker/tests/test_injury_status.py`, `test_card_2026_11_24.py`, `test_slate.py::test_stricter_status_wins_across_official_and_espn`, `::test_unknown_status_fails_closed` |
| 3 | Roster / game mapping invisible | **Fixed** (guard; today's mapping was correct) | `worker/prop_worker/slate.py:193` `resolve_team` | `test_card_2026_11_24.py::test_build_flags_each_issue` (wrong-game player) |
| 4 | Overconfident p | **Fixed for the card** (calibrated; see D10) | `src/models/calibration.py:121` `fit_kalshi`; `worker/prop_worker/card.py:114` `kalshi_pick_calibrated`; `worker/prop_worker/calibration.py:64` fail-closed | `tests/test_card_calibration.py`, `worker/tests/test_calibration_apply.py`, `worker/tests/test_replay.py` |
| 5 | Blend ratios conflict | **Fixed** | `config/model.yaml:15`, `config/model_config.py:21` | `tests/test_model_config.py::test_no_hard_coded_blend_ratios_outside_config` |
| 6 | Grading stalled | **Fixed** (root cause: yesterday-only grader) | `src/utils/grading.py:61` `to_grade`; `--catch-up` in both graders; `nba.py` daily | `tests/test_grading_catchup.py` (8) |
| 7 | Player Lookup = 2024-25 test set | **Fixed** (old view behind a toggle) | `dashboard/player_view.py:111` | `tests/test_player_view.py` |
| 8 | Backtest / Calibration only PTS/REB/AST | **Fixed**; also found those pages were **in-sample** | `src/backtest/model_backtest.py:105` (6 stats, N+ thresholds, held-out 2026-27) | `tests/test_model_backtest.py::test_rows_in_equals_rows_out_plus_counted_drops_with_null_features` |
| 9 | Props board exposes almost nothing | **Fixed**; Projection is partial (D20 below) | `dashboard/board_view.py:56` `board_frame` (all 22 columns, CSV); `:79` `ladder_frame` (every Kalshi strike); `worker/prop_worker/card.py` `leg_stats`, `ladder` | `tests/test_board_view.py`, `worker/tests/test_board_fields.py` |
| 10 | No health indicator | **Fixed** | `src/utils/step_status.py:26` WARN protocol; `nba.py:95`; `scripts/run_daily.py:77` exit 3; `src/data/worker_health.py:62`; `dashboard/health_view.py:39` 9-tile panel; `dashboard/today.py:495` | `tests/test_step_status.py`, `tests/test_health_view.py` |

Extra fixes found on the way: validate DNP match (`worker/prop_worker/jobs/validate.py:117`), two-way
injury rows (D16), the replay's pick-time quote query (`worker/prop_worker/replay.py:66`), the pick-time
snapshot archive (`worker/prop_worker/jobs/pick_snapshots.py:36`), no false overnight staleness (D19), and
`run_daily.py --dry-run` (D8b).

**D20 (Projection column, partial).** The board's Projection is the ML model's projection from the fair board,
which only covers props with a fresh DK-via-ESPN line. While that feed is dead (most days), the column is
blank and the page says why. Filling it for every card leg means running the six models for every player on
the slate, independent of sportsbook lines. That's a follow-up; I didn't invent a number.

## Before / after on real data

| | Before | After |
|---|---|---|
| DK via ESPN, 11-24 2:40 PM | "OK (exit 0)", 0 lines, nothing said | **WARN**: "DK via ESPN: 0/506 passed (406 stale, 100 future-skew)" in the run log, a yellow chip and a health tile |
| Card exclusions, 11-24 | "Hidden: 5 injured", no names | Named with source and time, e.g. Cade Cunningham OUT (official 7:15 PM ET), P.J. Washington Q (official) / Day-To-Day (ESPN). The stricter-of-two rule would have excluded Cade at 5:30 PM (official Probable, ESPN Day-To-Day) |
| Kalshi legs, held-out 11-18 → 11-24 | raw rules: 161 legs, p .758, hit .696, −1.1¢/contract after fee | calibrated rules: **0 legs** for every stat |
| Calibration (raw-selected legs, held-out) | Brier .2180, p .758 | Brier .2170, p .646 (market mid alone .2144) |
| Live Kalshi ledger (124 graded) | p .771, hit .702 | No stored mid on old rows, so calibration is reported raw only (D13) |
| Grading | last W/L 11-16; 11-20 ungraded; 11-21..23 no picks files | 11-20 and 11-24 graded (BOS −10.5 won, DAL@DEN U won); `--catch-up` self-heals |
| Backtest | "held-out 2024-25", actually in-sample | held-out 2026-27, all six stats 2–3 pts overconfident at N+ (pts .62 → .59) |
| Validate | red on AJ Green (None vs 0.15 min) | DNP match (takes effect from main) |

## Things that need you

1. **Merge to main.** Scheduled ingest and nightly runs check out the default branch (no `ref:` in either
   workflow), so the validate, two-way and pick-snapshot changes start only after the merge.
2. **Commit the pre-merge step.** At this report it was still uncommitted (migration 011 copy, flag ON,
   pick_snapshots message and test, `sql/011` header, D15 update). It's included in the commands below.
3. **Refit weekly:** `./venv/bin/python -m src.models.calibration --refit`. The fit covers 14 slates; the
   health panel stays yellow until 20.
4. **Optional:** `sql/proposed_2026-11-24_bets_p_raw.sql` (real `p_raw` and `market_mid` columns on `bets`);
   `sql/012_model_artifacts.sql` (only if the cloud ever builds the card).
5. **Workflow cosmetics (not edited):** the nightly dispatch description doesn't list `pick_snapshots` or `bets`.
6. **Leftover file:** `data/team_picks/2026-11-24.revision-20261125T004059291838Z.json` from the first dry run
   (gitignored; identical picks). Delete it if you like; I don't delete files.
7. No new env vars, API keys or cron changes are required. `NBA_PICK_SNAPSHOTS=0` is the off switch.

## Dead-code candidates (not deleted)

- `nba.py.before_opening_day_fix.bak` (repo root): a backup of `nba.py`.
- `dashboard/today.py` `job_chips()`: only the tests use it since the health panel replaced the chip strip.
- `dashboard/app.py` `run_backtest()` and the legacy 2024-25 sections: kept on purpose, labelled IN-SAMPLE.
  They can go once you no longer want the comparison.
- `src/models/edge_finder.py`: used only by `dashboard/app.py` and two research scripts
  (`scripts/backtest_props_vs_market.py`, `scripts/validate_playoffs.py`). It's the L10-line logic the memory notes call fake edge.
- Research scripts that never feed the live board (`scripts/ablation_*.py`, `phase1_calibration_audit.py`,
  `phase2_isotonic_fg3m.py`): allowlisted in `tests/test_model_config.py`.
- **Root `nba.py` is NOT dead.** `grep -rnE "nba\.py|^import nba$|^from nba import" src scripts tests` finds
  `scripts/run_daily.py:53` (runs `nba.py daily`), `tests/test_daily_workflow.py:15`,
  `tests/test_pipeline_integrity.py:10` and `tests/test_step_status.py:10`.
