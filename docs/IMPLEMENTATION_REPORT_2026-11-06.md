# Implementation report — November 6, 2026

## 1. Per-workstream changes, commits and tests

The prior uncommitted accepted work was preserved first as baseline commit `5d196c2`, excluding raw data, forecasts and model binaries. Each requested workstream then received its own tested commit. All commits are local; nothing was pushed.

**Approval remains disabled; stakes remain $0.** The 900-second production age gate and model formulas are unchanged. No picks were backfilled. All 154 protected existing data/model/feature/betting artifacts in the prior audit manifest still match their SHA-256 hashes, including the database and original forecasts. November 5 team/player pick files remain absent. Timestamp normalization and the explicitly requested exact-3.5 boundary are the only selection-adjacent corrections.

### Workstream 1 — Pipeline integrity

Commit: `4abaaad`. Full-suite passing count: **128 → 151**, with two skips at every stage. Added **23** test cases in `tests/test_pipeline_integrity.py`.

Files: `nba.py`, `scripts/find_bets.py`, `scripts/generate_todays_picks.py`, `src/data/errors.py`, `src/data/espn_odds.py`, `src/data/espn_props.py`, `src/data/http_archive.py`, `src/data/live_evidence.py`, `src/ingestion/team_stats.py`, `tests/test_pipeline_integrity.py`.

### Workstream 2 — Collection and as-of context

Commit: `dccba4d`. Full-suite passing count: **151 → 166**, with two skips at every stage. Added **15** test cases in `tests/test_collection.py`.

Files: `.gitignore`, `ops/CLOSING_CAPTURE.md`, `scripts/capture_closing_odds.py`, `scripts/find_bets.py`, `scripts/generate_todays_picks.py`, `scripts/install_closing_launchd.py`, `scripts/report_clv.py`, `src/data/closing.py`, `src/data/context_archive.py`, `src/data/espn_odds.py`, `src/data/espn_props.py`, `src/data/live_evidence.py`, `tests/test_collection.py`.

### Workstream 3 — Shadow freshness

Commit: `e698fc1`. Full-suite passing count: **166 → 171**, with two skips at every stage. Added **5** test cases in `tests/test_shadow_freshness.py`.

Files: `config/settings.py`, `scripts/find_bets.py`, `scripts/replay_shadow_freshness.py`, `src/data/espn_props.py`, `src/data/shadow_freshness.py`, `tests/test_shadow_freshness.py`.

### Workstream 4 — Roster continuity

Commit: `d419385`. Full-suite passing count: **171 → 175**, with two skips at every stage. Added **4** test cases in `tests/test_roster_continuity.py`.

Files: `config/settings.py`, `scripts/generate_todays_team_picks.py`, `scripts/report_roster_continuity.py`, `src/data/roster_continuity.py`, `tests/test_roster_continuity.py`.

### Workstream 5 — Small correctness fixes

Commit: `95a54a7`. Full-suite passing count: **175 → 181**, with two skips at every stage. Added **6** test cases in `tests/test_small_correctness.py`.

Files: `scripts/backtest_team_model.py`, `scripts/find_bets.py`, `scripts/generate_todays_team_picks.py`, `src/data/espn_props.py`, `tests/test_small_correctness.py`.

Implemented behavior:

