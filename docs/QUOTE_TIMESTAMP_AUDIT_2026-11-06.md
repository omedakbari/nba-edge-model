# November 6, 2026 — scoped NBA model audit and fixes

## 1. Task 1: root cause, evidence, changes, tests, counts

**Finding: (b), unusable upstream timestamp metadata / old upstream updates, not an observed local timezone parsing defect.** No timestamp corrections or model changes were justified by the November 6 evidence. Freshness remains 900 seconds with no future tolerance. Approval and staking remain disabled/$0. No November 5 picks were generated. Task 3 source files were audited read-only.

The original snapshot is `data/quote_snapshots/2026-11-06.json`. Its comparison instant is **2026-11-06T18:32:23.203700+00:00 (UTC)**. Every one of its 1,508 parsed sides has a string timestamp ending in `Z`. Every parsed row timestamp equals its corresponding `row_evidence.raw.lastUpdated` exactly: **zero mismatches**. No local timezone, DST offset, or epoch unit was assumed. Across 19 saved HTTP responses, local capture minus the server's HTTP Date is 0.013506–0.985890 seconds, consistent with whole-second server headers. Thus the evidence does not support a host clock running hours behind the provider's server.

Example: an upstream `2026-11-06T20:46Z` parses as 20:46 UTC, 8,016.7963 seconds after capture. An upstream `2026-11-06T17:39Z` parses as 17:39 UTC, 3,203.2037 seconds before capture, beyond 900 seconds. These rejections are correct under the current policy. Future `lastUpdated` values are inconsistent metadata; stale values may simply reflect older unchanged markets. Neither finding proves the underlying price itself was wrong. The archive cannot establish ESPN's internal cause or whether its field has different update semantics; there is no evidence supporting a guessed fixed-hour correction.

### Timestamp path and every relevant creation/parse/comparison boundary

| Stage | Location | Behavior |
|---|---|---|
| HTTP request/capture evidence | `src/data/espn_props.py:59` | `_get` creates request/capture times using `datetime.now(timezone.utc)`; records raw HTTP Date/Age/Last-Modified and response hash. HTTP Date is evidence, not a substituted quote-update time. |
| Raw provider timestamp | `src/data/espn_props.py:107` and `:148` | `_fetch_event` copies `p.lastUpdated` unchanged into the row. No seconds/ms conversion or timezone rewriting. |
| Slate and event cutoff | `src/data/espn_props.py:243`; `src/utils/slate.py:8`; `src/data/live_evidence.py:61` | Pacific calendar date selects the slate; explicit event start time is compared with aware UTC now. Past-date fetches deliberately use opening quotes, not historical frozen current values. |
| Consensus comparison time | `src/data/espn_props.py:257` | One aware UTC capture time is created after event fetches and passed into all current-quote checks. |
| Parse/freshness/reject | `src/data/espn_props.py:168`; `src/betting/quote_quality.py:6` | ISO timestamps parse with `Z` expanded to `+00:00`. Naive/invalid input is rejected. Aware subtraction computes elapsed seconds correctly across offsets/DST. Future age <0 and stale age >900 are rejected. |
| Pairing | `src/data/espn_props.py:190` | Remaining sides must match event, athlete, stat, bookmaker, line and update string. Duplicate sides and excessive/invalid overround are excluded; selected modal line determines consensus. |
| Supplemental/reused anchors | `src/data/supplemental_quotes.py:11`; `scripts/find_bets.py:456`; `src/betting/quote_quality.py:22` | `utc_now()` supplies aware UTC; `anchor_issues` rechecks capture times, every pair's update/capture times, scheduled status and start time. |
| Final approval | `scripts/find_bets.py:369`; `src/betting/eligibility.py:14` | Parses capture/update/start as aware times, compares to UTC now, and retains all approval/economic/role/settlement gates. Unchanged. |
| Stack/retrospective checks | `scripts/build_prop_stacks.py:103`; `scripts/report_market_diagnostics.py:53` | Stacks check live freshness; retrospective diagnostics check against the original saved forecast generation instant, not the present clock. |
| New explanations only | `src/data/quote_diagnostics.py:6` | Adds explicit UTC diagnostic renderings, original timezone, comparison time, elapsed age and parse errors; never accepts, corrects or replaces a timestamp. |

