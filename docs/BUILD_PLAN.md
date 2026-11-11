# Prop Research App — Build Plan (revised)

Revised 2026-11-10 from `~/Desktop/Prop Research App — Build Plan.pdf`, corrected against
the current repo and live endpoint probes. Where this file and the PDF disagree, this file wins.

## Verdict

One phone-first web app on top of this repo, not a second model. Python jobs pull data and
do the math on a schedule; hosted Postgres (Supabase) stores every line, projection and
result; a small Next.js front end shows the player page at one URL on the iPhone home screen.

Trust comes from a trust layer, not a smarter model:

- **Every number shows its source and age.** Stale lines are greyed out and can't become picks.
- **Conflicts fail loudly.** If the injury report, roster and odds feed disagree, the prop is
  blocked and the app says why.
- **The model grades itself nightly.** A calibration page shows how often its 70% calls hit.

## Corrections vs. the PDF

| PDF said | Reality (2026-11-10) | Change |
|---|---|---|
| Replace "the 65/35 blend and normal curve" | Default picks have been **market-anchored since 2026-10-07** (`src/models/market_blend.py`, params in `data/processed/models/market_blend.json`); `nba bets` already prices Kalshi ladders with the 0.07·P·(1−P) fee | Phase 4 extends the existing blend (adds minutes-first + count distributions as the model component). It does not rebuild it |
| Seed the report card from settled Kalshi history | Kalshi NBA player props launched Oct 2026, so there's **~1 month** of history | Seed from `historical_props` (ESPN/DK book props, 2023-24 onward) and add Kalshi as it builds up |
| PropLine $9/mo for book lines | PropLine's **free tier** (1,000 requests/day, key in `PROPLINE_API_KEY`) returns NBA player props from ~30 books (DK, FD, MGM, Pinnacle, Kalshi, PrizePicks …) and free box-score stats. Verified 2026-11-10 | Use PropLine free for book lines and as the second box-score source. Stay off paid tiers. The ESPN/DK scraper stays as a fallback |
| `nbainjuries` package | User preference: own parser, no convenience wrappers | Own pdfplumber parser for the official PDFs |
| cdn.nba.com "works from anywhere" | **HTTP 403** from this Mac *and* from GitHub-hosted runners (healthcheck run 42912209921) | **ESPN replaces cdn.nba.com** for schedule, box scores, and the secondary injury feed. cdn.nba.com stays only as an informational probe |
| Kalshi host `external-api.kalshi.com` | Repo uses `api.elections.kalshi.com/trade-api/v2` (verified 200, no auth) | Use the repo's host |

Unchanged from the PDF: the phases, the checks that gate each one, the freshness and blocking
rules, research mode until a stat earns probabilities, and stopping at checkpoints so the user
runs git.

## Data sources

| Feed | Source | Refresh | Cross-check / fallback |
|---|---|---|---|
| Schedule | ESPN `site.api.espn.com/.../nba/scoreboard?dates=YYYYMMDD` | Every ingest | — |
| Box scores (game logs) | ESPN `.../nba/summary?event={id}` → `boxscore.players` (MIN, PTS, FG, 3PT, FT, REB, AST, TO, STL, BLK, OREB, DREB, PF, +/-), finals only | Every ingest until all of last night's finals are in | PropLine `/events/{id}/stats` (free) for the 10-player validation |
| History backfill | `data/nba_edge.db` game logs (2019-20 onward; incomplete before 2023-24) | Once, from the Mac | — |
| Injuries (primary) | Official NBA PDFs: `ak-static.cms.nba.com/referee/injury/Injury-Report_YYYY-MM-DD_HH_MM{AM,PM}.pdf`, 15-min slots, parsed with pdfplumber | Every 15 min, 9am–tip | — |
| Injuries (secondary) | ESPN `.../nba/injuries` | Every 15 min | Both stored; disagreements flagged and block the prop |
| Kalshi prices | `api.elections.kalshi.com/trade-api/v2` series KXNBAPTS / REB / AST / 3PT / PRA + NBA game markets | Every ingest on game days | — |
| Book lines | PropLine `api.prop-line.com/v1` (`X-API-Key`), markets player_points / rebounds / assists / threes / points_rebounds_assists. Outcomes carry `player_id: "nba:<NBA.com id>"` | Every ingest on game days, **within 1,000 requests/day** | ESPN/DK scraper (`src/data/espn_props.py`); future timestamps go red |
| Lineups | Injury report + recent rotation from box scores | — | — |