- **WS1:** CLI child failures stop the workflow and preserve nonzero status; handler statuses reach process exit; final executed-step summaries print on success/failure. The baseline already raised from `run()`; that fail-fast behavior was retained. Source failures now raise `SourceError` instead of becoming empty markets/unknown injury feeds/dropped athlete names. Kalshi raw bodies/JSON/HTTP Date and failed requests are archived under `data/kalshi_snapshots/`. Team-stat snapshots must contain exactly the distinct active NBA franchises in the static registry (30 for this date), all required fields, the requested season and finite valid metrics; before a complete league sample exists the collector explicitly fails rather than treating a partial league as complete. Preflight makes at most three attempts with 1s/2s backoff, retains every attempt, and still stops before DB/grade changes after failure. The separate OS DNS lookup remains subject to OS resolver timeouts, while HTTP calls use the configured timeout.
- **WS2:** `data/asof_context/` stores immutable injury and roster payloads by fetch instant. Successful preflight now requests all 30 ESPN rosters; actual NBA roster responses used for player inference are separately archived. Historical `get_roster_player_ids(..., as_of=...)` never queries today's roster; missing as-of evidence raises explicitly. Historical injuries similarly use archives. Closing capture defaults to the Pacific slate and refuses past-date backfills. It records only scheduled games within 10 minutes before tipoff, checks both collection and source-capture time, requires HTTP freshness evidence and the same book as the saved pick/pick-time DB row, and rechecks after slow requests. Team observations also enter the existing `game_odds` closing store. Prop observations retain exact book/stat/athlete/line identity. `report_clv.py` uses first-saved picks, matched same-book captures and separate per-day/cumulative market groups. Spread/total/prop line CLV and same-line price/moneyline probability CLV have separate units; none represents approved profit.
- **WS3:** Shadow-only policy defaults to a feed capture within **5 minutes**, verified HTTP Date/capture clocks, confirmed pregame status, and no provider timestamp more than **60 seconds** into the future. Every positive future offset is separately counted/archived as an anomaly, even if within tolerance. Config: `NBA_SHADOW_CAPTURE_MINUTES`, `NBA_SHADOW_FUTURE_TOLERANCE_SECONDS`. Current-rule selections do not consume shadow outputs. Per-book/stat side and pair counts persist in quote snapshots and scan outputs under `freshness_rules`; console output distinguishes both rules.
- **WS4:** Returning-minutes share equals prior-season regular-season minutes on that team contributed by players in the as-of roster, divided by all recorded prior-season regular-season team minutes. Rosters are selected only at/before cutoff; dates and missing/older observations remain visible. Team outputs include `roster_continuity` and `LOW_CONTINUITY` below 60%, configured by `NBA_ROSTER_CONTINUITY_THRESHOLD`. The flag never changes ratings/probabilities or staking. Names are normalized with ambiguity checks; unavailable continuity stays unknown.
- **WS5:** Exact +/-3.5-point edges now receive matching LEAN labels and selected sides (`>=` / `<=`). Quote pairing keys and oldest-update selection use aware UTC instants rather than timestamp spelling/string order. The old team backtest is explicitly LEGACY/LEAKY in its docstring and prints a warning. Totals inputs have an explicit aware as-of cutoff; later mutable snapshots cannot enter historical forecasts. Existing legacy naive team-stat times retain the documented Pacific interpretation; missing times fail rather than introducing later data.

Regression tests simulate every daily-step failure, source transport/schema/empty/partial failures, preflight exhaustion/recovery, immutable archive reads, no live historical roster requests, closing-window boundaries, same-book matching, freshness failure, CLV signs, ignored revisions, shadow/current separation, future anomalies, continuity cutoff/denominator/flags, unchanged rating outputs, offset-equivalent timestamp pairs, exact spread boundaries and totals leakage prevention.

Final full suite: **181 passed, 2 skipped**, versus 128 passed at session start. A pre-existing joblib CPU-detection warning remains. `git diff --check` passes.

## 2. Shadow-rule replay on the original November 6 snapshot

Input: `data/quote_snapshots/2026-11-06.json`; original scan instant `2026-11-06T18:32:23.203700+00:00`. Output: `data/shadow_freshness/2026-11-06-final.json`. Original inputs were not rewritten.

| Measure | Current production rule | Shadow rule |
|---|---:|---:|
| Eligible quote sides | 24 | 891 |
| Paired source markets | 12 | 443 |
| Shadow pairs in supported stats | — | 301 |
| Future-dated anomalies reported | 617 rejected | 617 flagged; none admitted in this snapshot |
| Approved stake | $0 | $0 |

