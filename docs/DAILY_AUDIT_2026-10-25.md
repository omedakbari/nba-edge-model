# October 25 daily-run audit

Zero approved picks. No bets, paid APIs, model retraining, data refresh, or production grading were performed during this audit. Existing work was inspected before edits; there were no applicable AGENTS.md files in the repository or its parent directories. The repository already contained substantial modified and untracked work, which was retained.

## Preservation and reproducibility

The original fair board, market picks, team picks, results CSV, SQLite database, and features parquet have unchanged SHA-256 hashes. The manifest is `data/audits/2026-10-25/original_manifest.json`. The initial tracked diff and copies of the starting Python sources are retained alongside it. `session_changes.patch` isolates changes to existing Python files made in this session from the pre-existing work.

Results are in `data/audits/2026-10-25/corrected_replay.json` (including chronological evaluation), `team_walk_forward.csv`, and timestamped `corrected_replay.revision-*.json` (latest feature comparison). These are diagnostic replays, not fresh executable recommendations. Future fair/team/market forecast saves use timestamped revision filenames when a day's original exists. The grader continues to use the original daily file; revised predictions are deliberately not silently substituted into historical results.

## Findings: calculations, mappings, and features

The stored season is 2026-27. Franz Wagner's NBA ID is 1630532 and his team is ORL; Keyonte George's is 1641718 and his team is UTA. The NBA's current [Wagner profile](https://www.nba.com/player/1630532/franz-wagner) and [George profile](https://www.nba.com/player/1641718/keyonte-george) agree with these identities. The official 4 p.m. ET report confirms ORL at NYK and LAL at UTA. Wagner is away; George is home. Their saved team/opponent pairs are correct.

Ingestion parses each box score's MATCHUP into team/opponent/home fields; composite stats are SQLite generated columns. Across the stored pre-forecast rows, there are zero inconsistencies between the matchup text and those fields. There are zero conflicting team/opponent/orientation assignments per team/game. The 2024-25 and 2025-26 regular seasons each contain 1,230 games, have reciprocal team records, have no summed-score/win-label sign disagreements, and have no home-score disagreements against matching historical ESPN score records. The 2019-20 and 2020-21 archives do have 964 nonreciprocal team-game records; they are not complete historical seasons. The evaluation does not use those seasons as its test set.

The previously described wrong-matchup problem was **not reproduced in the saved October 25 selections**. That does not establish every historical roster join was correct. Recent box scores cannot certify a current roster: for example, the DB's last appearances for Brandon Ingram and Max Strus are on different teams from the current official report. Roster fallback now excludes future games and players whose later recorded team differs. Duplicate/ambiguous player contexts raise an error instead of allowing a last-row-wins join. Player props also reject a supplied quote whose event teams differ from the inference context. ESPN athlete IDs are retained separately from NBA player IDs; these namespaces are not conflated.

Confirmed inference defects were more than cosmetic:

| Field | Wagner before → corrected | George before → corrected |
| --- | --- | --- |
| game ID | 0022600096 → 401909863 | 0022600100 → 401909861 |
| season game number | 2 → 3 | 2 → 3 |
| opponent pace | 107.912 → 107.963 | 114.515 → 106.512 |
| own pace | 108.845 → 107.912 | 116.347 → 114.515 |

The original inference path wrote own-team pace into `pace_L15` and retained an old `own_pace_L15`. Training uses `pace_L15` for the opponent and a separate own-team feature. The wiring is corrected, game/season metadata is refreshed, and regular-season rows no longer inherit playoff context. Missing required model features now raise an explicit error. Saved model feature order is preserved. Current rolling player windows already included the latest completed game due to pre-existing uncommitted fixes; that work was retained.

These corrections did not change the listed candidates' rounded predictions or probabilities. The fitted tree models did not change their resulting predictions for these small input changes. Opponent labels, IDs, model inputs, and predicted numbers should not be treated as interchangeable evidence of a display-only issue.