No scheduled job calls stats.nba.com or cdn.nba.com. `nba_api` runs only on the Mac.

**PropLine budget:** 1,000 requests/day on the free tier. The ingest runs 64 times a day
(every 15 min, 9am–1am ET), so it needs about one PropLine call per run, using the bulk odds
endpoint rather than one call per game. The validation and healthcheck use a few more.
Each response carries `x-daily-remaining`, which the job records.

## Architecture

```
sources (ESPN, NBA injury PDFs, Kalshi, PropLine, nba_edge.db backfill)
   → worker/ (Python 3.12, GitHub Actions): ingest → project & price → grade & check
   → Postgres on Supabase (single source of truth; every row: source, provider_ts, fetched_at)
   → web/ (Next.js on Vercel, login-only, reads the DB only) → iPhone home screen
```

Data only flows one way into the app: every number on screen is a database row with a source
and a timestamp. Every table has a `sport` column (default `'nba'`) from day one, so other
leagues can be added later.

## Projection engine (Phase 4)

1. Minutes first: recent role, plus minutes freed by injured teammates, trimmed for blowout risk from the spread.
2. Per-minute rates, shrunk toward season and career rates (the existing HistGradientBoosting models predict rates).
3. Matchup: opponent and pace.
4. Count distributions: negative binomial for REB / AST / 3PM; combos simulated from one shared minutes draw.
5. Market anchor: the **existing** `market_blend`, with weights refit per stat.
6. Output: P(over) for every line and Kalshi threshold, fair price, and edge after fees.

## Trust layer (Phase 5)

1. Every stored row has `source`, `provider_ts` and `fetched_at`; the UI prints the age next to the number.
2. Freshness gates (starting values; tune in config):

| Feed | Green | Amber (shown, not pickable) | Red (hidden) |
|---|---|---|---|
| Kalshi price | < 10 min | 10–30 min | > 30 min |
| Book line | < 15 min | 15–60 min | > 60 min or future timestamp |
| Injury report | latest snapshot < 30 min | 30–90 min | > 90 min on a game day |
| Game logs | last night's finals loaded by 9am ET | — | any final missing |

3. A prop is blocked, with the reason shown, when: the player is Out / Doubtful / Questionable / GTD; the team doesn't match across injury report, roster and odds; projected minutes < 15; the game has started; the line is > 2 pts off the cross-book median; or |model − no-vig market| > 15 pts ("model probably missing information").
4. Jobs fail loudly: a `job_runs` row per run. On a game day a run is red if it loads 0 rows, finishes in < 2 s, or loads < 50% of its trailing average. A red run exits non-zero and sends an ntfy.sh push to `NTFY_TOPIC`; the app shows a banner.
5. Nightly grading: calibration in 5-point buckets per stat, Brier score, CLV against the Kalshi or book closing price.
6. Probabilities are earned per stat: ≥ 200 graded props, and every bucket with ≥ 30 props within 3 pts of its stated rate. Until then the stat stays in research mode (projections and hit rates, no edge, no picks). Approval stays disabled and stakes stay $0 until then.
7. Tests on every commit: parsers run against saved real responses (including a day with future timestamps, an empty game day and an unmatched player), plus a replay test of one stored game day.

## Build phases

Each phase ends at a check you can run without reading code. Don't start a phase until the
previous one passes.

1. **Accounts and skeleton** — `worker/` and `web/` folders, a Supabase project, GitHub Actions secrets.
   *Check:* the `healthcheck` workflow reaches Postgres, ESPN, the injury-PDF host and Kalshi from GitHub Actions.
   **Passed 2026-11-10** (run 42912209921). cdn.nba.com: 403 from the runner. PropLine probe added afterwards: free tier returns props and box-score stats.
