# Decisions log

Design choices made while fixing the 2026-11-24 audit (AUDIT_2026-11-24.md). Newest phase last.

## Phase 1: data integrity (2026-11-24)

**D1. DK-via-ESPN parser stays strict.** All 506 of today's `updated_at` values are `YYYY-MM-DDTHH:MMZ` and
parse correctly. 406 are genuinely hours old and 100 are up to 169 min in the future at scattered offsets.
Naive strings and epoch numbers stay rejected, never reinterpreted. The window and the allowed future skew
are now config values (`config/settings.py`: `QUOTE_MAX_AGE_SECONDS` = 900, `QUOTE_MAX_FUTURE_SKEW_SECONDS` = 0;
env overrides `NBA_QUOTE_MAX_AGE_SECONDS`, `NBA_QUOTE_MAX_FUTURE_SKEW_SECONDS`). I didn't loosen either:
loosening would let 10-hour-old prices into "fair" probabilities. Each rejected side records
`detail` ("stale: age 957.2 min" / "future: skew +123.8 min").

**D2. WARN is a status, not an exit code, inside a run.** A step that warns still exits 0 so the remaining
steps run. It appends a JSON line to `$NBA_WARN_FILE` (`src/utils/step_status.py`; same protocol in
`worker/prop_worker/util.py`). `nba.py run()` reads that file per step and records WARN. `run_daily.py`
collects every warning and exits **3** if anything warned (0 = clean, other = failure). The dashboard treats 3 as a
yellow banner, not a red error.

**D3. What counts as "input > 0, output 0".**
- DK via ESPN: sides offered > 0 and 0 passed the timestamp check.
- Fair board: book prop lines > 0 and 0 eligible props.
- Grading: saved picks > 0 and 0 graded; no picks file for yesterday (daily not run, or no games); team picks
  but 0 final scores.
- Card: games > 0 and 0 props in Supabase; props > 0 and 0 legs; injury data STALE; any team mismatch.
- Worker job red in `job_runs`.
Not warned: an injury-report name that didn't map to a player. Today's one is a two-way G League player with no
props, and a daily yellow nobody acts on would train ignoring yellow. These names are listed in the card's
Excluded section instead.

**D4. Injury status: the stricter source wins, unknown fails closed.** `worker/prop_worker/injury_status.py`.
Order: OUT > D > Q > GTD (incl. Day-To-Day/DTD) > UNK > P > AVL. Excluded: OUT, D, Q, GTD, UNK. On a
tie, the official report is shown as the source. Both sources' statuses and timestamps are kept on each leg.
This reverses the previous "official always wins" rule; the test that encoded it was replaced
(`test_stricter_status_wins_across_official_and_espn`).

**D5. Unmapped injury names are matched by normalized name + team** (diacritics, Jr/II/III, punctuation,
case via `names.normalize_name`) before giving up. What stays unmatched is listed, never silently ignored.

**D6. Injury staleness:** the card is STALE when the newest injury row (official or ESPN) is older than
`INJURY_MAX_AGE_H` (worker config, default 3 h, env `NBA_INJURY_MAX_AGE_H`), checked only for today's live
slate. Both feed timestamps are stored in the card JSON and shown on the Props tab.

**D7. Team mismatch: the roster ingest is authoritative.** If `players.team_abbr` is one of the game's two
teams, use it. If the roster has no team, fall back to the newest game log only when that team is in the game
(flagged). Anything else is excluded as "team mismatch" with roster team, last game-log team and game.
Previously a disagreeing roster team was silently replaced by the game-log team.

**D8. Worker health reads `job_runs` read-only** (`src/data/worker_health.py`), newest row per job in 3 days.
A green job with warnings is shown yellow. Jobs older than their expected cadence are yellow ("no run for N h").
Red reasons are shown verbatim so validate names the player. The dashboard caches it for 5 min.
Supabase unreachable → "unknown" (yellow), never a crash.

## Between phases (2026-11-24)