### Twenty rejected quote observations

All observations below are November 6, from **Draft Kings**. `Now` in every row is the original **18:32:23.203700 UTC** on that date, not the time this audit ran. `UTC / none` means UTC is explicit in upstream `Z`; no timezone was assumed. The CSV contains the complete dates, comparison instants, row indexes, event IDs, sides and signed ages for every row.

| Event / athlete / stat (over side) | Raw upstream timestamp | Parsed UTC value | Source TZ / assumed TZ | Now (UTC, Nov 6) | Reason |
|---|---|---|---|---|---|
| 401909287 / 5142718 / pts | 2026-11-06T20:46Z | 2026-11-06T20:46:00+00:00 | UTC / none | 18:32:23.203700 | future provider timestamp |
| 401909288 / 4251 / pts | 2026-11-06T20:24Z | 2026-11-06T20:24:00+00:00 | UTC / none | 18:32:23.203700 | future provider timestamp |
| 401909289 / 4351851 / pts | 2026-11-06T20:36Z | 2026-11-06T20:36:00+00:00 | UTC / none | 18:32:23.203700 | future provider timestamp |
| 401909290 / 6442 / pts | 2026-11-06T21:08Z | 2026-11-06T21:08:00+00:00 | UTC / none | 18:32:23.203700 | future provider timestamp |
| 401909291 / 3133626 / pts | 2026-11-06T20:10Z | 2026-11-06T20:10:00+00:00 | UTC / none | 18:32:23.203700 | future provider timestamp |
| 401909292 / 3133628 / pts | 2026-11-06T20:49Z | 2026-11-06T20:49:00+00:00 | UTC / none | 18:32:23.203700 | future provider timestamp |
| 401909293 / 4395628 / pts | 2026-11-06T20:51Z | 2026-11-06T20:51:00+00:00 | UTC / none | 18:32:23.203700 | future provider timestamp |
| 401909294 / 3945274 / pts | 2026-11-06T20:04Z | 2026-11-06T20:04:00+00:00 | UTC / none | 18:32:23.203700 | future provider timestamp |
| 401909295 / 3064440 / pts | 2026-11-06T19:01Z | 2026-11-06T19:01:00+00:00 | UTC / none | 18:32:23.203700 | future provider timestamp |
| 401909287 / 4431680 / ast | 2026-11-06T20:43Z | 2026-11-06T20:43:00+00:00 | UTC / none | 18:32:23.203700 | future provider timestamp |
| 401909287 / 6583 / pts | 2026-11-06T17:39Z | 2026-11-06T17:39:00+00:00 | UTC / none | 18:32:23.203700 | stale provider timestamp |
| 401909289 / 1966 / pts | 2026-11-06T16:45Z | 2026-11-06T16:45:00+00:00 | UTC / none | 18:32:23.203700 | stale provider timestamp |
| 401909290 / 4278049 / pts | 2026-11-06T10:19Z | 2026-11-06T10:19:00+00:00 | UTC / none | 18:32:23.203700 | stale provider timestamp |
| 401909291 / 2991070 / pts | 2026-11-06T18:01Z | 2026-11-06T18:01:00+00:00 | UTC / none | 18:32:23.203700 | stale provider timestamp |
| 401909293 / 3032976 / pts | 2026-11-06T09:49Z | 2026-11-06T09:49:00+00:00 | UTC / none | 18:32:23.203700 | stale provider timestamp |
| 401909294 / 3995 / pts | 2026-11-06T18:01Z | 2026-11-06T18:01:00+00:00 | UTC / none | 18:32:23.203700 | stale provider timestamp |
| 401909295 / 3155942 / pts | 2026-11-06T12:51Z | 2026-11-06T12:51:00+00:00 | UTC / none | 18:32:23.203700 | stale provider timestamp |
| 401909287 / 2490149 / pts | 2026-11-06T13:54Z | 2026-11-06T13:54:00+00:00 | UTC / none | 18:32:23.203700 | stale provider timestamp |
| 401909289 / 3992 / pts | 2026-11-06T17:22Z | 2026-11-06T17:22:00+00:00 | UTC / none | 18:32:23.203700 | stale provider timestamp |
| 401909290 / 6442 / ast | 2026-11-06T11:57Z | 2026-11-06T11:57:00+00:00 | UTC / none | 18:32:23.203700 | stale provider timestamp |

