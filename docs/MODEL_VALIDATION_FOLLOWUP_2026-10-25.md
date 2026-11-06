# Repaired-model validation follow-up — October 25, 2026

## Decision

The system remains **diagnostic-only**. No replacement probability model was deployed, no current pick is approved, and maximum approved entry cost is **$0**. This decision now rests on implemented population corrections and temporal statistical/economic tests, rather than only review flags.

All six original forecast/data/result artifact hashes still match the earlier audit. All ten files in the follow-up manifest also match, including every original trained model/calibrator and the existing feature parquet/database. The original audit and its artifacts were retained. No production refresh, grading, betting, paid data access, or unrelated retraining occurred.

## 1. Target-population correction

`src/features/population.py` implements version `appearance-v2`. It constructs player windows from **every positive-minute appearance**, builds team/opponent aggregates from all positive-minute players, and decides rotation eligibility using only prior `min_L10 >= 25`. The eligibility threshold is the existing daily rotation threshold; current-game minutes are never an eligibility input. A player who subsequently plays five minutes remains a training/evaluation outcome.

The same module supplies inference features with unknown current outcomes and date-bounded history. Regression tests compare inference against historical training features, then corrupt current/future minutes, points and attempts to ensure forecasts cannot change. Both pace features, opponent defense, context and player rolling windows agree.

The experiment fixes a recipe before the holdout: 33,202 eligible regular-season appearances in 2021-22 through 2023-24 train four affected component models (PTS, REB, AST, 3PM), expected minutes, and a sub-12-minute risk classifier. Calibration uses 2024-25. Testing uses 2025-26. Original model files are not refitted or replaced. One additional PTS ablation uses the identical features/recipe but excludes sub-12-minute **targets**, isolating the target-selection effect.

| 2025-26 test population | n | All-appearance PTS MAE | Filtered-target PTS MAE | All-appearance scoring bias | Filtered-target scoring bias |
| --- | ---: | ---: | ---: | ---: | ---: |
| All pregame-eligible appearances | 11,327 | 5.4567 | 5.4678 | +0.2437 | +0.3414 |
| Subsequently below 12 minutes | 137 | 11.1337 | 11.3501 | +11.1337 | +11.3501 |
| Subsequently at least 12 minutes | 11,190 | 5.3872 | 5.3958 | +0.1103 | +0.2066 |

This confirms an upward selection bias from removing low-minute outcomes. The correction helps, but sudden minute losses remain difficult to forecast. Among this pregame rotation cohort, low-minute appearances represent 1.21% of the full-season holdout; in the first current-season snapshot they represent 20/368, or 5.43%. The latter is descriptive evidence of opening-season distribution change, not an independently validated risk adjustment.

The minutes-risk classifier's held-out Brier score is 0.01153 versus 0.01195 for the training-rate constant; log loss is 0.05962 versus 0.06562. Expected-minutes MAE is 4.32 minutes. These modest improvements do not establish reliable prop-tail probabilities.

DNPs are not zeros in this distribution. Probability estimates are conditional on positive-minute participation. A DNP follows the venue's verified settlement rule: void when that rule is void, otherwise review. The zero-minute archive is incomplete, so an unconditional participation probability cannot be inferred from its missing rows. In particular, conditional sportsbook probabilities cannot automatically price Kalshi NO contracts if their DNP settlement differs.

## 2. Probability validation and deployment decision

New market calibrators use date-bounded version-2 predictions, market no-vig logit, model disagreement, and predicted minutes risk. They are fitted on 2024-25 and evaluated on the later season. No coefficients were tuned to today's picks or odds.

| Market | Test pairs | V2 Brier | Bookmaker Brier | V2 log loss | Bookmaker log loss | V2 calibration error / bookmaker error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| PTS | 2,744 | 0.24690 | 0.24710 | 0.68553 | 0.68626 | 0.02168 / 0.01293 |
| REB | 2,633 | 0.24154 | 0.24195 | 0.67451 | 0.67573 | 0.02138 / 0.01485 |
| AST | 2,190 | 0.24624 | 0.24519 | 0.68513 | 0.68277 | 0.03668 / 0.02850 |
| 3PM | 2,306 | 0.23888 | 0.23936 | 0.66840 | 0.66973 | 0.02538 / 0.04136 |
| PRA | 2,348 | 0.24796 | 0.24803 | 0.68889 | 0.68907 | 0.01064 / 0.00546 |

Calibration error is ten-bin expected calibration error; complete reliability bins are saved. Lower Brier/log loss is better. Date-cluster paired bootstrap intervals include zero for PTS, REB, 3PM and PRA improvement. AST is worse: Brier difference +0.001055, 95% interval [+0.000009, +0.002044]. This is not convincing evidence for deploying a model-derived disagreement edge.

