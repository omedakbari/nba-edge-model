"""Worker configuration. Everything comes from env vars so GitHub Actions and the Mac share one code path."""

import os
from zoneinfo import ZoneInfo

try:  # .env is optional; Actions injects secrets as env vars
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ET = ZoneInfo("America/New_York")
SPORT = "nba"

SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
PROPLINE_API_KEY = os.getenv("PROPLINE_API_KEY", "")

# Honest, generic client name (no personal contact info).
USER_AGENT = "nba-edge-model-worker/0.1 (+https://github.com/omedakbari/nba-edge-model)"
HTTP_TIMEOUT = 20

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES = {"KXNBAPTS": "pts", "KXNBAREB": "reb", "KXNBAAST": "ast",
                 "KXNBA3PT": "fg3m", "KXNBAPRA": "pra"}
KALSHI_GAME_SERIES = {"KXNBAGAME": "game_winner", "KXNBASPREAD": "spread", "KXNBATOTAL": "total"}
NBA_INJURY_PDF = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{date}_{hh}_{mm}{ampm}.pdf"
CDN_NBA_SCOREBOARD = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"

# PropLine (free tier: 1,000 requests/day; key sent as X-API-Key, never logged).
PROPLINE_API = "https://api.prop-line.com/v1"
PROPLINE_SPORT = "basketball_nba"
# PropLine market key -> our stat key (same keys as KALSHI_SERIES values).
PROPLINE_MARKETS = {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast",
                    "player_threes": "fg3m", "player_points_rebounds_assists": "pra"}
# Only these books' main lines go into book_quotes (500 MB cap). Filtered server-side too, and
# prune deletes stored rows from any other book. Chosen 2026-11-11:
#   draftkings, fanduel, betmgm, pinnacle  priced books for the no-vig consensus market_blend anchors on
#                                          (Pinnacle = sharp reference; DK and MGM are in its training data;
#                                          DK is also where your book singles were placed)
#   prizepicks, sleeper                    pick'em apps you've bet (pick_results line_source); lines only
# Dropped: fanatics, betrivers, hardrock (retail books that copy the same lines) and underdog
# (pick'em you haven't bet). Adding a book back only affects new rows.
BOOK_QUOTE_BOOKS = ("draftkings", "fanduel", "betmgm", "pinnacle", "prizepicks", "sleeper")
PROPLINE_QUOTA_WARN = 100          # warn when fewer requests than this are left today

# Storage (prune job). Kalshi game spread/total ladders keep ONE strike per game and series in
# quote_history: the one closest to 50c at tip-off (that strike is the market's spread / total;
# the projection engine uses the spread for blowout risk). Player-prop strikes are all kept: they
# are what you bet. job_runs rows older than JOB_RUNS_KEEP_DAYS are deleted (status uses 7 days,
# the red-run trailing average the last 20 runs).
KALSHI_LADDER_ONE_STRIKE = ("spread", "total")
JOB_RUNS_KEEP_DAYS = 45
SEASON_GAMES = 1230 + 90           # regular season + play-in and playoffs (typical), for the projection

# Ingest windows
SCHEDULE_DAYS = range(-1, 6)       # yesterday .. 5 days ahead (Kalshi lists game markets ~5 days out)
BOXSCORE_LOOKBACK_DAYS = 3
INJURY_PDF_MAX_AGE_SLOTS = 8       # the NBA posts every 15 min; > 2 h without one is red
SLATE_ROLLOVER_HOURS = 6           # 00:30 ET still belongs to the previous night's slate

# Red-run rules (docs/BUILD_PLAN.md, trust layer item 4)
RED_MIN_DURATION_S = 2.0
# "relative": a run under 2 s is red only if it is also < RED_FAST_FRACTION of the job's trailing
#             median duration (these jobs normally take 0.1-2.5 s, so a flat 2 s floor is always red).
# "absolute": any game-day run under 2 s is red (the rule exactly as first written).
RED_DURATION_MODE = "relative"
RED_FAST_FRACTION = 0.25
RED_TRAILING_FRACTION = 0.5
TRAILING_WINDOW = 20               # last N green, expected, game-day runs of the same job
TRAILING_MIN_RUNS = 5              # don't judge against fewer than this
FUTURE_TS_TOLERANCE_S = 120        # provider_ts this far past fetched_at counts as "future"


def in_ci() -> bool:
    return os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("CI") == "true"
