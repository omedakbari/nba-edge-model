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
| PropLine $9/mo for book lines; `nbainjuries` package | User preference: free, self-built pipelines; no paid vendors or convenience wrappers | Odds: Kalshi public API + existing ESPN/DK scraper (`src/data/espn_props.py`) behind freshness gates. Injuries: own parser for the official PDFs. **$0/month** |
| cdn.nba.com "works from anywhere" | Plain requests from this Mac get **HTTP 403** (schedule and scoreboard) | Schedule and box scores come from ESPN (`scoreboard` / `summary`, both verified). cdn.nba.com is probed only for information |
| Kalshi host `external-api.kalshi.com` | Repo uses `api.elections.kalshi.com/trade-api/v2` (verified 200, no auth) | Use the repo's host |

Unchanged from the PDF: the phases, the checks that gate each one, the freshness and blocking
rules, research mode until a stat earns probabilities, and stopping at checkpoints so the user
runs git.

## Data sources (all free)

| Feed | Source | Refresh | Cross-check / fallback |
|---|---|---|---|
| Schedule | ESPN `site.api.espn.com/.../nba/scoreboard?dates=YYYYMMDD` | Daily + every ingest | — |
| Box scores (game logs) | ESPN `.../nba/summary?event={id}` → `boxscore.players` (MIN, PTS, FG, 3PT, FT, REB, AST, TO, STL, BLK, OREB, DREB, PF, +/-) | After each final | Mac-only `nba_api` backfill |
| History backfill | `data/nba_edge.db` game logs (2019-20 onward; incomplete before 2023-24) | Once, from the Mac | — |
| Injuries (primary) | Official NBA PDFs: `ak-static.cms.nba.com/referee/injury/Injury-Report_YYYY-MM-DD_HH_MM{AM,PM}.pdf`, 15-min slots, parsed with pdfplumber | Every 15 min, 9am–tip | — |
| Injuries (cross-check) | ESPN `.../nba/injuries` (already archived by the repo) | Every 15 min | A disagreement with the official report blocks the prop |
| Kalshi prices | `api.elections.kalshi.com/trade-api/v2` series KXNBAPTS / REB / AST / 3PT / PRA | Every 5 min on game days | — |
| Book lines | Existing ESPN/DK scraper with provider timestamps; future timestamps go red | Every 15 min | — |
| Lineups | Injury report + recent rotation from box scores | — | — |

The scheduled jobs never call stats.nba.com.

## Architecture

```
sources (ESPN, NBA injury PDFs, Kalshi, nba_edge.db backfill)
   → worker/ (Python 3.12, GitHub Actions): ingest → project & price → grade & check
   → Postgres on Supabase (single source of truth; every row: source, provider_ts, fetched_at)
   → web/ (Next.js on Vercel, login-only, reads the DB only) → iPhone home screen
```

Data only flows one way into the app: every number on screen is a database row with a source
and a timestamp. Every table has a `sport` column from day one, so other leagues can be added later.

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
4. Jobs fail loudly: a `job_runs` row per run. On a game day a run is red if it loads 0 rows, finishes in < 2 s, or loads < 50% of the usual row count. A red run shows a banner in the app and sends an ntfy.sh push.
5. Nightly grading: calibration in 5-point buckets per stat, Brier score, CLV against the Kalshi or book closing price.
6. Probabilities are earned per stat: ≥ 200 graded props, and every bucket with ≥ 30 props within 3 pts of its stated rate. Until then the stat stays in research mode (projections and hit rates, no edge, no picks). Approval stays disabled and stakes stay $0 until then.
7. Tests on every commit: parsers run against saved real responses (including a day with future timestamps), plus a replay test of one stored game day.

## Build phases

Each phase ends at a check you can run without reading code. Don't start a phase until the
previous one passes.

1. **Accounts and skeleton** — `worker/` and `web/` folders, a Supabase project, GitHub Actions secrets.
   *Check:* the `healthcheck` workflow, run from GitHub Actions, reaches Postgres, ESPN, the injury-PDF host and Kalshi. It also reports whether cdn.nba.com answers from cloud IPs.
2. **Data pipeline** — schema, ingest jobs (schedule, boxscores, injuries_official, injuries_espn, kalshi, book_lines), `job_runs`, freshness stamps, backfill from `nba_edge.db`.
   *Check:* three game days in a row with every job green, plus an automated check that 10 random players' game logs match the box score.
3. **Player page** — stat tabs, line stepper, hit-rate cards (L5 / L10 / L15 / season / H2H), filters, game-log chart, line chips with age. Installable as a PWA.
   *Check:* loads in < 2 s on the phone, and every number traces to a database row.
4. **Projection engine** (above).
   *Check:* on a 2025-26 backtest, each enabled stat beats the current market_blend and L10 baselines on log loss.
5. **Trust layer and grading** (above).
   *Check:* the replay test passes, and the report card fills from `historical_props` plus the Kalshi history available.
6. **Kalshi ladder, Today board, card builder, bet log.**
   *Check:* two weeks live with CLV tracked, and at least one stat out of research mode.

## Costs and risks

- $0/month: Supabase Free (500 MB; the 15-min ingest keeps it from pausing), GitHub Actions, Vercel Hobby.
- **500 MB cap:** store a quote only when the price changes, and thin to hourly after settlement.
- **Unofficial endpoints change format:** fixture tests catch it. BALLDONTLIE All-Star ($9.99) is the paid fallback, used only if needed.
- **The ESPN/DK book feed has had bad timestamps** (`docs/QUOTE_TIMESTAMP_AUDIT_2026-11-06.md`): freshness gates hide those quotes rather than trusting them.
- **The model may not beat Kalshi's prices.** That's a normal outcome. Research mode keeps the app useful.
- **Data terms:** keep the deployed app login-only.

## Open decisions

- [ ] Public or private repo (public = free Actions minutes and a portfolio piece; the app stays login-only either way).
- [ ] Retire the Streamlit dashboard once the player page ships (recommended).
