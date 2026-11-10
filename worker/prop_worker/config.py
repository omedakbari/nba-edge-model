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

# Honest, generic client name (no personal contact info).
USER_AGENT = "nba-edge-model-worker/0.1 (+https://github.com/omedakbari/nba-edge-model)"
HTTP_TIMEOUT = 20

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES = {"KXNBAPTS": "pts", "KXNBAREB": "reb", "KXNBAAST": "ast",
                 "KXNBA3PT": "fg3m", "KXNBAPRA": "pra"}
NBA_INJURY_PDF = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{date}_{hh}_{mm}{ampm}.pdf"
CDN_NBA_SCOREBOARD = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"


def in_ci() -> bool:
    return os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("CI") == "true"
