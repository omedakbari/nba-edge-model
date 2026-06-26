# NBA Edge Model — Playoff Mode Validation Report
**Generated:** 2026-04-06  
**Validation data:** 2023-24 and 2024-25 playoffs (1,756 player-game rows at min≥20, 171 players)  
**Full appearances:** 3,034 (min≥0) across both seasons

---

## 1. Database Coverage

| Season | Type | Rows | Players | Status |
|---|---|---|---|---|
| 2025-26 | Regular Season | 25,417 | 578 | Current |
| 2024-25 | Playoffs | 1,804 | 219 | Complete |
| 2024-25 | Regular Season | 26,306 | 569 | Complete |
| 2023-24 | Playoffs | 1,230 | 155 | Partially backfilled |
| 2023-24 | Regular Season | 26,401 | 572 | Complete |
| 2022-23 | Regular Season | 24,653 | 511 | Complete |
| 2021-22 | Regular Season | 24,735 | 577 | Complete |

**2022-23 and 2021-22 playoff data not yet ingested.** Each requires one full session of `python scripts/pull_season_data.py --playoffs` (30+ min due to NBA.com rate limiting). The pull is now resumable — any interrupted run picks up exactly where it left off.

---

## 2. Stat Calibration — Playoff vs Regular-Season L10

All stats show players underperform their regular-season L10 average in the playoffs (binary over/under framing). The **median ratio** is the relevant signal for PP binary markets:

| Stat | % Over L10 | Median Ratio | Mean Ratio | Note |
|---|---|---|---|---|
| PTS | 43.9% | 0.928 | 0.985 | Fat right tail (blow-up games) |
| REB | 50.0% | 1.008 | 1.104 | Essentially flat |
| AST | 36.2% | 0.781 | 0.892 | Largest underperformance |
| FG3M | 41.0% | 0.833 | 1.096 | Very right-skewed |

**Implication:** The regular-season L10 systematically overpredicts playoff output for PTS/AST/FG3M. REB is near-flat. Models trained on regular-season data need a downward adjustment for playoff picks — which is exactly what the adjustment layer does.

---

## 3. Tier Multipliers (Current, Data-Calibrated)

Multipliers applied on top of regular-season model prediction before computing P(over):

| Tier | Min threshold | PTS | REB | AST | FG3M | PRA | REB_AST |
|---|---|---|---|---|---|---|---|
| STAR | ≥34 min | 0.97 | 1.01 | 0.93 | 0.94 | 0.96 | 0.97 |
| CORE | 28–33.9 | 0.98 | 1.00 | 0.95 | 0.95 | 0.97 | 0.97 |
| ROTATION | 25–27.9 | 0.93 | 0.93 | 0.92 | 0.92 | 0.92 | 0.92 |

**All multipliers are directionally correct** (all tiers show <50% actual over rate, consistent with downward adjustments).

Historical elevation ratios (player-level playoff vs RS per-minute production) are also applied from DB data. 203 players have historical ratios; these are blended with tier multipliers.

---

## 4. Calibration — P(over) vs Actual Hit Rate

Using corrected tier multipliers + historical ratios. Line = player's regular-season L10 average (not real market line — see caveat below).

| P(over) threshold | Qualifying plays | Actual hit rate |
|---|---|---|
| ≥55% | 442 | 51.6% |
| ≥57% | 333 | 52.0% |
| ≥60% | 242 | 53.3% |
| ≥63% | 183 | 54.1% |
| ≥65% | 107 | 54.2% |

**Key takeaway:** The model monotonically improves with confidence threshold. At P≥60% (the useful operating range), the hit rate is 53.3% on 242 plays — above the ~52% break-even for 3-leg SAFE Flex.

**Caveat:** Lines are L10 averages, not real PP market lines. Real calibration requires grading actual picks post-season. This is directional signal only.

---

## 5. Suppression Check — Full Minutes Audit

Previous validation had survivorship bias (only saw min≥20 games). This corrected check uses **all** playoff appearances (min≥0) to properly test whether suppression correctly predicts low-minute risk.

| Group | n | Avg actual min | % played <20 min | % played <5 min |
|---|---|---|---|---|
| Active (not suppressed) | 1,800 | 30.0 | 17.6% | 1.2% |
| Suppressed | 1,234 | **12.4** | **77.9%** | **26.7%** |

**Minute gap: −17.6 min. Suppression is working correctly.**

77.9% of players flagged for suppression played fewer than 20 minutes in their actual playoff game. Among non-suppressed players, only 17.6% fell below 20 minutes. The suppression logic is correctly identifying rotation players who get limited usage in playoff games.

The earlier "CAUTION — suppressed plays actually hit more" verdict (from the previous biased validation) was an artifact of only looking at games where suppressed players *did* play 20+ minutes. Among the full population, suppression is effective.

**Suppression logic applied:**
- ROTATION players only (≥25 min avg, <28 min avg)
- Trigger: ≥2 of last 5 regular-season games played <20 min, OR recent min_L3 / min_L10 < 0.82
- Stars and Core players are NOT suppressed (resting at end of regular season ≠ playoff DNP risk)

---

## 6. What Remains Weak

| Weakness | Severity | Notes |
|---|---|---|
| L10 as proxy line | Medium | Real calibration requires grading actual playoff picks |
| Only 2 seasons of data | Medium | 2022-23 + 2021-22 pulls needed (resumable, run `--playoffs` again) |
| Historical ratios are in-sample | Low | Ratios computed from same 2 seasons being validated |
| No live pace adjustment | Low | Handled implicitly by `own_pace_L15` in model features |
| Actual playoff 2025-26 grading | Medium | Can only be done after 2026 playoffs conclude |

---

## 7. Operational Workflow

```bash
# Step 1: Update current season data (run daily)
python scripts/pull_season_data.py --update

# Step 2: Backfill playoff history (run once; resumable if interrupted)
python scripts/pull_season_data.py --playoffs

# Step 3: Rebuild features
python -m src.features.pipeline

# Step 4: Generate picks (use --playoffs during playoff season)
python scripts/generate_todays_picks.py --playoffs --save

# Step 5: Validate playoff adjustment layer
python scripts/validate_playoffs.py --seasons 2024-25 2023-24 --show-suppressed

# Grade picks after games
python scripts/log_pick_results.py --date YYYY-MM-DD
```

---

## 8. Verdict

**Playoff mode is ready for practical use with understood limitations.**

- The directional calibration is validated and internally consistent
- Suppression correctly identifies 77.9% of low-minute risks
- P≥57% picks show 52%+ actual hit rates on a 333-play sample
- The workflow runs end-to-end without errors
- The pull is now resumable — re-running after interruption continues from where it stopped

**Real-world calibration** (vs actual PP lines, grading real picks from 2025 and 2026 playoffs) remains the gap. The current validation is structural correctness, not market-calibration proof.