**D8a. Validate: one-source DNP is a match.** Minutes missing in one source and under 1.0 in the other
(AJ Green, 11-23: ESPN None vs PropLine 0.15) count as a DNP match (`validate.dnp_match`, `DNP_MINUTES = 1.0`).
This applies both ways round. Both sources missing is still a mismatch.

**D8b. `run_daily.py --dry-run`.** No ledger or Supabase writes (card without `--log`). Today's card, brief and
picks files are untouched: the card goes to `data/runs/dry-<stamp>/`, boards run without `--save`, the brief is
skipped and grading runs with `--no-save` (rolled back). The data refresh is skipped. Append-only archives
(live and quote snapshots, fair-board and stack revisions) are still written. The Today page can launch it
(`start_run(dry_run=True)`) and labels it as a dry run.

**D8c. Unmapped injury name (Zack Austin, CLE)** is not a normalization miss: no such player is in `players`.
Two-way players aren't in the roster ingest (same for Jonas Aidoo and Rafael Castro).

## Phase 2: one blend config and calibration (2026-11-24)

**D9. One blend config: `config/model.yaml`** (loader `config/model_config.py`; the worker reads the same file).
It holds `props_model.l10_weight` 0.40, `card.hist_weight` 0.35 (+ `no_market_shrink` 0.5),
`team_ratings.season_weight` 0.65 and `prop_prior.signal_weight` 0.5. Values are unchanged; only their home
moved. Fitted artifacts (`market_blend.json`, `card_calibration.json`) are deliberately not in it.
`tests/test_model_config.py` fails on any new hard-coded blend ratio. Research scripts are allowlisted by name.

**D10. Card calibration is fit on what the card selects on, not on p alone.** The replay
(`worker/prop_worker/replay.py`, no lookahead) shows raw p is calibrated over all 20,152 strikes. But in the
legs the card's rules pick, raw p was .756 against a .670 hit rate. Within each edge bucket the hit rate
tracks the market mid, not the card's p. Isotonic regression on p alone (the brief's default) fixed the
average but not the slice that matters: legs passing the card's rules after isotonic calibration hit 71% at an
80¢ average price. So Kalshi uses **`logit2`**: logistic regression on logit(market mid for the side) and
dev = (raw p − mid)/W_HIST (history − market), fit only on the **card region** (sides with raw p > ask).
On held-out 11-18 → 11-23 data, for legs the raw rules selected, Brier went .2323 → .2238 (isotonic .2325,
market mid .2241) and mean p went .756 → .635 against .670 actual. Isotonic stays in the report for comparison.

**D11. One pooled Kalshi map.** Per-stat maps (≈1-2k fit rows each) had unstable coefficients (PRA b_mid .35,
b_dev 3.3) and were no better on the test split. Per-stat maps kick in at ≥ 5,000 fit rows (`MIN_FIT`).

**D12. Time split with only 13 slates of history.** Kalshi player-prop quotes exist from 2026-11-11, so a
2-week holdout would leave nothing to fit. Fit on 11-11 → 11-17, test on 11-18 → 11-23. The shipped map
is refit on all dates (n and dates are in the artifact). Opening quotes (pruned days) are used only when first
seen before pick time (first tip − 60 min), and never the closing price. Intraday quotes must be the newest within 45 min before
pick time. Rows are correlated (many strikes per player-game; 1,244 player-games), so the effective n is
far below 20k.

**D13. Sleeper: shrunk ledger offset, no replay yet.** n=75 graded legs, hit .640 vs p .648. Offset
−0.0078, shrunk by n/(n+100) to −0.0033. The ledger joins the Kalshi fit once n ≥ 300 per bucket family.
The ledger keeps `p_at_placement` = **calibrated** p (what the rules used). Raw p and the side's mid go into
`note` (`p_raw=…, mid=…`) so the ledger can still be scored raw, with no schema change. Optional schema
change for a proper column: `sql/proposed_2026-11-24_bets_p_raw.sql` (not applied).

**Consequence (2026-11-24 slate):** the calibrated card shows 0 Kalshi legs where the raw card showed 8.
On the test split, the raw card's selected Kalshi legs lost 3.6¢ per contract after fees. The hit-rate history
adds no information over the Kalshi price in this sample.