2. **Data pipeline** (in progress) — in `worker/`:
   - Schema migration in Supabase: `players, player_xref, teams, games, game_logs, injuries, kalshi_markets, kalshi_quotes, book_quotes, projections, picks, results, job_runs`. Every quote/injury row stores `source, provider_ts, fetched_at`; quotes are stored only when the price changes (500 MB cap).
   - `player_xref`: one internal `player_id` → NBA.com ID (from `nba_edge.db`), ESPN ID, Kalshi name/ticker fragment, PropLine ID. Matched on team + normalized name; unmatched players go to a report and are never silently dropped.
     **Built 2026-11-10** (`worker/prop_worker/xref.py`). PropLine joins directly on `nba:<NBA.com id>`. ESPN and Kalshi match on team + normalized name, then the same team with last name + first initial. The last fallback (`name_unique_stale_team`) links only when the name is unique league-wide and our team for the player is unknown or from before this season: offseason moves and rookies. A player with current-season games for another team is never auto-linked. ESPN rosters keep `players.team_abbr` current. Manual fixes go in `worker/xref_overrides.csv`. Local rehearsal against the live feeds: ESPN 518/520, PropLine 94/94, Kalshi 87/87; the 2 unmatched are real (a player missing from `nba_edge.db`, and a trade in the last few days).
   - Ingest jobs, each with a CLI: `schedule` (ESPN), `boxscores` (ESPN, finals only), `injuries` (official PDF primary + ESPN secondary, both stored, disagreements flagged), `kalshi` (five KXNBA* prop series + NBA game markets), `propline` (book player props).
   - `job_runs` on every job (rows_in, rows_out, duration, status) with the red-run rule and ntfy alerts above.
     **Built 2026-11-10** (`worker/prop_worker/jobs/`, `runner.py`, `ingest.py`, `status.py`). Jobs: `rosters` (once per slate day), `schedule`, `boxscores`, `injuries`, `kalshi`, `propline`. Checks built into the jobs:
     - **boxscores:** a game loads only if each team's player points add up to its final score. Rows for unmatched players wait in `pending_rows` and are re-applied once the player resolves.
     - **injuries:** the official PDF is parsed from word positions, with wrapped reasons and page breaks handled. Disagreements with ESPN go to `injury_flags`; G League assignments are excluded.
     - **kalshi:** a market's line is stored only when its "N+" title matches its strike.
     - **propline:** main lines only, pregame only, from the 9 books.

     Two refinements to the red-run rules, based on a live rehearsal:
     - **Trailing average:** compares rows per game or event, and only against runs at about the same ET time of day (±1 h). Volume grows through the day as teams file reports and books post props.
     - **"< 2 s":** red only when the run is also under 25% of that job's usual duration. The jobs normally take 0.1–2.5 s, so a flat 2 s floor turned every run red. `RED_DURATION_MODE = "absolute"` restores the literal rule.

     **Storage finding:** even with change-only storage, about 1,600 prices change per evening run. That projects to roughly 20–35 MB a day, so pruning of settled quotes is needed before the ingest runs unattended past about two weeks.
   - Backfill script (Mac-only): `nba_edge.db` game logs → Postgres via `player_xref`, with before/after counts that must match and an unmatched-player count.
     **Built 2026-11-11** (`worker/prop_worker/backfill.py`). Everything happens in one transaction, which rolls back if any count check fails, and `--dry-run` is available. A games row is created for each NBA game ID, or the ID is attached to the row ESPN already made. Where `nba_edge.db` and ESPN overlap, the NBA.com row wins, and unmatched players are held in `pending_rows`. Unchanged rows aren't rewritten, and the touched tables are vacuumed afterwards with a 30 s lock timeout so dead rows don't count against the cap. Local rehearsal: 144,116 of 144,116 rows matched, per-season counts equal, 0 unmatched; the re-run wrote nothing. Size: `game_logs` ≈ 49 MB, database ≈ 90 MB after the backfill.
   - The 10-player validation moves to checkpoint 5 with the workflows and the prune job (approved 2026-11-11: keep 2 days of intraday quotes, then opening, closing and any price a pick used).
   - **Checkpoint 5 built 2026-11-11.**
     - **Workflows:**
       - `ingest.yml` runs every 15 min, 9am–1am ET. The UTC crons are set per month (EST months, EDT months, and a superset in March and November), and the worker's `--window` check trims extra slots. A test simulates every 15-minute slot of a full year.
       - `nightly.yml` runs at 6am ET: schedule, boxscores, validate, prune, then the grade placeholder.
       - `healthcheck.yml` stays manual.
       - `tests.yml` runs on every push, with a Postgres service container, so the database tests run in CI.
       - All workflows use Node 24 actions (checkout, setup-python and upload-artifact v7; cache v6) on `ubuntu-24.04`, and the virtualenv is cached.
     - **Validation** (`jobs/validate.py`): 10 seeded-random players' ESPN rows from the last game night, compared with PropLine's free box score. It runs nightly in CI; `--against nba_edge_db` runs it on the Mac instead. Any mismatch is red, including a player missing from the second source. A live rehearsal for Nov 10 matched 10 of 10 against PropLine.
     - **Prune** (`jobs/prune.py`): implements the approved rule, vacuums afterwards, and turns red when the database passes 400 MB.
   - **Long-term quote storage (approved and built 2026-11-11).** After 2 days, each line folds into one `quote_history` row: opening and closing for both sides, keyed by a 16-byte hash. Rows a pick used stay in full, and settled Kalshi market metadata is deleted (migration 005, `jobs/prune.py`).
     - Dry run on real data (`--as-of`): 646 KB → 202 KB per game, 69% smaller. That's with only about 3 snapshots per line; full game days compact more.
     - Projection: roughly 270–330 MB for the rest of the season on top of about 76 MB, under the prune job's 400 MB alarm. Next levers if needed: fewer books long-term, or fewer Kalshi strikes.
   - **Actions minutes: resolved.** The repo went public on 2026-11-11, so standard runners are free; ingest stays every 15 min.
   - **validate fix (2026-11-11):** it checks last night only and never an older date. It compares whichever `game_logs` source holds that night (ESPN, or `nba_edge_db` where the backfill superseded ESPN), and fails loudly, naming the games, if any of last night's finals has no box score. PropLine events now match on `game_date`, because backfilled games have no start time.
   - **Security hardening (2026-11-11):** see `docs/SECURITY.md`.
     - Migration 004: RLS everywhere, no privileges for `anon`/`authenticated` (now or for future tables), `security_invoker` views, fixed `search_path`. `migrate` re-verifies all of it on every run.
     - TLS required on every remote connection.
     - Secrets masked and redacted in every log, summary, alert and artifact.
     - Workflows: SHA-pinned actions, `permissions: {}` by default, no privileged triggers. Regression tests enforce each rule.
   *Check:* three game days in a row with every job green, plus the 10-player check passing.