Critically, these are **not full-season market samples**. The usable regular-season quote holdout spans only 42 distinct dates, October 21 through January 15, with substantial gaps. The archive contains no December 2025 PTS quotes and lacks February/March PTS coverage. REB+AST has only **74 regular-season matched pairs on two dates** in 2025-26; most of its other matched rows are postseason. Its new calibration is unsupported and was not fitted. The coverage artifact records the season/type counts.

Historical sides after 2024-25 can be inferred from ESPN ordering, and opening-quote issue times were not archived. Results using those rows are research evidence with explicit provenance limitations, not proof of executable probability accuracy.

The versioned research bundle is:

`data/audits/2026-10-25/followup/2026-10-25T215941.947988_0000/appearance_v2_research.pkl`

The pace wiring repair alone does not invalidate a fitted tree's original feature definition: it restores the intended opponent/own distinction. However, changing the minute population and using complete team aggregates changes the training distribution. Inference correction alone is insufficient for that change; versioned feature rebuilding, affected-component fitting and fresh probability calibration were necessary and were performed. No unrelated composite models were retrained. The evidence does not support promoting this replacement. Legacy probabilities remain labeled unvalidated; the new bundle is an explicit research-only replay path.

## 3. Timestamped injury/role handling

`src/models/role_context.py` implements a generic conditional role estimator using player IDs, canonical NBA game IDs, team identity, source URL, publication time and tipoff. It rejects evidence published after tipoff or the forecast cutoff, mismatched teams/dates, and OUT assertions contradicted by participation. Later available statuses supersede earlier OUT statuses. A joint absence pattern requires evidence for **every** requested absent teammate; a single missing box-score row is never an injury assertion.

The verified ledger contains **25 Banchero-out cases**: 15 in 2024-25 and ten in 2025-26. Each came from an official report published before that game's tipoff. Examples include [November 1, 2024](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2024-11-01_12AM.pdf) and [November 14, 2025](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-11-14_11AM.pdf). The ledger stores the exact publication/tipoff instants in UTC and distinguishes normalized fact extraction from a raw PDF download. It is data, not a hardcoded adjustment in model code.

Chronological evaluation predicts each of the ten later Wagner appearances using only earlier verified cases and compares with his pregame last-ten baseline:

| Target | Conditional-role MAE | Last-ten baseline MAE | Paired difference 95% interval |
| --- | ---: | ---: | --- |
| Minutes | 3.850 | 4.050 | [-1.081, +0.678] |
| Points | 4.976 | 4.880 | [-1.351, +1.537] |
| Rebounds | 0.961 | 1.220 | [-0.726, +0.285] |
| Assists | 2.233 | 1.650 | [-0.223, +1.329] |
| Team opportunity share | 0.04387 | 0.03493 | [-0.01126, +0.02608] |

Opportunity share is `(player FGA + .44*FTA + TOV) / team total`, not the NBA's official on-court usage percentage. The estimator also measures scoring, rebounding, assist and shooting opportunities per minute. Across all 25 verified absence cases, Wagner averaged 33.68 minutes, 25.12 points, 5.92 rebounds, 5.24 assists and 21.42% of team opportunity. His current last-ten baseline is 28.7 minutes, 18.2 points, 5.1 rebounds, 3.8 assists and 16.12% of team opportunity.

Those differences establish a plausible role shift, **not** a trustworthy numerical probability adjustment. The held-out errors and uncertainty do not support automatic deployment; other simultaneous absences and roster changes confound transferability. Today's full Orlando absence pattern also lacks matched joint-pattern evidence. The new role context is attached to future boards with its actual evidence count and precise limitations, and no adjustment is applied automatically. No under was reversed because of the player's name or a narrative assumption.

The [October 25, 5:30 p.m. ET official report](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-10-25_05_30PM.pdf) was accessible through the web tool. It still lists Banchero out, now lists Anthony Black available and Jose Alvarado out, and adds VJ Edgecombe questionable. Those later facts are saved separately and never inserted into the original 1:34 p.m. Pacific forecast replay.

## 4. Opening-week teams and bookmaker baseline

The previous evaluation's baseline was the expanding historical unconditional home-win rate, not a bookmaker. It excluded both teams' first ten games, so it did not validate opening-week probabilities.

The new candidate uses `TeamRatingEngine(aggregate_mode='game_logs')`. It reconstructs cold-start strength from scoring margins available before each forecast date; previous-season priors and HCA use only prior-season games. This avoids mutable/future `team_stats` snapshots. Mean game margin is explicitly a reconstructible alternative, not misrepresented as an archived official NET_RTG value. The original default snapshot engine remains unchanged.