Evidence: `data/audits/2026-11-06_quote_timestamps/replay_verified/audit.json` and `rejected_quote_samples.csv`. The sample deliberately includes ten distinct player/stat observations per rejection reason across multiple games, rather than over/under duplicates of the same observations.

### Why one bookmaker, and which source is affected

| Parsed source | Offered sides | Future | Stale | Timestamp-valid sides | Selected pairs |
|---|---:|---:|---:|---:|---:|
| Draft Kings | 1,508 | 617 | 867 | 24 | 12 |

**One bookmaker was already present before timestamp filtering.** All nine recorded propBets URLs use provider ID 100; all 1,508 rows identify Draft Kings. `_fetch_event` iterates all returned provider items, with no DraftKings-only configuration; it skips provider names containing `live`. No independent second-book feed is provisioned automatically. Supplements use `NBA_SUPPLEMENTAL_QUOTES` / `--book-quotes` (`scripts/find_bets.py:88`, `src/data/supplemental_quotes.py:11`).

The original odds-index response bodies/provider inventory were not saved, so we cannot reconstruct any live-only providers skipped at the index stage or upstream provider pagination not captured. The evidence establishes the source actually fetched, not every bookmaker ESPN might offer elsewhere. New snapshots now retain the advertised provider inventory, skipped-live flag and raw prop-item counts, including empty provider responses. This closes that observability gap without changing provider selection.

A bounded read-only odds-index probe during this session failed DNS in this execution environment; the exact response is in `provider_index_probe.json`. It did not write picks or refresh data. No fresh live coverage improvement is claimed.

### Changes made

Modified existing code:

- `src/data/espn_props.py:107`: archive provider inventory; attach raw/UTC/comparison/age evidence to timestamp rejections; count previously silent missing timestamps; print and persist per-book coverage. Previously rejected missing timestamps remain rejected.
- `scripts/find_bets.py:232`: clarify empty selected-candidate source lists and add separate `scan_summary` fields. No pricing/selection/model/approval change.

Added:

- `src/data/quote_diagnostics.py`: diagnostic timestamp rendering and per-book rejection/coverage summaries.
- `scripts/audit_quote_timestamps.py`: reproducible offline original-time replay, 20-row CSV, HTTP clock comparison and protected input hashes. Writes only the requested new audit directory.
- `tests/test_quote_timestamp_audit.py`: 14 additional test cases, including parameterized cases.
- This report and isolated artifacts under `data/audits/2026-11-06_quote_timestamps/`.

Tests cover naive input rejection, explicit offsets, `Z`, epoch seconds/milliseconds remaining unsupported/rejected, the Pacific DST repeated hour, exact 900-second/future boundaries, mixed/missing per-book timestamps, multiple advertised providers/live filtering, append-only snapshot provenance, offline replay and empty-candidate source reporting. Epoch support was not introduced: no epoch inputs occur in this evidence and accepting a new format would alter the current contract unnecessarily.

Baseline: **114 passed, 2 skipped**. After changes: **128 passed, 2 skipped**. One pre-existing joblib CPU-detection warning remains. `git diff --check` passes.

### Before/after, on the same original-time inputs

