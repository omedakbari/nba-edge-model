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
