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
    is_play_in = payload.get('is_play_in', False)
    # Map saved flags → the season_type string used in game_logs.
    # Older JSON files lack 'is_play_in'; they may have is_playoffs=True for
    # play-in dates — fall through to 'Playoffs' in that case (safe: the
    # void-guard below will catch any dates that were never actually graded).
    if is_play_in:
        season_type = 'PlayIn'
    elif is_playoffs:
        season_type = 'Playoffs'
    else:
        season_type = 'Regular Season'
    if not picks:
        print(f"  No picks in {picks_path}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    init_results_table(conn)

    # Check for already-graded picks.
    # Void-only rows are grading artifacts (DB not yet updated at grade time);
    # they do not count as a real grade.  Auto-clear them so a re-run without
    # --overwrite still produces correct results once the DB is current.
    if not overwrite:
        total_existing, gradeable_existing = conn.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN result IN ('hit','miss') THEN 1 ELSE 0 END)
               FROM pick_results WHERE pick_date = ?""",
            (date_str,),
        ).fetchone()
        if total_existing > 0:
            if gradeable_existing == 0:
                # All existing rows are void/push — DB was stale at grade time.
                # Delete them and fall through to a clean re-grade.
                conn.execute(
                    "DELETE FROM pick_results WHERE pick_date = ?", (date_str,)
                )
                conn.commit()
                print(f"  Note: cleared {total_existing} void-only rows for {date_str} "
                      f"— re-grading with current DB.")
            else:
                print(f"  {total_existing} picks already graded for {date_str} "
                      f"({gradeable_existing} with real outcomes). "
                      f"Use --overwrite to re-grade.")
                conn.close()
                return

    # Look up actuals — try DB first, fall back to API
    player_ids = list({p['player_id'] for p in picks})
    if is_play_in:
        mode_label = 'Play-In'
    elif is_playoffs:
        mode_label = 'Playoffs'
    else:
        mode_label = 'Regular Season'
    print(f"  Looking up actuals for {len(player_ids)} players on {date_str} [{mode_label}]...")
    actuals = fetch_actuals_from_db(conn, player_ids, date_str, season_type=season_type)

    # Defensive fallback: picks files generated with --playoffs instead of --play-in
    # will have season_type='Playoffs' but the game logs live under 'PlayIn'.
    if not actuals and season_type == 'Playoffs':
        play_in_check = fetch_actuals_from_db(conn, player_ids, date_str, season_type='PlayIn')
        if play_in_check:
            print(f"  Note: 0 Playoffs rows found — retrying as PlayIn "
                  f"(picks file may have used --playoffs instead of --play-in).")
            actuals = play_in_check
            season_type = 'PlayIn'

    missing = [pid for pid in player_ids if pid not in actuals]
    if missing:
        missing_names = [p['player_name'] for p in picks if p['player_id'] in missing]
        print(f"  {len(actuals)}/{len(player_ids)} found in DB. "
              f"Fetching {len(missing)} via NBA API (slow): {', '.join(missing_names)}")
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


# ── Miss detail report (item 8) ────────────────────────────────────────────────

def run_miss_detail_report(conn: sqlite3.Connection,
                            date_filter: str, params: list) -> None:
    """
    Log high-confidence misses, near-misses, and direction×stat hit rates
    to surface systematic underperformers (UNDERs, REB_AST, rotation props).
    """
    sep = "─" * 62

    # High-confidence misses (P >= 75%)
    hc_misses = conn.execute(f"""
        SELECT player_name, stat_key, direction, line, actual_value, prob, pick_date
        FROM pick_results
        {date_filter} AND result = 'miss' AND prob >= 0.75
        ORDER BY prob DESC
        LIMIT 15
    """, params).fetchall()

    print(f"\n  {sep}")
    print(f"  MISS DETAIL REPORT")
    print(f"  {sep}")
    print(f"\n  High-confidence misses (P ≥ 75%): {len(hc_misses)}")
    if hc_misses:
        print(f"  {'Date':<12} {'Player':<22} {'Stat':>7} {'Dir':>5}  "
              f"{'Line':>5}  {'Actual':>6}  {'P':>5}  Note")
        for name, stat, direction, line, actual, prob, pick_date in hc_misses:
            actual_s = f"{actual:.0f}" if actual is not None else "DNP"
            if actual is not None:
                gap = abs(actual - line)
                note = " [≤0.5]" if gap <= 0.5 else (" [≤1.0]" if gap <= 1.0 else "")
            else:
                note = ""
            print(f"  {pick_date:<12} {name:<22} {stat.upper():>7} {direction:>5}  "
                  f"{line:>5.1f}  {actual_s:>6}  {prob:>4.0%}{note}")

    # Near-miss breakdown by stat × direction
    near_rows = conn.execute(f"""
        SELECT stat_key, direction,
               COUNT(*)                                                          AS n,
               SUM(CASE WHEN ABS(actual_value - line) <= 0.5 THEN 1 ELSE 0 END) AS w05,
               SUM(CASE WHEN ABS(actual_value - line) <= 1.0 THEN 1 ELSE 0 END) AS w10
        FROM pick_results
        {date_filter} AND result = 'miss' AND actual_value IS NOT NULL
        GROUP BY stat_key, direction
        ORDER BY w05 DESC
    """, params).fetchall()

    if near_rows:
        print(f"\n  Near-misses by stat × direction:")
        print(f"  {'Stat':<10} {'Dir':<6} {'Misses':>6}  {'≤0.5':>5}  {'≤1.0':>5}")
        for stat, direction, n, w05, w10 in near_rows:
            print(f"  {stat.upper():<10} {direction:<6} {n:>6}  {(w05 or 0):>5}  {(w10 or 0):>5}")

    # Hit rate by direction × stat — flag slices underperforming model by >8pp
    slice_rows = conn.execute(f"""
        SELECT direction, stat_key, COUNT(*) AS n, SUM(hit) AS hits, AVG(prob) AS avg_p
        FROM pick_results
        {date_filter}
        GROUP BY direction, stat_key
        ORDER BY direction, stat_key
    """, params).fetchall()

    if slice_rows:
        print(f"\n  Hit rate by direction × stat  (⚠️  = underperforms model by >8pp):")
        print(f"  {'Dir':<6} {'Stat':<10} {'N':>4}  {'Hit%':>6}  {'Model%':>7}  {'Diff':>6}")
        for direction, stat, n, hits, avg_p in slice_rows:
            if not n:
                continue
            hr   = (hits or 0) / n
            diff = hr - (avg_p or 0)
            flag = "  ⚠️" if diff < -0.08 else ""
            print(f"  {direction:<6} {stat.upper():<10} {n:>4}  {hr:>6.1%}  "
                  f"{(avg_p or 0):>7.1%}  {diff:>+6.1%}{flag}")

    print(f"  {sep}\n")


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

    # Void-only dates — in DB but excluded above because no hit/miss rows.
    # Build a range-aware filter for the void query.
    void_range = "1=1"
    void_params: list = []
    if from_date:
        void_range += " AND pick_date >= ?"
        void_params.append(from_date)
    if to_date:
        void_range += " AND pick_date <= ?"
        void_params.append(to_date)
    void_rows = conn.execute(f"""
        SELECT pick_date, COUNT(*) as n
        FROM pick_results
        WHERE {void_range}
          AND pick_date NOT IN (
              SELECT DISTINCT pick_date FROM pick_results
              WHERE result IN ('hit', 'miss')
                AND {void_range}
          )
        GROUP BY pick_date ORDER BY pick_date
    """, void_params + void_params).fetchall()
    if void_rows:
        print(f"\n  Void-only dates (excluded from hit-rate — no game data at grade time):")
        for pick_date, n in void_rows:
            print(f"    {pick_date}:  {n} void picks  ← run --overwrite after updating DB")

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
        action = "raising" if diff < 0 else "lowering"
        print(f"  → Consider {action} STAT_MIN_PROB thresholds in generate_todays_picks.py")

    print(f"  {sep}\n")

    run_miss_detail_report(conn, date_filter, params)

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
