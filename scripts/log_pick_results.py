"""
Pick result logger and calibration reporter.

Reads a saved picks JSON, looks up actual box-score stats (from the local DB
after a daily update, or falls back to the NBA API), grades each pick as
hit / miss / push / void, and writes to the pick_results table in SQLite.

Usage:
    # Grade picks after games finish (requires --update to have been run first)
    python scripts/log_pick_results.py --date 2026-04-02

    # Force re-grade even if already graded
    python scripts/log_pick_results.py --date 2026-04-02 --overwrite

    # Show calibration report across all graded dates
    python scripts/log_pick_results.py --report

    # Report for a specific date range
    python scripts/log_pick_results.py --report --from 2026-03-01 --to 2026-04-30
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import DB_PATH

PICKS_DIR = Path(__file__).parent.parent / "data" / "picks"

# Maps stat_key (from JSON) to the column name in game_logs.
# pra and reb_ast are SQLite generated columns so they're directly queryable.
STAT_COLUMN = {
    'pts': 'pts', 'reb': 'reb', 'ast': 'ast',
    'fg3m': 'fg3m', 'pra': 'pra', 'reb_ast': 'reb_ast',
}


# ── DB setup ───────────────────────────────────────────────────────────────────

def init_results_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pick_results (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            pick_date    TEXT NOT NULL,
            graded_at    TEXT NOT NULL DEFAULT (datetime('now')),
            player_id    INTEGER NOT NULL,
            player_name  TEXT NOT NULL,
            team         TEXT NOT NULL,
            opp          TEXT NOT NULL,
            stat         TEXT NOT NULL,
            stat_key     TEXT NOT NULL,
            direction    TEXT NOT NULL,
            line         REAL NOT NULL,
            model_pred   REAL NOT NULL,
            prob         REAL NOT NULL,
            prob_bucket  TEXT NOT NULL,
            card         TEXT NOT NULL,
            n_legs       INTEGER NOT NULL,
            line_source  TEXT NOT NULL,
            injury_flag  TEXT NOT NULL DEFAULT '',
            actual_value REAL,
            result       TEXT,
            hit          INTEGER,
            UNIQUE(pick_date, player_id, stat_key, direction)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pick_results_date
            ON pick_results(pick_date)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pick_results_stat
            ON pick_results(stat_key)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pick_results_bucket
            ON pick_results(prob_bucket)
    """)
    conn.commit()


# ── Actual stat lookup ─────────────────────────────────────────────────────────

def fetch_actuals_from_db(conn: sqlite3.Connection,
                          player_ids: list[int],
                          game_date: str,
                          season_type: str = 'Regular Season') -> dict[int, dict]:
    """
    Pull actual box-score stats for a list of players on a given date.
    Returns {player_id: {stat_key: value, 'min': value, 'played': bool}}.
    """
    placeholders = ','.join('?' * len(player_ids))
    rows = conn.execute(f"""
        SELECT player_id, min, pts, reb, ast, fg3m, pra, reb_ast
        FROM game_logs
        WHERE player_id IN ({placeholders})
          AND game_date = ?
          AND season_type = ?
    """, player_ids + [game_date, season_type]).fetchall()

    result = {}
    for row in rows:
        pid, minutes, pts, reb, ast, fg3m, pra, reb_ast = row
        result[pid] = {
            'min': minutes,
            'played': minutes is not None and minutes > 0,
            'pts': pts, 'reb': reb, 'ast': ast,
            'fg3m': fg3m, 'pra': pra, 'reb_ast': reb_ast,
        }
    return result


def fetch_actuals_from_api(player_ids: list[int],
                            game_date: str,
                            season: str,
                            season_type: str = 'Regular Season') -> dict[int, dict]:
    """
    Fallback: pull actual stats via NBA API if the DB hasn't been updated yet.
    Slow — one request per player.
    """
    from nba_api.stats.endpoints import playergamelog
    from config.settings import NBA_API_DELAY
    from datetime import datetime

    # PlayerGameLog without explicit date bounds silently drops the most recent
    # games from its result set.  Pinning to the exact game date fixes that.
    api_date = datetime.strptime(game_date, '%Y-%m-%d').strftime('%m/%d/%Y')

    result = {}
    for pid in player_ids:
        time.sleep(NBA_API_DELAY)
        try:
            log = playergamelog.PlayerGameLog(
                player_id=pid, season=season,
                season_type_all_star=season_type,
                date_from_nullable=api_date,
                date_to_nullable=api_date,
            )
            df = log.get_data_frames()[0]
            if df.empty:
                continue
            r = df.iloc[0]
            minutes = float(r.get('MIN', 0) or 0)
            pts  = int(r.get('PTS', 0))
            reb  = int(r.get('REB', 0))
            ast  = int(r.get('AST', 0))
            fg3m = int(r.get('FG3M', 0))
            result[pid] = {
                'min': minutes, 'played': minutes > 0,
                'pts': pts, 'reb': reb, 'ast': ast, 'fg3m': fg3m,
                'pra': pts + reb + ast, 'reb_ast': reb + ast,
            }
        except Exception as e:
            print(f"  Warning: API error for player {pid}: {e}")
    return result