These are quote/pair counts **before player-history/rotation/model-calibrator eligibility**. The original seven eligible player forecasts are not directly comparable with 443 source markets. No shadow player picks or prospective probabilities were invented, and no fresh executable quote is claimed.

All input quotes were DraftKings. By stat:

| Stat | Current paired markets | Shadow paired markets |
|---|---:|---:|
| AST | 0 | 61 |
| FG3M | 0 | 64 |
| PRA | 2 | 39 |
| PTS | 6 | 60 |
| PTS_AST | 1 | 40 |
| PTS_REB | 2 | 51 |
| REB | 1 | 77 |
| REB_AST | 0 | 51 |

## 3. Returning-minutes continuity — all 30 teams

Live refresh was attempted but failed DNS. Values below come from the latest **archived** observations available before the report cutoff. Eighteen teams have November 6 roster observations; twelve are older. They are not represented as newly fetched or guaranteed current. The stored report also includes exact UTC capture times, source URLs, unmatched names, numerator/denominator and status per team.

| Team | Returning minutes | Flag | Roster observation (UTC date) |
|---|---:|---|---|
| ATL | 71.1% | — | 2026-11-06 |
| BKN | 64.9% | — | 2026-11-04 |
| BOS | 75.4% | — | 2026-11-06 |
| CHA | 60.7% | — | 2026-11-06 |
| CHI | 54.7% | LOW_CONTINUITY | 2026-11-04 |
| CLE | 63.2% | — | 2026-11-06 |
| DAL | 56.4% | LOW_CONTINUITY | 2026-11-06 |
| DEN | 61.1% | — | 2026-11-04 |
| DET | 72.9% | — | 2026-11-04 |
| GSW | 77.2% | — | 2026-11-02 |
| HOU | 84.1% | — | 2026-11-04 |
| IND | 72.2% | — | 2026-11-04 |
| LAC | 49.2% | LOW_CONTINUITY | 2026-11-04 |
| LAL | 39.7% | LOW_CONTINUITY | 2026-11-06 |
| MEM | 52.0% | LOW_CONTINUITY | 2026-11-06 |
| MIA | 61.5% | — | 2026-11-04 |
| MIL | 65.2% | — | 2026-11-06 |
| MIN | 65.8% | — | 2026-11-06 |
| NOP | 84.6% | — | 2026-11-06 |
| NYK | 87.7% | — | 2026-11-04 |
| OKC | 70.7% | — | 2026-11-06 |
| ORL | 90.4% | — | 2026-11-02 |
| PHI | 56.0% | LOW_CONTINUITY | 2026-11-06 |
| PHX | 75.1% | — | 2026-11-04 |
| POR | 70.5% | — | 2026-11-06 |
| SAC | 57.4% | LOW_CONTINUITY | 2026-11-06 |
| SAS | 93.7% | — | 2026-11-06 |
| TOR | 66.3% | — | 2026-11-06 |
| UTA | 63.8% | — | 2026-11-06 |
| WAS | 73.2% | — | 2026-11-06 |

**Philadelphia: 11,138 / 19,881 = 56.0233%, LOW_CONTINUITY.** Its archived roster was fetched **2026-11-06T18:32:02.162896+00:00**. LeBron James and Jaylen Brown are present in that roster; their prior-season minutes on other teams do not count as returning Philadelphia minutes. This confirms the diagnostic flag from the saved observation, not a freshly verified roster or numerical injury/transaction adjustment.

Stored outputs: `data/roster_continuity/2026-11-06.json` records the refresh failure and dated values; its latest revision records the normalized-name offline calculation. The original November 6 team picks were not regenerated to insert flags retrospectively.

## 4. Exact closing-capture scheduling setup

A user LaunchAgent plist was written to:

`/Users/omedakbari/Library/LaunchAgents/com.omedakbari.nba-closing.plist`

It uses absolute interpreter/script paths, the repository working directory, a **60-second StartInterval**, RunAtLoad, and logs under `logs/closing_capture.log` / `logs/closing_capture.error.log`. It polls every day but stores observations only for qualifying game windows, including across Pacific/UTC date changes. The machine must be awake and connected; missed windows are never backfilled.