| Measure | Before | After diagnostic changes |
|---|---:|---:|
| Parsed quote sides | 1,508 | 1,508 |
| Future / stale rejected sides | 617 / 867 | 617 / 867 |
| Timestamp-valid sides | 24 | 24 |
| Paired book prop lines | 12 | 12 |
| Saved eligible props | 7 | 7 |
| Anchor bookmakers | 1 | 1 |
| Sportsbook sides priced from saved board | 14 | 14 |
| Sportsbook candidates at existing 4% screen | 0 | 0 |
| Approved stake | $0 | $0 |

Loading the preserved original quote reader and the updated reader produces exactly identical consensus dictionaries and rejection identities/reasons (`baseline_equivalence.json`). The replay reruns quote selection and prices the seven **saved** fair-board forecasts at their original generation time. It does not rerun model inference, invent historical Kalshi responses, or assert saved prices remain executable. The conditional requirement for a fresh scan after a parsing fix does not apply: no parsing defect was found/fixed, and the live probe failed DNS. Thus counts are an as-of replay, not a fresh live scan.

Preservation: of 218 pre-session source/artifact hashes, **216 are unchanged**; only the two existing source files listed above changed. Original production quotes, fair boards, picks, stacks, results, database, feature exports, model artifacts, approval/freshness code and Task 3 source files are unchanged. See `before_manifest.json` and `preservation_check.json`. November 5 player/team pick files remain absent.

## 2. Task 2: empty market_picks finding and clarification

**Expected output, not evidence of missing configured lines or a demonstrated silent sportsbook failure.** `price_sources` is actually a list (`[]`), not a numeric zero. Its comprehension in `scripts/find_bets.py:266` gathered sources only from selected candidate records. With zero selected records it must be empty, even when seven eligible props were priced.

The saved fair board contains seven DraftKings-anchored props; repricing its fourteen sportsbook sides reproduces zero >=4% candidates. The original run does not archive Kalshi's responses, so its absence cannot be distinguished retrospectively from a suppressed Kalshi transport failure. That separate risk is reported in Task 3e, not claimed fixed here.

The existing field keeps its meaning for compatibility. New `scan_summary` separately reports eligible props, anchor books and selected count, and zero-selection output explicitly explains `price_sources=[]`. Original November 6 output was not rewritten.

## 3. Task 3: read-only findings a–e

### a. Early-season team ratings

- Constants: `config/settings.py:75` sets 20 prior-equivalent games and 0.8 retention. `src/models/team_ratings.py:373` builds the current signal, then blends it at `:412`.
- Exact formula: `rating = n/(n+20) * current_signal + 20/(n+20) * 0.8 * previous_season_rating`, provided at least 20 previous-season games exist. Thus the blend shares of current versus the regressed prior are 28.6%/71.4% after 8 games, 33.3%/66.7% after 10, 50%/50% after 20, and 80.4%/19.6% after 82. **There is no hard cutoff at game 10 or 20; prior influence persists all season.** Prior shrinkage is distinct from blend share: multiply prior share by 0.8 for its coefficient on the unshrunk rating.
- At >=10 current games, SRS is preferred. Earlier the current signal can use official NET_RTG times pace/100; home-neutralized L10 form contributes 35% when at least five games exist. Constants/implementation: `src/models/team_ratings.py:55`, `:73`, `:310`, `:346`, `:396`.
- Prior snapshot ratings come from the previous season's team_stats (`:244`, `:310`). The active engine uses snapshot mode by default (`:215`); research can use reconstructed game-log margins instead (`:314`).
- Home advantage uses three historical seasons (`:159`); rest/B2B uses previous game dates (`:426`); postseason adds fixed HCA/series context (`:460`).
- **No player roster, transaction, player-impact, or explicit injury inputs feed team ratings.** New rosters influence them only indirectly as new game results accumulate. This audit does not quantify the causal contribution of any particular Philadelphia transaction.

### b. Injury/availability influence