Calibration fits the first-ten-game cohorts in 2023-24 and 2024-25: **311 games**. The fixed later-season holdout has 155 first-ten-game games, including **53 games in the first seven calendar days**. The corresponding historical training opening-week cohort is 103 games. All 53 and 155 holdout games have paired bookmaker opening ML quotes. DraftKings is preferred by a fixed rule, followed by stored provider priority; outcomes do not select the book. Home/away implied probabilities from the same bookmaker row are normalized to remove vig.

| Holdout cohort / estimate | n | Brier | Log loss | Ten-bin calibration error |
| --- | ---: | ---: | ---: | ---: |
| Opening week: legacy scale on reconstructed ratings | 53 | 0.22648 | 0.64309 | 0.14515 |
| Opening week: newly fitted calibration | 53 | 0.22730 | 0.64673 | 0.13135 |
| Opening week: expanding home-win baseline | 53 | 0.24584 | 0.68482 | 0.05497 |
| Opening week: bookmaker no-vig | 53 | 0.21826 | 0.62705 | 0.15141 |
| First ten: legacy scale on reconstructed ratings | 155 | 0.21149 | 0.61019 | 0.11593 |
| First ten: newly fitted calibration | 155 | 0.21783 | 0.62648 | 0.13122 |
| First ten: expanding home-win baseline | 155 | 0.24525 | 0.68364 | 0.06296 |
| First ten: bookmaker no-vig | 155 | 0.19579 | 0.57547 | 0.08025 |

The new calibration does not improve the legacy scaling. Its paired Brier disadvantage versus the bookmaker over the first ten games is +0.02204, date-cluster 95% interval [+0.00943, +0.03528]. Opening-week estimates have only seven independent date clusters; the interval is wide and ten-bin calibration numbers are noisy. The replacement was not deployed.

This is a retrospective temporal experiment, not an untouched prospective test. Historical bookmaker fields are designated opening quotes but their exact issue times are unavailable, so an exactly simultaneous as-of bookmaker comparison is still not established. The model's inputs are date-bounded, and no closing/in-game price was substituted. Injury/roster changes are not explicitly modeled in this team candidate. Later-season accuracy is not used to waive these failures.

## 5. Fixed KEA/EV/exposure policy test

The policies stayed unchanged: expected return >=4%, and probability advantage over break-even >=3 percentage points. They were applied separately to the saved temporal holdout. Returns below are hypothetical flat one-unit arithmetic at archived best opening decimals, without extra fees; they are not executable profit claims.

| Research screen | Candidates | Dates | Mean predicted probability | Observed hit rate | Unit ROI | Date-cluster ROI 95% interval |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| EV only | 510 | 41 | 58.02% | 56.27% | +4.57% | [-3.69%, +12.71%] |
| EV and KEA | 289 | 40 | 59.95% | 56.06% | +2.76% | [-7.03%, +12.35%] |
| EV and KEA, highest-EV candidate per player/date | 232 | 40 | 60.12% | 53.88% | -1.16% | [-12.97%, +10.09%] |

Forty-six player/dates have overlapping candidates before the exposure restriction. None of these intervals establishes reliable profitability. The player/date restriction is a declared research exposure screen, not an approved portfolio or assumed independence. There are no combination recommendations and no invented conservative joint probabilities.

## 6. Network diagnosis and corrected reruns

DNS fails not only for ESPN/NBA hosts but also unrelated domains. No HTTP/HTTPS proxy is configured. Connecting to a literal IP is denied with `Operation not permitted`. This identifies an execution-environment DNS/egress restriction rather than an API key, typo or isolated NBA endpoint failure. It cannot be repaired by adding retries inside the model. No unauthorized network workaround or paid source was used.