Training and inference both condition player history on at least 12 minutes. That is consistent with the saved training recipe but can bias full-game under probabilities by excluding low-minute appearances. About 18.5% of positive-minute historical rows fall below this filter. Also, training computes team aggregates after this player filter while inference sums complete box scores. This remaining population mismatch requires a separately versioned training/evaluation change; existing model files and parquet were not rebuilt. Approval stays blocked rather than claiming this population issue is calibrated away.

## Team probabilities and chronological evaluation

The active engine combines SRS, L10 form, historical team home advantage, rest, and an opening-season prior. `WIN_PROB_SCALE=0.664` is inherited from an earlier rating-system backtest, not established validation of the current SRS/prior implementation. The old 68.2% display has been removed. Probabilities remain visible as **unvalidated diagnostics**; they cannot generate ML picks or approved stakes. ATS strength is displayed independently.

The old historical backtest uses a separate inline engine and mutable season aggregates, so it was not accepted as current-model validation. The new audit invokes the actual production engine chronologically. Every game-log window ends strictly before the predicted date. Current-season `team_stats` snapshots captured after the prediction cutoff are excluded. The engine accepts an explicit timezone-aware `as_of` timestamp for saved replay. Legacy team-stat timestamps are interpreted as Pacific local time; newly ingested timestamps are UTC with an explicit suffix.

Evaluation covers **2,150 games**: 1,075 in 2024-25 for a research-only calibration fit, then 1,075 in 2025-26 for a later-season holdout. Both teams must have ten current-season games. The baseline is the expanding historical home-win rate, using only earlier dates.

| 2025-26 holdout | Brier (lower better) | Log loss | Calibration error, ten bins |
| --- | ---: | ---: | ---: |
| Legacy scale | 0.20838 | 0.60503 | 0.05289 |
| Raw normal-CDF estimate | 0.20743 | 0.60564 | 0.03828 |
| Research Platt fit on 2024-25 | 0.20712 | 0.60296 | 0.04077 |
| Expanding home-win baseline | 0.24790 | 0.68895 | 0.01690 |

The research fit has intercept -0.0720 and slope 0.8890 on the clipped raw normal-CDF logit. It was **not deployed**. Its modest improvement on this retrospective holdout does not validate October's cold-start regime or injury-driven roster changes. Previous-season final aggregates are assumed available by the following season; their original publication snapshots are unavailable. The model design was selected after these historical seasons, so this is not a prospective untouched validation exercise. Full reliability bins and game-level predictions are saved for review.

The October 25 diagnostic team margins/probabilities reproduce exactly: BKN 46.29%, LAC 24.48%, DET 69.43%. No probabilities were adjusted toward today's odds.

## Injuries, minutes, and role changes