- `scripts/generate_todays_picks.py:675` fetches and archives ESPN injury data, but returns only normalized player-name -> broad status. Detailed questionable/probable/doubtful semantics and source timestamps are not carried in that simplified mapping.
- `scripts/find_bets.py:99`, `:107`, `:127`: the daily scanner **excludes the player themself** if status contains out/inactive. Thus availability is not purely display-only; it changes eligibility. Day-To-Day/doubtful players are not automatically excluded by this condition.
- `scripts/find_bets.py:129` calls the conditional-role estimator and stores teammate/opponent injuries and evidence counts. `src/models/role_context.py:43` computes historical conditional summaries with timestamp/identity checks. **Those summaries do not adjust the point forecasts or probabilities.**
- `src/models/appearance_daily.py:23` predicts component stats, minutes and short-minutes risk from historical features; `:39` calibrates using market logit, model disagreement and minutes risk. There are no injury/lineup covariates in these active model inputs. The market anchor may incorporate news externally; that is not our explicit injury model.
- Team calls at `scripts/generate_todays_team_picks.py:248` do not pass injuries. Team projections have no explicit injury adjustment. All approval flags stay false.

### c. Existing validation and missing evidence

- `scripts/backtest_team_model.py:54` / `:113` uses an older inline engine; `:141` reads mutable whole-season team_stats without an as-of bound. It lacks current SRS/prior parity and can leak later aggregate information. Its own-spread coverage is not market ATS validation. Do not use it to certify today's engine.
- `scripts/backtest_vs_market.py:80` chooses stored closing fields when available, otherwise fallback fields. It reconstructs date-bounded totals inputs at `:120` and replays production team ratings at `:151`, but excludes first-ten-game cohorts and assumes -110 spread/total payouts (`:203`). It is research, not evidence of executable closing prices or actual historical price ROI.
- `scripts/audit_daily_run.py:47` invokes the actual date-bounded engine for 2024-25 calibration / 2025-26 holdout. Both teams need ten current games, and the baseline is historical home-win rate, not a bookmaker. Useful post-ten-game chronological evaluation; insufficient for opening weeks.
- `scripts/validate_repaired_models.py:173` separately evaluates reconstructed cold-start team ratings against bookmaker opening ML quotes: fit on 2023-24/2024-25; test on 2025-26. Saved `data/audits/2026-10-25/followup/2026-10-25T215941.947986_0000/validation.extended.json` has 53 opening-week and 155 first-ten-game holdout games. First-ten Brier: legacy 0.21149; research calibration 0.21783; bookmaker 0.19579 (lower is better). The research replacement did not improve the bookmaker or justify deployment; it also uses reconstructible margin aggregates, not archived production NET_RTG snapshots.
- Player temporal validation is `scripts/validate_repaired_models.py:61`: train through 2023-24, calibrate 2024-25, test 2025-26. Probability/market comparisons and date-cluster uncertainty are at `:145`. Existing results remain inconclusive for most stats and worse for AST; neither code tests nor today's scan change that finding.
- `scripts/report_market_diagnostics.py:20` scores first-saved selected player candidates only, excluding revisions; it splits model/stat/side/early-season/quote quality, reports Brier/log loss/hit rate and hypothetical ROI, and leaves absent results pending. **Seventeen groups is not seventeen independent validation trials or proof of profitability.** It does not validate team forecasts, totals, or the full unselected player board.
- CLV infrastructure: `src/data/odds_store.py:22` persists snapshots; `scripts/capture_closing_odds.py:37` separately labels fetched snapshots closing; `scripts/log_team_results.py:264` compares them with saved pick prices. Database audit found **124 pick_time snapshots, zero closing snapshots**. There is currently no measured CLV from this store. The daily runner does not schedule closing captures.
- Closing capture has no enforced near-tipoff cutoff, provider update freshness or same-book reconciliation; latest_snapshot (`src/data/odds_store.py:72`) simply takes the latest row labeled closing. A label alone does not establish a verified closing line.
- Totals: `scripts/generate_todays_team_picks.py:164` loads mutable ORTG/DRTG/pace; `:181` uses the arithmetic efficiency/pace formula plus postseason factor; `:314` gates sparse current samples. Missing: archived as-of scoring inputs, verified closing lines/prices, exact production-path historical validation, fitted total uncertainty/coverage probabilities, independent calibration and uncertainty-aware economic evaluation.
- Team gaps: exact as-of prior/current aggregates, current roster/availability information, trustworthy market capture/side/price provenance, spread-cover calibration at actual prices, and prospective evidence against bookmaker baselines. The inherited `WIN_PROB_SCALE` at `src/models/team_ratings.py:65` predates the active SRS/prior recipe and remains explicitly unvalidated.