The web tool could retrieve official injury PDFs and normalized current status facts. [NBA.com showed LAL–UTA and IND–BKN live](https://www.nba.com/games?date=2026-10-25). Those games are excluded from pregame approval. ESPN's HTML odds page returned a JavaScript verification challenge; its API remained inaccessible. Official team-roster pages did not expose usable roster payloads through the web tool. Therefore no fresh executable player quote, complete current roster response, or actual all-in Kalshi order cost was obtained.

`src/data/live_evidence.py` and `scripts/run_daily.py` archive request/capture timestamps, HTTP Date/Last-Modified when supplied, raw successful JSON payloads, body hashes, roster responses when accessible, and explicit failures. The runner attempts these before grading/refreshing. The completed rerun exited **2** on DNS failures, before changing production data or grades; it did not report a false empty slate. Future successful automatic prop fetches also preserve raw source items and distinguish explicit source-side labels from inferred pair ordering. Contradictory labels are rejected; authoritative explicit labels can clear that particular mapping check, without clearing unrelated validation requirements.

The version-2 historical replay was also completed using only saved original lines and pre-forecast box scores. Its research probabilities are approximately 66.18% for Wagner PRA under and 62.52% for Wagner PTS under; George REB+AST has no supported new calibration. The two numerical research estimates pass the nominal KEA screen at the **old saved** prices but remain unvalidated and unapproved. They are not current recommendations, received no injury adjustment, and never replaced original predictions or results. The replay records zero approved entry cost.

## 7. What is resolved, and what is still required

Implemented and tested:

- Correct full-appearance target population, pregame-only rotation eligibility, complete team aggregates and shared training/inference feature semantics.
- Selective versioned fitting of the affected component/minutes models, temporal calibration/holdout evaluation, and a target-filter ablation.
- Generic timestamp/identity/participation-safe role estimation, chronological validation and live board attachment of evidence counts/reasons.
- Reconstructible early-season ratings and separate opening-week/first-ten evaluations against both the previous baseline and paired no-vig bookmaker quotes.
- Fixed KEA/EV policy and exposure evaluation, explicit source-side evidence, append-only live capture and failure-aware normal daily execution.

Still required before probability deployment or approved recommendations:

1. **Network-enabled execution:** restore DNS and outbound HTTPS for the runner, or execute the normal command from a terminal that has them. This only enables collection; it does not clear the statistical blockers.
2. **Complete timestamped historical markets:** repair the regular-season quote coverage gaps, especially December 2025 and February/March 2026, and expand the two-date REB+AST holdout. Historical side labels and issue times must be verified from authoritative source data. If ESPN no longer retains them, a different permitted archive is needed.
3. **A broader pregame injury/roster ledger:** collect report updates and exact active/rotation context across teams and seasons, including control cases and joint absences. Fit and evaluate transferable minutes/usage/stat distributions conditional on information known before tipoff. Twenty-five single-player absence cases did not establish reliable scoring/playmaking improvement or today's joint absence effect.
4. **Independent probability/policy validation:** the current player improvements are inconclusive, AST is worse, the team calibration is worse, and fixed-screen ROI intervals include losses. Develop the next candidate on training data, reserve new independent seasons/date blocks or prospective data, and validate the probability disagreement and selected-policy cohort without tuning on today's lines or this already-inspected holdout.
5. **Venue-specific settlement and cost:** verify participation/DNP/push rules, exact market definition, fresh paired prices and order fees before using any model probability for sizing. No evidence currently supports an approved maximum entry cost above zero.

A quote refresh alone is explicitly **not completion** of these requirements. No model-validation, KEA or exposure threshold was relaxed.

## 8. Verification and artifacts

The full suite passes: **96 passed, 2 skipped**, versus 88 passed before this follow-up. New regression cases cover low-minute target retention, bench-inclusive defense, historical/inference parity, future-outcome perturbation, DNP/push semantics, absence publication/identity/contradiction/joint-pattern rules, later status supersession, reconstructed team rating date bounds, explicit versus inferred quote sides, network failure propagation, and scheduled-status lag after tipoff. `git diff --check` passes. A benign existing joblib CPU-detection warning remains.

Research artifacts are under `data/audits/2026-10-25/followup/`:

- Player model/holdout and extended validation: `2026-10-25T215941.947988_0000/`.
- Cold-start team holdout and extended validation: `2026-10-25T215941.947986_0000/`.
- `verified_absence_evidence.json`, timestamped `role_validation.revision-*.json`, normalized official/current status facts, `population_v2_replay.json`, `reb_ast_calibration_coverage.json`, and `workflow_assessment.json`.
- Live failure/success capture directories under `data/live_snapshots/`.

Code added: `src/features/population.py`, `src/models/role_context.py`, `src/data/live_evidence.py`; `scripts/validate_repaired_models.py`, `validate_role_context.py`, `evaluate_repaired_policy.py`, `replay_repaired_population.py`, `run_daily.py`; `tests/test_repaired_population.py`.

Existing files extended: `src/models/team_ratings.py`, `src/data/espn_props.py`, `scripts/find_bets.py`, and `README.md`. Earlier uncommitted work and completed fixes remain in place.

## Normal daily command

```bash
cd /Users/omedakbari/projects/nba-edge-model && caffeinate -i ./venv/bin/python -u scripts/run_daily.py
```

This archives live evidence and runs the normal daily workflow when transport is available. It never trains or promotes research models. It reports explicit failure when required live transport is unavailable. Successful transport does not make the current diagnostic models approved.