**Activation was attempted but failed:** `launchctl bootstrap` returned **5 (Input/output error)**. A subsequent `launchctl print` confirmed the service is not loaded. The collector is prepared but **not running automatically** in this environment. Do not use root as a workaround; activate it from the normal logged-in user terminal:

```sh
cd /Users/omedakbari/projects/nba-edge-model
./venv/bin/python scripts/install_closing_launchd.py --activate
launchctl print gui/$(id -u)/com.omedakbari.nba-closing
./venv/bin/python scripts/capture_closing_odds.py --window-minutes 10
tail -n 30 logs/closing_capture.log
tail -n 30 logs/closing_capture.error.log
./venv/bin/python scripts/report_clv.py
```

Inspect the most recent execution's result (distinguishes running successfully from actual observations):

```sh
./venv/bin/python -c "import json; from pathlib import Path; p=max(Path('data/closing_snapshots').glob('*/*.json'),key=lambda p:p.stat().st_mtime_ns); r=json.loads(p.read_text()); print(p, 'ok=',r['ok'], 'observations=',len(r['observations']), 'error=',r.get('error'))"
```

Stop the job if needed:

```sh
launchctl bootout gui/$(id -u)/com.omedakbari.nba-closing
```

The current CLV report shows **zero matched values, 97 unmatched saved selected picks**. The new direct capture attempt failed DNS, exited 1 and archived its failure under `data/closing_snapshots/2026-11-06/`. Existing unverified/merely labeled closing rows are not promoted into the new verified CLV report. A successful process with zero observations still does not mean closing lines were obtained. Collection freshness establishes observed feed/HTTP provenance, not ESPN's undisclosed bookmaker last-change semantics.

## 5. What could not be completed live, and why

- **Fresh live data collection:** ESPN DNS/egress fails in this execution environment. Direct closing capture and the all-team roster refresh failed explicitly. A final read-only preflight also archives all retry attempts. No stale observations were relabeled as fresh.
- **Automatic scheduling activation:** macOS rejected LaunchAgent bootstrap (exit 5). The plist, installer and verification commands are ready, but service registration requires the user's working interactive launchd context.
- **All 30 rosters freshly confirmed today:** only eighteen existing roster snapshots are from November 6; twelve are older. Their stored continuity values are useful dated diagnostics, not verified present-time facts. PHI has same-day evidence.
- **Nonempty CLV history:** no verified captures exist yet; the report honestly returns unmatched coverage. This requires future successful execution near tipoff, not fabricated historical snapshots.
- The source-freshness hypothesis is evaluated only in shadow. Its 443-market coverage does not validate prices, probabilities, future metadata, side mappings, or profitability. Approval remains disabled and no model was retrained/promoted.

## 6. Verification commands (one command per line)

```sh
cd /Users/omedakbari/projects/nba-edge-model
./venv/bin/python -m pytest -q tests/test_pipeline_integrity.py
./venv/bin/python -m pytest -q tests/test_collection.py
./venv/bin/python -m pytest -q tests/test_shadow_freshness.py
./venv/bin/python -m pytest -q tests/test_roster_continuity.py
./venv/bin/python -m pytest -q tests/test_small_correctness.py
./venv/bin/python -m pytest -q
./venv/bin/python scripts/replay_shadow_freshness.py --snapshot data/quote_snapshots/2026-11-06.json --output "$(mktemp -d /private/tmp/nba-shadow.XXXXXX)/replay.json"
./venv/bin/python scripts/report_roster_continuity.py
./venv/bin/python scripts/report_clv.py
git diff --check
git log -6 --oneline
```

Commands that intentionally attempt new live collection, once network access is restored:

```sh
./venv/bin/python scripts/report_roster_continuity.py --refresh
./venv/bin/python scripts/capture_closing_odds.py --window-minutes 10
./venv/bin/python scripts/run_daily.py
```