3. **Player page** — stat tabs, line stepper, hit-rate cards (L5 / L10 / L15 / season / H2H), filters, game-log chart, line chips with age. Installable as a PWA.
   *Check:* loads in < 2 s on the phone, and every number traces to a database row.
4. **Projection engine** (above).
   *Check:* on a 2025-26 backtest, each enabled stat beats the current market_blend and L10 baselines on log loss.
5. **Trust layer and grading** (above).
   *Check:* the replay test passes, and the report card fills from `historical_props` plus the Kalshi history available.
6. **Kalshi ladder, Today board, card builder, bet log.**
   *Check:* two weeks live with CLV tracked, and at least one stat out of research mode.

## Costs and risks

- $0/month: Supabase Free (500 MB; the 15-min ingest keeps it from pausing), GitHub Actions, Vercel Hobby, PropLine Free.
- **500 MB cap:** store a quote only when the price changes, and thin to hourly after settlement.
- **PropLine free quota (1,000/day):** one bulk odds call per ingest. If a night runs out of quota, book lines go amber/red through the freshness gates instead of failing silently.
- **Unofficial endpoints change format:** fixture tests catch it. BALLDONTLIE All-Star ($9.99) is the paid fallback, used only if needed.
- **The ESPN/DK book feed has had bad timestamps** (`docs/QUOTE_TIMESTAMP_AUDIT_2026-11-06.md`): freshness gates hide those quotes rather than trusting them.
- **The model may not beat Kalshi's prices.** That's a normal outcome. Research mode keeps the app useful.
- **Data terms:** keep the deployed app login-only.

## Open decisions

- [x] Public repo (2026-11-11). Hardening in `docs/SECURITY.md`.
- [x] Long-term quote storage: compact `quote_history` (2026-11-11).
- [x] Retire the Streamlit dashboard after Phase 3 (decided 2026-11-10; untouched until then).