# ── Grading ────────────────────────────────────────────────────────────────────

def prob_bucket(prob: float) -> str:
    if prob >= 0.70: return '70+'
    if prob >= 0.65: return '65-70'
    if prob >= 0.60: return '60-65'
    return '55-60'


def grade_pick(actual: float | None, line: float, direction: str) -> tuple[str, int | None]:
    """
    Returns (result, hit) where result ∈ {'hit', 'miss', 'push', 'void'}
    and hit ∈ {1, 0, None}.
    """
    if actual is None:
        return 'void', None
    if actual == line:
        return 'push', None
    if direction == 'OVER':
        return ('hit', 1) if actual > line else ('miss', 0)
    else:  # UNDER
        return ('hit', 1) if actual < line else ('miss', 0)


# ── Main grading flow ──────────────────────────────────────────────────────────

def grade_date(date_str: str, overwrite: bool = False) -> None:
    from datetime import datetime as _dt
    try:
        _dt.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print(f"  Invalid date format: {date_str!r}. Expected YYYY-MM-DD.")
        return

    picks_path = PICKS_DIR / f"{date_str}.json"
    if not picks_path.exists():
        print(f"  No picks file found: {picks_path}")
        print(f"  Run first: python scripts/generate_todays_picks.py --date {date_str} --save")
        return

    try:
        with open(picks_path) as f:
            payload = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  Invalid JSON in {picks_path}: {e}")
        return

    picks = payload['picks']
    season = payload.get('season', '2025-26')
    is_playoffs = payload.get('is_playoffs', False)
    season_type = 'Playoffs' if is_playoffs else 'Regular Season'
    if not picks:
        print(f"  No picks in {picks_path}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    init_results_table(conn)

    # Check for already-graded picks
    if not overwrite:
        existing = conn.execute(
            "SELECT COUNT(*) FROM pick_results WHERE pick_date = ?", (date_str,)
        ).fetchone()[0]
        if existing > 0:
            print(f"  {existing} picks already graded for {date_str}. Use --overwrite to re-grade.")
            conn.close()
            return

    # Look up actuals — try DB first, fall back to API
    player_ids = list({p['player_id'] for p in picks})
    mode_label = 'Playoffs' if is_playoffs else 'Regular Season'
    print(f"  Looking up actuals for {len(player_ids)} players on {date_str} [{mode_label}]...")
    actuals = fetch_actuals_from_db(conn, player_ids, date_str, season_type=season_type)

    missing = [pid for pid in player_ids if pid not in actuals]
    if missing:
        print(f"  {len(actuals)}/{len(player_ids)} found in DB. "
              f"Fetching {len(missing)} via NBA API (slow)...")
        api_actuals = fetch_actuals_from_api(missing, date_str, season, season_type=season_type)
        actuals.update(api_actuals)

    # Grade and upsert
    graded = 0
    skipped = 0
    for pick in picks:
        pid      = pick['player_id']
        stat_key = pick['stat_key']
        line     = pick['line']
        direction = pick['direction']

        player_data = actuals.get(pid)
        if player_data is None:
            actual_value = None  # player not found in any source
        elif not player_data['played']:
            actual_value = None  # DNP — will grade as void
        else:
            actual_value = player_data.get(stat_key)

        result, hit = grade_pick(actual_value, line, direction)

        bucket = prob_bucket(pick['prob'])

        try:
            if overwrite:
                conn.execute("""
                    INSERT INTO pick_results (
                        pick_date, player_id, player_name, team, opp,
                        stat, stat_key, direction, line, model_pred, prob,
                        prob_bucket, card, n_legs, line_source, injury_flag,
                        actual_value, result, hit
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(pick_date, player_id, stat_key, direction)
                    DO UPDATE SET
                        actual_value = excluded.actual_value,
                        result       = excluded.result,
                        hit          = excluded.hit,
                        graded_at    = datetime('now')
                """, (
                    date_str, pid, pick['player_name'], pick['team'], pick['opp'],
                    pick['stat'], stat_key, direction, line, pick['model_pred'],
                    pick['prob'], bucket, pick['card'], pick['n_legs'],
                    pick['line_source'], pick['injury_flag'],
                    actual_value, result, hit,
                ))
            else:
                conn.execute("""
                    INSERT OR IGNORE INTO pick_results (
                        pick_date, player_id, player_name, team, opp,
                        stat, stat_key, direction, line, model_pred, prob,
                        prob_bucket, card, n_legs, line_source, injury_flag,
                        actual_value, result, hit
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    date_str, pid, pick['player_name'], pick['team'], pick['opp'],
                    pick['stat'], stat_key, direction, line, pick['model_pred'],
                    pick['prob'], bucket, pick['card'], pick['n_legs'],
                    pick['line_source'], pick['injury_flag'],
                    actual_value, result, hit,
                ))
            graded += 1
        except sqlite3.Error as e:
            print(f"  DB error for {pick['player_name']} {stat_key}: {e}")
            skipped += 1

    conn.commit()

    # Summary
    hits   = sum(1 for p in picks if _lookup_result(actuals, p) == 'hit')
    misses = sum(1 for p in picks if _lookup_result(actuals, p) == 'miss')
    voids  = sum(1 for p in picks if _lookup_result(actuals, p) == 'void')
    gradeable = hits + misses
    hit_rate = hits / gradeable if gradeable else float('nan')

    print(f"\n  ── {date_str} Results ──────────────────────────────────────")
    print(f"  Picks graded: {graded}  |  Skipped: {skipped}")
    print(f"  Hit: {hits}  |  Miss: {misses}  |  Void/DNP: {voids}")
    if gradeable:
        print(f"  Hit rate: {hit_rate:.1%}  ({hits}/{gradeable})")

    for pick in picks:
        player_data = actuals.get(pick['player_id'])
        actual_value = None
        if player_data and player_data['played']:
            actual_value = player_data.get(pick['stat_key'])
        result, _ = grade_pick(actual_value, pick['line'], pick['direction'])
        icon = {'hit': '✅', 'miss': '❌', 'push': '➖', 'void': '⚫'}.get(result, '?')
        actual_str = f"{actual_value:.0f}" if actual_value is not None else "DNP"
        print(f"  {icon} {pick['player_name']:25s} {pick['stat']:>7} "
              f"{pick['direction']:>5}  line={pick['line']:.1f}  "
              f"actual={actual_str:>4}  [{pick['card']}]")

    conn.close()


def _lookup_result(actuals: dict, pick: dict) -> str:
    player_data = actuals.get(pick['player_id'])
    if player_data is None or not player_data['played']:
        actual_value = None
    else:
        actual_value = player_data.get(pick['stat_key'])
    result, _ = grade_pick(actual_value, pick['line'], pick['direction'])
    return result


# ── Calibration report ─────────────────────────────────────────────────────────

def run_report(from_date: str | None = None, to_date: str | None = None) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    init_results_table(conn)

    date_filter = "WHERE result IN ('hit', 'miss')"
    params: list = []
    if from_date:
        date_filter += " AND pick_date >= ?"
        params.append(from_date)
    if to_date:
        date_filter += " AND pick_date <= ?"
        params.append(to_date)

    total_row = conn.execute(
        f"SELECT COUNT(*), SUM(hit) FROM pick_results {date_filter}", params
    ).fetchone()
    total, total_hits = total_row
    if not total:
        print("  No graded picks found. Run log_pick_results.py --date YYYY-MM-DD first.")
        conn.close()
        return

    sep = "─" * 62
    print(f"\n  {sep}")
    print(f"  CALIBRATION REPORT  {'(' + from_date + ' to ' + to_date + ')' if from_date else '(all dates)'}")
    print(f"  {sep}")
    print(f"  Overall: {int(total_hits)}/{total} = {total_hits/total:.1%} hit rate")

    # By stat
    print(f"\n  By stat:")
    rows = conn.execute(f"""
        SELECT stat_key, COUNT(*) as n, SUM(hit) as hits
        FROM pick_results {date_filter}
        GROUP BY stat_key ORDER BY stat_key
    """, params).fetchall()
    for stat_key, n, hits in rows:
        rate = hits / n if n else 0
        bar = '█' * int(rate * 20)
        print(f"    {stat_key.upper():>8}: {int(hits):>3}/{n:<4}  {rate:.1%}  {bar}")

    # By probability bucket
    print(f"\n  By probability bucket (model confidence):")
    bucket_order = ['55-60', '60-65', '65-70', '70+']
    rows = conn.execute(f"""
        SELECT prob_bucket, COUNT(*) as n, SUM(hit) as hits, AVG(prob) as avg_prob
        FROM pick_results {date_filter}
        GROUP BY prob_bucket
    """, params).fetchall()
    bucket_map = {r[0]: r for r in rows}
    for b in bucket_order:
        if b not in bucket_map:
            continue
        _, n, hits, avg_prob = bucket_map[b]
        rate = hits / n if n else 0
        calibration = rate - avg_prob
        cal_str = f"{calibration:+.1%}"
        marker = '✅' if abs(calibration) <= 0.05 else ('📈' if calibration > 0 else '📉')
        print(f"    P={b:>6}%:  {int(hits):>3}/{n:<4}  actual={rate:.1%}  "
              f"model={avg_prob:.1%}  diff={cal_str}  {marker}")

    # By card
    print(f"\n  By card:")
    rows = conn.execute(f"""
        SELECT card, n_legs, COUNT(*) as n, SUM(hit) as hits
        FROM pick_results {date_filter}
        GROUP BY card, n_legs ORDER BY card
    """, params).fetchall()
    for card, n_legs, n, hits in rows:
        rate = hits / n if n else 0
        print(f"    {card} ({n_legs}-leg):  {int(hits):>3}/{n:<4}  {rate:.1%}")

    # By direction
    print(f"\n  By direction:")
    rows = conn.execute(f"""
        SELECT direction, COUNT(*) as n, SUM(hit) as hits
        FROM pick_results {date_filter}
        GROUP BY direction
    """, params).fetchall()
    for direction, n, hits in rows:
        rate = hits / n if n else 0
        print(f"    {direction:>5}:  {int(hits):>3}/{n:<4}  {rate:.1%}")

    # Date-by-date
    print(f"\n  By date:")
    rows = conn.execute(f"""
        SELECT pick_date, COUNT(*) as n, SUM(hit) as hits
        FROM pick_results {date_filter}
        GROUP BY pick_date ORDER BY pick_date
    """, params).fetchall()
    for pick_date, n, hits in rows:
        rate = hits / n if n else 0
        bar = '█' * int(rate * 10)
        print(f"    {pick_date}:  {int(hits):>2}/{n:<3}  {rate:.0%}  {bar}")

    # Threshold validation summary
    print(f"\n  Threshold calibration check:")
    print(f"  (model P vs actual hit rate — within ±5% is well-calibrated)")
    overall_model_p = conn.execute(
        f"SELECT AVG(prob) FROM pick_results {date_filter}", params
    ).fetchone()[0]
    actual_rate = total_hits / total
    diff = actual_rate - overall_model_p
    status = "✅ well-calibrated" if abs(diff) <= 0.05 else ("📉 overconfident" if diff < 0 else "📈 underconfident")
    print(f"  Avg model P: {overall_model_p:.1%}  |  Actual hit rate: {actual_rate:.1%}  |  {status}")
    if abs(diff) > 0.05:
        suggestion = "lower" if diff < 0 else "raise"
        print(f"  → Consider {suggestion}ing STAT_MIN_PROB thresholds in generate_todays_picks.py")

    print(f"  {sep}\n")
    conn.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Grade daily picks and report calibration")
    parser.add_argument('--date',      help='Date to grade: YYYY-MM-DD')
    parser.add_argument('--overwrite', action='store_true',
                        help='Re-grade picks even if already graded')
    parser.add_argument('--report',    action='store_true',
                        help='Show calibration report across all graded dates')
    parser.add_argument('--from',     dest='from_date', help='Report start date YYYY-MM-DD')
    parser.add_argument('--to',       dest='to_date',   help='Report end date YYYY-MM-DD')
    args = parser.parse_args()

    if args.report:
        run_report(args.from_date, args.to_date)
        return

    if not args.date:
        parser.print_help()
        sys.exit(1)

    print(f"\n  Grading picks for {args.date}...")
    grade_date(args.date, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
