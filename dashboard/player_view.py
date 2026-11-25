"""Player Lookup, current season (Phase 4c): game log, L5/L10/L20/season averages, hit rate vs each of the
player's props tonight, and tonight's injury status. Reads the local data/nba_edge.db (refreshed by the daily run;
the 'as of' date is shown) and today's card JSON (props, ladders, exclusions). The old 2024-25 test-set view
stays behind the "2024-25 test set" toggle in dashboard/app.py.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'data' / 'nba_edge.db'
STAT_COL = {'MIN': 'min', 'PTS': 'pts', 'REB': 'reb', 'AST': 'ast', '3PM': 'fg3m', 'PRA': 'pra', 'RA': 'ra'}
WINDOWS = (('L5', 5), ('L10', 10), ('L20', 20))


def current_season(conn) -> str | None:
    row = conn.execute("select max(season) from game_logs where season_type = 'Regular Season'").fetchone()
    return row[0] if row else None


def players(conn, season: str) -> dict[str, int]:
    """{full name: player_id} for everyone who played this season."""
    return dict(conn.execute(
        """select p.full_name, p.player_id from players p
           where exists (select 1 from game_logs g where g.player_id = p.player_id and g.season = ? and g.min > 0)
           order by p.full_name""", (season,)).fetchall())


def game_log(conn, player_id: int, season: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """select game_date as Date, opp_abbr as Opp, matchup as Matchup, min as MIN, pts as PTS, reb as REB,
                  ast as AST, fg3m as "3PM"
           from game_logs where player_id = ? and season = ? and coalesce(min, 0) > 0
           order by game_date desc""", conn, params=(player_id, season))
    df['PRA'] = df['PTS'] + df['REB'] + df['AST']
    df['RA'] = df['REB'] + df['AST']
    return df


def averages(log: pd.DataFrame) -> pd.DataFrame:
    cols = ['MIN', 'PTS', 'REB', 'AST', '3PM', 'PRA']
    rows = [{'Window': name, 'Games': min(n, len(log)), **log.head(n)[cols].mean().round(1).to_dict()}
            for name, n in WINDOWS]
    rows.append({'Window': 'Season', 'Games': len(log), **log[cols].mean().round(1).to_dict()})
    return pd.DataFrame(rows)


def props_tonight(card: dict | None, name: str) -> list[dict]:
    """[{venue, market, stat, line, side}] for this player: shown card legs + every Kalshi strike."""
    from board_view import norm_name
    who = norm_name(name)
    out = []
    for g in (card or {}).get('legs', []):
        if norm_name(g.get('player', '')) != who:
            continue
        if g.get('platform') == 'kalshi':
            strike = int(str(g['line']).rstrip('+'))
            out.append({'venue': 'Kalshi (card)', 'market': f"{g['stat']} {strike}+", 'stat': g['stat'],
                        'line': strike - 0.5, 'side': 'over' if g['side'] == 'YES' else 'under'})
        else:
            out.append({'venue': 'Sleeper (card)', 'market': f"{g['stat']} {str(g['side']).lower()} {g['line']}",
                        'stat': g['stat'], 'line': float(g['line']), 'side': str(g['side']).lower()})
    for lad in (card or {}).get('kalshi_ladders', []):
        if norm_name(lad.get('player', '')) != who:
            continue
        for s in lad['strikes']:
            out.append({'venue': 'Kalshi ladder', 'market': f"{lad['stat']} {s['strike']}+", 'stat': lad['stat'],
                        'line': s['strike'] - 0.5, 'side': 'over', 'p_cal_yes': s.get('p_cal_yes'),
                        'yes_ask': s.get('yes_ask')})
    return out


def hit_table(log: pd.DataFrame, props: list[dict]) -> pd.DataFrame:
    """Hit rate vs each line ('hits/n'), L5 / L10 / L20 / season, from the game log (pushes count as misses)."""
    rows = []
    for p in props:
        col = p['stat'] if p['stat'] in log.columns else None
        row = {'Venue': p['venue'], 'Market': p['market'], 'Side': p['side']}
        for name, n in WINDOWS + (('Season', None),):
            vals = (log[col] if col else pd.Series(dtype=float)).head(n) if n else (log[col] if col else pd.Series(dtype=float))
            vals = vals.dropna()
            hits = int(((vals > p['line']) if p['side'] == 'over' else (vals < p['line'])).sum())
            row[name] = f'{hits}/{len(vals)}' if len(vals) else ''
        if 'p_cal_yes' in p:
            row['YES ask'] = p.get('yes_ask')
            row['P cal (YES)'] = p.get('p_cal_yes')
        rows.append(row)
    return pd.DataFrame(rows)


def injury_status(card: dict | None, name: str) -> str:
    from board_view import norm_name
    who = norm_name(name)
    for e in (card or {}).get('excluded', []):
        if norm_name(e.get('player', '')) == who and e['reason'] in ('injury', 'not in roster ingest — two-way',
                                                                     'injury name not matched'):
            return f"EXCLUDED tonight — {e['detail']}"
    for g in (card or {}).get('legs', []):
        if norm_name(g.get('player', '')) == who and isinstance(g.get('injury'), dict):
            i = g['injury']
            return f"{i.get('code')} ({i.get('status')}, {i.get('source')} as of {i.get('as_of')}) — not excluded"
    inj = (card or {}).get('injury') or {}
    return (f"Not on tonight's injury reports (official as of {inj.get('official_as_of') or '?'}, "
            f"ESPN {inj.get('espn_as_of') or '?'})" if card else "No card for today yet")


def render(st, day: str) -> None:
    if not DB.exists():
        st.warning("data/nba_edge.db not found.")
        return
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True) as conn:
        season = current_season(conn)
        names = players(conn, season) if season else {}
        as_of = conn.execute('select max(game_date) from game_logs').fetchone()[0]
        st.caption(f"Season {season} from data/nba_edge.db, box scores through **{as_of}** (refreshed by the daily run).")
        name = st.selectbox("Search for a player", list(names), index=None, placeholder="Type a player name...")
        if not name:
            return
        log = game_log(conn, names[name], season)
    try:
        card = json.loads((ROOT / 'data' / 'cards' / f'{day}.json').read_text())
    except (OSError, ValueError):
        card = None
    st.markdown(f"**Tonight's injury status:** {injury_status(card, name)}")
    st.subheader("Averages")
    st.dataframe(averages(log), hide_index=True, width='stretch')
    props = props_tonight(card, name)
    st.subheader("Tonight's props: hit rate vs each line")
    if props:
        st.dataframe(hit_table(log, props), hide_index=True, width='stretch')
    else:
        st.caption("No props for this player on today's card or Kalshi ladders.")
    st.subheader(f"Game log ({len(log)} games)")
    st.dataframe(log.drop(columns=['RA']), hide_index=True, width='stretch')
