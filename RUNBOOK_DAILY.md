# NBA Edge Model — Daily Operations Runbook

**Repo:** `~/projects/nba-edge-model`
**DB (local only):** `data/nba_edge.db`
**Code:** saved in git / GitHub — push after any changes

---

## Quick start (any game day)

```bash
cd ~/projects/nba-edge-model
source venv/bin/activate
```

---

## Play-In workflow

### Pregame

```bash
# 1. Refresh data
python scripts/update_data.py

# 2. Player props
python scripts/generate_todays_picks.py --play-in --season 2025-26 --save

# 3. Team spread / ML picks
python scripts/generate_todays_team_picks.py --play-in --season 2025-26 --save
```

### Next morning (grade)

```bash
python scripts/update_data.py
python scripts/log_pick_results.py --date YYYY-MM-DD
python scripts/log_team_results.py --date YYYY-MM-DD
```

---

## Playoffs workflow

### Pregame — Game 1 of a series

```bash
# 1. Refresh data (pulls latest game logs + team stats including playoff games)
python scripts/update_data.py --playoffs

# 2. Player props
python scripts/generate_todays_picks.py --playoffs --season 2025-26 --save

# 3. Team spread / ML picks
python scripts/generate_todays_team_picks.py --playoffs --season 2025-26 --save
```

### Pregame — Game 2+ of a series

Add `--g1-winner` to the team picks command to apply series momentum (±1.5 pts):

```bash
python scripts/generate_todays_team_picks.py --playoffs --season 2025-26 --save \
  --g1-winner HOME_ABBR:WINNER_ABBR[,HOME_ABBR:WINNER_ABBR...]
```

**Format:** `HOME_TEAM:WHO_WON_G1`

| Situation | Flag |
|-----------|------|
| Boston hosted G1, Boston won | `BOS:BOS` |
| Boston hosted G1, Milwaukee won | `BOS:MIL` |
| Two series on same day | `BOS:BOS,DEN:DEN` |

Example:
```bash
python scripts/generate_todays_team_picks.py --playoffs --season 2025-26 --save \
  --g1-winner BOS:BOS,MIL:IND
```

### Next morning (grade)

```bash
python scripts/update_data.py --playoffs
python scripts/log_pick_results.py --date YYYY-MM-DD
python scripts/log_team_results.py --date YYYY-MM-DD
```

### Weekly performance review

```bash
python scripts/log_pick_results.py --report
python scripts/log_team_results.py --report
```

---

## What to review after generating picks

### Player props output

Check the footer — in playoff mode it must show:

```
TIER GATES  STAR >= 55%  |  CORE >= 55%  |  ROTATION >= 65%
STAT GATES  STAR PTS >= 70%
```

If those lines are missing, you are not in playoff mode. Re-run with `--playoffs`.

Look for:
- `⚠️` flags = Day-To-Day or Doubtful — verify lineup before betting
- `hist✓` = historical playoff hit rate confirms the direction
- `pred` % = how far the model prediction is from the PrizePicks line (larger gap = stronger signal)
- ROTATION tier at ≥65% is gated; anything below that threshold is filtered out automatically

### Team picks output

Each game will be classified as:

| Tier | Spread gap | Action |
|------|-----------|--------|
| STRONG | ≥ 5.5 pts | Bet |
| LEAN | 3.5 – 5.4 pts | Bet (smaller) |
| MONITOR | 2.0 – 3.4 pts | Watch, do not bet |
| NONE | < 2.0 pts | Pass |

The spread edge is `model_spread + market_spread`.
Positive = model more bullish on home than market → take home ATS.
Negative = model more bullish on away → take away ATS.

**Always check injury news before acting on any team pick.** The team model does not ingest live injury data.

---

## Canonical baseline artifacts

| File | Description |
|------|-------------|
| `PLAYOFF_BACKTEST_REPORT_4season.md` | Full 4-season playoff props backtest (2021–25) |
| `data/processed/playoff_backtest_4season.csv` | Raw results for the 4-season report |
| `data/processed/playoff_backtest.csv` | Most recent single-season run |
| `data/team_picks/results_log.csv` | Running spread/ML grading log |
| `data/picks/` | Daily props picks JSON files |
| `data/team_picks/` | Daily team picks JSON files |

Baseline commit: `e0ef35d` — frozen playoff production baseline.

---

## Known limitations

**Player props model:**
- Uses L10 rolling averages and playoff elevation ratios fit on 2021-22 through 2024-25.
- Does not model matchup-specific defenders in real time (e.g. a lockdown PG on the opposing team is not in the signal).
- Elevation ratios can lag if a player's role changed significantly this season.
- Props lines move after the script runs. Check the current line before placing.

**Team spread / ML model:**
- 68.2% directional accuracy across 4,604 games (4-season backtest). ~1 in 3 picks is wrong direction.
- Spread MAE = 11.92 pts — model is useful for direction, not exact line precision.
- **No live injury input.** A star player being out does not change the team rating. Manual sanity check required on every game before betting.
- Compared against the line at run time, not the closing line.
- Playoff-specific behavior (rotation tightening, pace changes) is approximated via HCA and series momentum adjustments, not learned from current-season playoff data until enough playoff games accumulate.

**Database:**
- `data/nba_edge.db` is local only. Not in git. Back it up separately if needed.
- The DB must be refreshed daily via `update_data.py` — the model is only as current as the last ingest.

---

## When to pause and investigate

**Player props — warning thresholds:**

| Situation | Action |
|-----------|--------|
| Rolling live hit rate drops materially below backtest baseline | Investigate — check if lines moved or player roles shifted |
| Live hit rate at P≥55% drops below ~54% over meaningful sample (30+ picks) | Investigation warning |
| Live hit rate below ~48% over large sample (50+ picks) | Hard warning — pause, audit |
| STAR PTS at ≥70% gate underperforming badly | Gate may not be holding — review ablation logs |

**Team picks — warning thresholds:**

| Situation | Action |
|-----------|--------|
| STRONG tier directional accuracy below 55% over 20+ games | Investigate team ratings freshness |
| Systematic bias (model always high or always low on home) | Check mean_err in backtest output |
| Lines consistently moved against picks by >3 pts at close | Market may be ahead of model — reduce bet size |

---

## Data flow summary

```
NBA API ──► game_logs (DB)  ──► TeamRatingEngine ──► model spread / win%
         ──► team_stats (DB) ─┘
ESPN API ──► live DK lines  ──► edge calculation ──► STRONG / LEAN / MONITOR

PrizePicks API ──► lines
NBA API        ──► player game logs
ESPN API       ──► injury report
                    └──► Props model ──► SAFE Flex card picks
```

---

## File index

```
scripts/
  update_data.py                  — daily data refresh (run first)
  generate_todays_picks.py        — player props picks
  generate_todays_team_picks.py   — spread / ML picks
  log_pick_results.py             — grade props after games
  log_team_results.py             — grade spread/ML after games
  backtest_team_model.py          — re-run team model backtest
  run_playoff_backtest.py         — re-run props playoff backtest

src/
  models/team_ratings.py          — TeamRatingEngine
  data/espn_odds.py               — ESPN odds fetcher
  playoffs/adjustments.py         — playoff elevation ratios + gates
  ingestion/game_logs.py          — player game log ingestion
  ingestion/team_stats.py         — team NET_RTG / pace ingestion

data/
  nba_edge.db                     — SQLite DB (local only, not in git)
  team_picks/                     — daily JSON picks + results_log.csv
  picks/                          — daily props JSON picks
  processed/                      — backtest CSVs and reports
```