### d. DNS/timeouts, retry/backoff

| Component | Location | Existing handling |
|---|---|---|
| Preflight | `src/data/live_evidence.py:14` | One DNS lookup and one HTTP attempt (HTTP timeout 8s); **no retry/backoff**. The timeout does not bound the separate DNS lookup. |
| Daily entrypoint | `scripts/run_daily.py:16` | Required scoreboard/injury transport failure exits 2 before grade/refresh. No automatic whole-run retry. Auxiliary NBA scoreboard/roster success is not required by live_api_ready (`live_evidence.py:51`). |
| ESPN props | `src/data/espn_props.py:59` | Four attempts, 30s request timeout, sleeps 5/10/20/40s (including after the last failure), then Blocked; event failures raise from the live fetch. |
| ESPN team odds | `src/data/espn_odds.py:144` | Three total attempts by default with 2s delays; exhausted transport returns [] (ambiguous with no games). |
| Bulk game logs | `src/ingestion/game_logs.py:205` | Three attempts, 60s request timeout, exponential delay from NBA_API_DELAY; then raises RuntimeError. |
| Team stats | `src/ingestion/team_stats.py:52` | No explicit application retry loop; run catches exceptions and continues. |
| Secondary injury fetch | `scripts/generate_todays_picks.py:675` | One 8s HTTP attempt; exceptions/non-200 return {}. |

Recommended location for bounded transient preflight retries: `fetch_snapshot`, with every attempt retained in evidence and collect/run_daily still failing before mutations after exhaustion. Separately propagate child exit codes. Persistent DNS/egress restrictions require restoring access; retries cannot establish success in that environment. No retry changes were made because Task 3 is read-only.

### e. Other correctness issues found (not changed)

1. **False successful daily status:** `nba.py:198` ignores grade/scan/stack child return codes and returns None after a refresh failure; `nba.py:382` discards handler return values. Consequently `scripts/run_daily.py:27` can see exit 0 despite partial failure and still run diagnostics. Preflight exit 2 is correct but does not protect later failures.
2. **Team-stat fetch failures can exit 0:** `src/ingestion/team_stats.py:123` catches and continues; `:127` also accepts empty responses, and there is no failure exit at run completion. `scripts/update_data.py:89` relies on subprocess return code, so a printed team-stat failure can look successful. Exact row-completeness checks are also absent after mapped rows are skipped.
3. **Suppressed secondary source errors:** injury fetch at `scripts/generate_todays_picks.py:686`; team odds at `src/data/espn_odds.py:154`; athlete-name lookup at `src/data/espn_props.py:295`; Kalshi at `scripts/find_bets.py:314`. Non-200/transport can become {}, [], or dropped name mappings. Thus Kalshi coverage is not retrospectively reconstructible from zero picks. None clears current approval gates.
4. **False historical current-roster certainty:** `scripts/generate_todays_picks.py:915` calls CommonTeamRoster by season without an as-of date; only its DB fallback is date-bounded. Re-running a past forecast with today's roster/injury fetch can introduce later information even though statistical game windows are date-bounded. Saved raw contexts should be used for historical replay. This session did not backfill picks.
5. **Historical aggregate/totals leakage risks:** inline engine above reads full-season mutable aggregates; `load_team_scoring_stats` (`scripts/generate_todays_team_picks.py:164`) has no timestamp cutoff. The active team rating loader has an explicit current-season cutoff (`src/models/team_ratings.py:326`), unlike the totals loader. Prior-season values are still mutable snapshots rather than original published versions.
6. **Unverified closing observations:** closing labels/latest snapshot do not enforce pre-tipoff, same-book or source update evidence. CLV is unavailable now and would remain questionable without those checks.
7. **Threshold boundary mismatch:** `scripts/generate_todays_team_picks.py:153` labels exactly 3.5 points LEAN, but `:280` selects a spread only when strictly >3.5. A read-only fixture reproduces LEAN with spread_pick_side=null; saved in `read_only_findings.json`. Small impact; no threshold changed.
8. **Timezone/pairing edge cases outside today's root cause:** pairs use raw timestamp-string equality (`src/data/espn_props.py:190`), so equivalent instants written with different offsets will not pair. `scripts/find_bets.py:375` chooses minimum update by string order. Today's data is uniformly Z, so these do not explain its rejections. `scripts/capture_closing_odds.py:39` uses host-local date.today instead of the Pacific slate utility; optional Sleeper date conversion at `scripts/generate_todays_picks.py:778` also uses host-local time. These are portability hazards; no such cause was observed today.
9. **Documented feature approximation:** recent form uses flat 2.5-point home neutralization (`src/models/team_ratings.py:357`), whereas SRS/spread use per-team HCA. This is a modeling/documentation inconsistency to evaluate, not a timestamp defect; math unchanged.

