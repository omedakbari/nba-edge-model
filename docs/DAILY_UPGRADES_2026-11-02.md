# Daily workflow upgrades — November 2, 2026

The daily market scanner now uses the existing `appearance-v2` research bundle with matching all-appearance inference features. This changes diagnostic predictions, not approval: all research probabilities remain unvalidated and all approved stakes remain zero. Original forecast files and original trained models are preserved.

## Changes

- Live sportsbook anchors reject missing, naive, future, or older-than-15-minute provider timestamps before consensus calculation. Reused boards are checked again before automatic or manual pricing. Every constituent pair is checked, so one valid timestamp cannot hide another invalid one. No timezone offset or freshness is invented. Raw source items, excluded-side reasons, HTTP Date/Age/Last-Modified and response hashes support investigation of upstream timestamp errors.
- Live source transport failures raise errors rather than masquerading as an empty market. Scheduled events also need a future start time.
- Team output prints both market handicaps, such as `CHA +5.5 / DET -5.5`. PASS comparisons use home handicaps on both sides of the comparison.
- `--update` fetches league-wide player game logs for a bounded window, normally seven days before the latest stored game through today. Existing box scores are corrected with upserts. New, corrected and unchanged rows are counted separately. Per-player ingestion remains available for initial backfills. `--update --season` now respects the supplied season.
- The normal feature export is `data/processed/features_appearance_v2.parquet`, built from every positive-minute appearance and complete bench-inclusive team aggregates. Eligibility uses past minutes, never the target game's realized minutes. The older `features.parquet` and original models remain separate; explicit legacy pick'em tools still use that compatibility path.
- Daily research inference uses the existing temporally trained four component models, minutes expectation and short-minutes risk model. It exposes expected minutes and short-minutes risk in saved candidates. Only supported calibrators are used: the current bundle supports PTS, REB, AST, FG3M and PRA. REB+AST and other unsupported combinations are omitted. Alternate-line probabilities and postseason probabilities are not extrapolated from this regular-season bundle.
- The bundle location is explicitly configured in `config/settings.py`; `NBA_APPEARANCE_RESEARCH_BUNDLE` can point to another trusted local appearance-v2 research artifact. A missing bundle is an error, never a silent fallback to older models.
- Candidates show absent teammates and matched joint-absence evidence counts. They receive no fabricated role adjustment. The board continues to attach conditional role context and precise evidence limitations.
- Single-book anchors are explicitly labeled. Duplicate versions of one bookmaker cannot count as multiple independent books. Additional independently obtained quote pairs can be imported using the adapter described below.
- Candidate side counts and quote exclusions are archived under `data/market_diagnostics`. A separate report scores first-saved candidates by model version, stat, side, first-ten-game/later cohort, and quote quality. It reports Brier score, log loss, hit rate and hypothetical saved-price ROI. Invalid quote cohorts have no ROI estimate. Missing appearances remain pending; nonparticipation and pushes are excluded from binary calibration. Revisions are excluded from this first-forecast evaluation.
- Live reuse and stack screens read the latest revision while grading/research retain the original. Stack screens exclude stale player anchors and unapproved game quotes.

## Running

The normal entrypoint is unchanged:

```sh
./venv/bin/python scripts/run_daily.py
```

To reconcile corrections older than the normal overlap:

```sh
./venv/bin/python src/ingestion/game_logs.py --update --full-season --season 2026-27
```

To inspect side/stat/cohort diagnostics without network access:

```sh
./venv/bin/python scripts/report_market_diagnostics.py
```

To explicitly regenerate compatibility features for older pick'em tools:

```sh
./venv/bin/python -m src.features.pipeline --legacy
```

## Additional bookmaker input

No second bookmaker subscription or feed is created automatically. Use an independently obtained JSON list with the following paired-quote schema. Identities, start time, and line must match the existing ESPN anchor; timestamps must be timezone-aware and fresh. These example times illustrate the schema and must be replaced with actual contemporaneous observations.

```json
[
  {
    "player": "Exact player name from the ESPN board",
    "stat": "pts",
    "book": "Independent sportsbook name",
    "line": 24.5,
    "over": 1.91,
    "under": 1.91,
    "event_id": "ESPN event ID",
    "teams": ["ORL", "WAS"],
    "start_time": "2026-11-03T00:30Z",
    "captured_at": "2026-11-02T20:12:00+00:00",
    "updated_at": "2026-11-02T20:10:00+00:00",
    "source_url": "https://example.com/actual-source-quote",
    "side_mapping_verified": false
  }
]
```

Pass `--book-quotes /path/quotes.json` directly to `scripts/find_bets.py`. For the normal daily command, set `NBA_SUPPLEMENTAL_QUOTES=/path/{date}.json`. Only set side mapping verified when actual source labels establish the sides. Importing additional books does not clear probability, role, settlement, or other approval gates.

## Remaining empirical requirements

Code cannot establish profitable probabilities or transferable injury effects without suitable independent data. The prior temporal evaluation remains inconclusive, and AST/team results have material weaknesses. Broader timestamped injury/control/joint-absence evidence, authoritative quote sides, actual multi-book observations and prospective calibration are still required. This update neither retrains against today's lines nor presents the old holdout as a new independent test.

Verification: corrected feature export built successfully (136,409 usable rows); an offline replay of saved November 2 inputs produced 542 supported research props. That replay wrote only a temporary directory and is not a fresh executable quote or live recommendation. Network-dependent ingestion is covered with isolated database/endpoint fixtures; no live production ingestion was used for verification.
