# NBA Edge Model — Playoffs Edition Research Blueprint

> Created: 2026-04-06
> Status: Implemented (see `src/playoffs/adjustments.py`)

---

## Purpose

This document records the research rationale and design decisions behind the Playoffs Edition adjustment layer. The implementation is a post-prediction overlay — the regular-season ML model runs unchanged, and playoff-specific corrections are applied to predictions and variance before probability calculation.

---

## Core Problem

The regular-season model (`HistGradientBoostingRegressor`) was trained exclusively on regular-season game logs (2019–2025). Playoffs differ from regular season in ways the model cannot detect:

| Factor | Regular Season | Playoffs |
|---|---|---|
| Pace | ~100 possessions/game | ~96–98 possessions/game |
| Star minutes | 30–34 min (managed load) | 36–40 min |
| Bench/rotation minutes | 18–25 min (role clarity) | Highly volatile — coaches shrink rotations |
| Performance variance | Moderate | Lower for stars, much higher for bench |
| Opponent quality | Mixed | Top-8 teams only — consistently better defense |
| Elimination pressure | None | Heightens for stars; exposes rotation weakness |

---

## Adjustment Layer Design

### Tier Classification

Players are classified into three tiers based on `min_L10` (rolling 10-game average minutes):

| Tier | Threshold | Rationale |
|---|---|---|
| ★ Star | min_L10 ≥ 34 | Coaches trust these players in crunch time; minutes increase |
| ◆ Core | 28 ≤ min_L10 < 34 | Stable role; roughly flat or slight increase |
| ● Rotation | 25 ≤ min_L10 < 28 | Real DNP risk; coaches compress rotations |

(Players below 25 min average are filtered by `MIN_MINUTES = 25` before reaching this layer.)

### Prediction Multipliers

Applied per (tier, stat) to the raw model prediction:

| Stat | STAR | CORE | ROTATION | Reasoning |
|---|---|---|---|---|
| pts | 1.03 | 1.00 | 0.94 | Stars get more usage/FTs; rotation players lose minutes |
| reb | 1.04 | 1.01 | 0.93 | Directly proportional to floor time |
| ast | 1.02 | 1.00 | 0.94 | Playmaking load concentrates on stars |
| fg3m | 1.01 | 0.99 | 0.93 | Pace reduces total attempts; stars maintain volume |
| pra | 1.03 | 1.00 | 0.93 | Composite — weighted avg of components |
| reb_ast | 1.03 | 1.00 | 0.93 | Same as above |

**Historical elevation ratio:** If playoff game logs are present in the DB, a per-player elevation ratio (playoff per-min pts / regular-season per-min pts) is computed and layered on top of the tier multiplier. Capped at [0.88, 1.15] to prevent overfitting on small samples.

### Variance Recalibration

Standard deviation multipliers applied to `{stat}_std_L10`:

| Tier | Std multiplier | Effect on probability |
|---|---|---|
| ★ Star | 0.87 | Tighter distribution → higher confidence when edge exists |
| ◆ Core | 0.93 | Slightly tighter |
| ● Rotation | 1.18 | Wider → lower confidence → fewer rotation plays surface |

**Unstable rotation penalty:** If `low_min_count_L5 ≥ 2` AND not a Star, additional ×1.15 applied to std.

### Suppression Rules

A play is suppressed entirely (excluded from cards) when:
- Rotation tier AND `low_min_count_L5 ≥ 2`
- `min_L3 / min_L10 < 0.82` (recent 3-game avg >18% below L10 — recent minute cut signal)

---

## Validation Approach

### Baseline vs Playoffs Mode Comparison
Run both modes against historical playoff game logs (once pulled). Key metrics:
- Hit rate at P ≥ 57%, 60%, 65% thresholds
- Calibration by confidence bucket
- Whether suppressed rotation plays had lower actual hit rates than non-suppressed

### Historical Playoff Data Required
```bash
python scripts/pull_season_data.py --playoffs
```
Pulls Playoffs season_type for 2021-22 through 2024-25 (~4 playoff seasons).

---

## Known Limitations

1. **Model trained on regular season only** — the underlying gradient boosting has never seen a playoff game. The adjustment layer is a correction, not a retraining.
2. **Multipliers are empirically motivated, not fitted** — until enough playoff data exists, these remain calibrated heuristics.
3. **Rotation suppression cannot detect game-1 DNPs** — the model knows nothing about a coach's real-time decision until the next data pull.
4. **PrizePicks lines may be set differently in playoffs** — sharper lines, different hold %, may shift the calibration point.
5. **Small series samples** — a 7-game series is too small to meaningfully calibrate on; multi-season aggregation is required.

---

## Files

| File | Role |
|---|---|
| `src/playoffs/adjustments.py` | Core adjustment logic |
| `scripts/generate_todays_picks.py` | `--playoffs` flag, wires in adjustment layer |
| `scripts/pull_season_data.py` | `--playoffs` flag, pulls historical playoff game logs |

---

## Commands

```bash
# Pull historical playoff data (one-time, ~20-40 min)
python scripts/pull_season_data.py --playoffs

# Daily playoff workflow
python scripts/pull_season_data.py --update
python -m src.features.pipeline
python scripts/generate_todays_picks.py --playoffs --save

# Grade picks (same as regular season)
python scripts/log_pick_results.py --date YYYY-MM-DD
```