Positive checks: current player windows use shift(1) (`src/features/player_features.py:25`), active appearance inference filters game_date before target (`src/features/population.py:35`), rotation eligibility uses prior min_L10 (`:24`), and current model recipe avoids realized target-game minutes. No new lookahead defect was found in those audited paths. Existing historical/inference perturbation tests remain intact.

## 4. Recommended next fixes, ranked by impact

Effort estimates are engineering estimates, not promises of improved betting returns or time needed to accumulate independent data.

| Rank | Next work | Estimate |
|---|---|---|
| 1 | Propagate nonzero daily child exits; make team-stat failures/empty or incomplete responses fail explicitly; distinguish source failure from empty slate/injury/market responses. | 1–2 days |
| 2 | Resolve quote supply: use new per-provider evidence to establish timestamp field semantics, pagination and actual independent bookmaker availability; verify authoritative over/under labels. Keep freshness fixed. | 1–3 days investigation; 2–5 days per permitted adapter, subject to access |
| 3 | Add bounded preflight retry/backoff with per-attempt evidence, plus explicit retryable/nonretryable failures; retain stop-before-mutation behavior. | 0.5–1 day |
| 4 | Archive as-of rosters, transactions, injury updates and complete contexts; build/test a roster-aware team prior and transferable player-role/minutes candidate against bookmaker baselines. | 1–2 weeks for data/versioning; 2–4 additional weeks for a research model, plus prospective collection |
| 5 | Schedule and verify same-book pre-tipoff closing captures with source timestamps; then measure spread/total CLV and actual-price outcomes. | 2–3 days engineering; weeks of collection |
| 6 | Validate the exact production totals path chronologically with as-of inputs and real prices; fit a research uncertainty/calibration model only on training periods. | 1–2 weeks once data is available |
| 7 | Retire or clearly quarantine legacy leaky backtests; fix raw-string timestamp pairing/minimum selection and host-local date utilities; resolve the exact-3.5 display/selection inconsistency after choosing intended semantics. | 1–2 days |

## 5. Verification commands (one command per line)

Run from the repository. The offline audit uses a new temporary directory each time and writes no production forecasts. The optional live command is separate and will normally refresh/grade/save today's research workflow; it is not required for offline verification and was not run in this session.

```sh
cd /Users/omedakbari/projects/nba-edge-model
./venv/bin/python -m pytest -q tests/test_quote_timestamp_audit.py
./venv/bin/python -m pytest -q
./venv/bin/python scripts/audit_quote_timestamps.py --snapshot data/quote_snapshots/2026-11-06.json --fair-board data/fair/2026-11-06.json --market-picks data/market_picks/2026-11-06.json --output-dir "$(mktemp -d /private/tmp/nba-quote-audit.XXXXXX)/replay"
git diff --check
```

Optional normal live workflow, when network access works:

```sh
./venv/bin/python scripts/run_daily.py
```