The [NBA's October 25, 4 p.m. ET report](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-10-25_04_00PM.pdf) confirms Banchero and Duren out, Thompson questionable, Embiid out, and the five Clippers absences in the request. It also lists Anthony Black questionable and additional Orlando absences. That report was available before the saved market forecast at 20:34:58 UTC / 1:34:58 p.m. Pacific. The feature file was written at 20:34:29 UTC; the latest box-score insert was 20:34:16 UTC. The current-season team-stat snapshot corresponds to 20:34:18 UTC under its legacy local-time encoding, before the saved forecast.

The player code excluded a candidate listed out but did not model a teammate's absence or redistribute minutes, scoring, or playmaking. The team engine has no explicit injury component. Player rolling history can reflect prior absences indirectly; that is not an explicit adjustment for today's lineup. Market consensus may incorporate injury information, but the original run discarded the quote-update history and injury payload, so precisely which news was priced cannot be established.

Wagner's last ten recorded appearances averaged 28.7 minutes, 18.2 points, 5.1 rebounds, and 3.8 assists. His first two current-season appearances were 29 and 33 minutes, with 22 and 18 points and five assists each. In 2025-26 plus the first two current-season games, the available records show:

| Banchero record | Wagner appearances | Minutes | Points | Rebounds | Assists | FGA | FTA |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Banchero also appeared | 32 | 28.69 | 18.75 | 5.03 | 3.34 | 14.22 | 4.97 |
| No recorded Banchero appearance | 10 | 33.90 | 23.60 | 6.10 | 3.90 | 16.40 | 7.60 |

These are observational comparisons, not expected minutes or causal usage adjustments. No recorded appearance is not verified injury status. Shot attempts/free throws are usage proxies; a defensible expected usage rate or today's adjusted assist distribution was not established. The sample supports requiring role review, not automatically reversing an under. No player-specific numerical adjustment was hardcoded.

New boards save teammate/opponent injury flags and raw timestamped injury responses when available. Missing role validation blocks approval. The same gate applies to George and the team selections; it is not limited to the names in this audit.

## Spread signs, strength, and market probabilities

Philadelphia's -8.3 is an **expected home scoring margin**, equivalent to PHI +8.3 / DET -8.3 as fair betting handicaps. At market PHI +1.5 / DET -1.5, the home ATS edge is -8.3 + 1.5 = -6.8, favoring Detroit. The calculation and existing spread grading are correct. Display names were misleading; explicit expected-margin and fair-handicap fields were added.

Brooklyn's 4.96-point ATS edge and the Clippers' 5.33-point ATS edge are LEAN under the 5.5-point STRONG threshold. The previous shared STRONG tier came from their 13.79- and 14.36-point diagnostic ML advantages, not a spread-threshold error. The output now separates ATS and ML strength, and unvalidated ML probabilities cannot elevate the active ATS tier or generate ML selections. Detroit's 6.8-point ATS disagreement remains STRONG as a diagnostic strength label, with no betting approval.

The printed ML implied probabilities include vig. ML edges subtract probabilities renormalized from the paired home/away quotes. That difference was intentional; the display now explicitly explains it. It is not sportsbook break-even probability, and it is not KEA eligibility.

## Prices, EV, eligibility, and exposure

The original ESPN team fetch uses scoreboard spread/details and the moneyline `close` node. Those field names do not prove opening prices, a verified closing snapshot, or freshness. The old footer's claim that these were opening lines was unsupported. Original game-odds capture time is 20:35:06 UTC, but provider quote-update times were not saved. Original player boards retained best decimal prices but lost bookmaker identity, raw pairs, ESPN athlete/event IDs, and source update times. Some ESPN prop sides are inferred from item ordering, which is not authoritative exact-side validation.

Future prop snapshots preserve raw rows and scoreboard data, source timestamps, event and athlete IDs, selected book, opening/current designation, and paired quotes. Vig removal requires matching event, book, threshold, quote-update timestamp, and one unambiguous quote per side. Current sides cannot silently fall back to the opposite side's opening price. Invalid/missing/nonfinite prices, duplicate quotes, unknown spread teams, invalid moneylines, unknown game status, and mismatched dates are rejected. Source side-order inference still blocks approval; no unverified API ordering is promoted into settlement certainty.

Sportsbook return is `p * decimal_payout - 1`; probability advantage is `p - 1/decimal_payout`. Both calculations are retained separately. American prices printed by the original code were rounded approximations of ESPN's decimal quotes; this creates small differences from recalculating with exactly -122/-135/-120. New automatic sportsbook candidates display the actual stored decimal price and keep any American conversion explicitly approximate.

| Candidate | Original → corrected diagnostic P | Original → corrected EV | Probability advantage at saved decimal | KEA 3-point screen | Approved stake |
| --- | ---: | ---: | ---: | --- | ---: |
| Wagner UNDER 33.5 PRA, decimal 1.82 | 57.6463% → same | 4.9162% → same | 2.7012 points | FAIL | $0 |
| George UNDER 10.5 REB+AST, decimal 1.74 | 60.2000% → same | 4.7481% → same | 2.7288 points | FAIL | $0 |
| Wagner UNDER 22.5 PTS, decimal 1.83 | 56.8465% → same | 4.0291% → same | 2.2017 points | FAIL | $0 |

Their 4% EV-policy passes were not EV formula bugs. All fail the separate configurable KEA margin, even before the probability, injury/role, exact-side, freshness, and settlement review gates. Per-candidate Kelly calculations are no longer printed as usable stakes for blocked candidates. Wagner's two unders share an exposure group and are explicitly flagged as overlapping. There is no approved combined allocation.

The earlier 2023-24 EV-policy ROI research used blend parameters fitted on later seasons. That is retrospective policy research, not chronological out-of-sample approval evidence. The 4% return screen is retained as an existing candidate policy without claiming that ROI validates today's probabilities.

For Kalshi, authoritative break-even is actual all-in order cost divided by gross winning payout. A new helper calculates from those actual amounts. The existing generic 100-contract fee estimate is retained only as an explicitly unverified estimate; the [current exchange fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) includes market-specific multipliers and order-dependent rounding. No sportsbook EV is represented as executable Kalshi EV. Only explicit N+ integer-stat titles consistent with strike metadata receive a diagnostic mapping: NO on 23+ points corresponds to UNDER 22.5 points when settlement rules match. Ambiguous thresholds are skipped, and actual rules/fees remain an approval gate.

Entry pricing no longer manufactures a joint success probability by multiplying point estimates. CSB requires a defensible conservative joint probability of at least 80%; none is available here. A dependence-safe union-bound helper accepts externally validated lower bounds, but no such bounds were invented or substituted for these candidates.

## Validation and remaining limits

The complete suite passed: **88 passed, 2 skipped**. Existing tests were preserved. Added regression cases cover inference identity/orientation, both pace features, duplicate rosters, missing model features, spread edge/sign/grades/pushes, ATS/ML distinction, invalid prices, same-book/time/event quote pairs, KEA versus EV, injury/probability/freshness/pregame gates, exact Kalshi thresholds, actual-cost economics, grading identity, joint bounds, prediction revisions, and historical aggregate leakage. A benign existing joblib CPU-detection warning remains. `git diff --check` passed.

The three saved player probabilities/EVs and all seven team margins/probabilities were reproduced without retraining. All six original artifact hashes match. No historical results were regraded.

An exact contemporaneous reconstruction is impossible because the original raw quotes, roster response, and injury response were not archived. Corrected model inputs are available as separate audit artifacts, but full-game player probabilities remain unvalidated because of minutes/role/population issues; team opening-week probabilities remain unvalidated because suitable historical snapshots and injury-aware validation are absent. API DNS resolution is blocked in this execution environment, so no fresh executable market or live game-status claim was made. Current approval requires fresh quote and start timestamps and scheduled status; absent verification, it fails closed. Started/finished games cannot receive approval.

## Files changed in this session

Existing files edited: `nba.py`; `scripts/find_bets.py`, `generate_todays_picks.py`, `generate_todays_team_picks.py`, `log_pick_results.py`, `log_team_results.py`; `src/betting/ev.py`; `src/data/espn_odds.py`, `espn_props.py`; `src/ingestion/team_stats.py`; `src/models/team_ratings.py`.

Added: `src/utils/slate.py`, `src/betting/eligibility.py`, `scripts/audit_daily_run.py`, `tests/test_oct25_audit.py`, this report, and audit artifacts under `data/audits/2026-10-25/`. Other pre-existing modified/untracked files were left in place.

To repeat the offline audit without touching production forecasts or results:

```bash
cd /Users/omedakbari/projects/nba-edge-model
./venv/bin/python -u scripts/audit_daily_run.py --date 2026-10-25
```

Add `--walk-forward` to rerun the full chronological evaluation. To obtain fresh pregame quotes from a terminal with working network access, without a data refresh or retraining:

```bash
cd /Users/omedakbari/projects/nba-edge-model
./venv/bin/python -u scripts/find_bets.py --date 2026-10-25 --season 2026-27 --save
```

Fresh prices alone do not clear the unvalidated probability and role gates. Zero approved picks is the current result.
