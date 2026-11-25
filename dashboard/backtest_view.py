"""Backtest page (top) and Calibration page (ML section): held-out results for all six models at Kalshi N+
thresholds (src/backtest/model_backtest.py), and the research card's Kalshi legs on held-out dates
(src/models/calibration.py report)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BACKTEST = ROOT / 'data/processed/backtest/model_backtest.json'
CALIBRATION = ROOT / 'data/processed/models/card_calibration.json'


def _load(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def model_frame(rep: dict) -> pd.DataFrame:
    rows = []
    for stat, e in rep['stats'].items():
        if 'error' in e:
            rows.append({'stat': stat, 'season': None, 'held out': None, 'note': e['error']})
            continue
        c, t, l = e['counts'], e['thresholds']['overall'], e['l10_line']['overall']
        rows.append({'stat': stat, 'season': e['season'], 'held out': not e['in_sample'],
                     'rows in': c['rows_in'], 'scored': c['rows_scored'], 'dropped min<20': c['dropped_min_L10'],
                     'dropped no box': c['dropped_no_actual'], 'null-feature rows kept': c['rows_with_null_features'],
                     'MAE': e['mae'], 'N+ n': t['n'], 'N+ p': t['pred'], 'N+ hit': t['hit'], 'N+ Brier': t['brier'],
                     'N+ log loss': t['logloss'], 'L10-line p': l['pred'], 'L10-line hit': l['hit']})
    return pd.DataFrame(rows)


def card_frame(art: dict) -> pd.DataFrame:
    """Per stat, held-out dates: legs the raw rules picked vs legs the calibrated rules pick (hit, P&L)."""
    rows = []
    for r in art['report']['kalshi_test']:
        if r['subset'] not in ('card selected (raw rules)', 'card selected (calibrated rules)'):
            continue
        o = r['raw']['overall']
        rows.append({'rules': 'calibrated' if 'calibrated' in r['subset'] else 'raw (old card)', 'stat': r['stat'],
                     'legs': o['n'], 'hit rate': o['hit'], 'mean price': r.get('mean_price'),
                     'P&L / contract after fee': r.get('pnl_per_contract_after_fee')})
    return pd.DataFrame(rows)


def render(st, buckets: bool = False) -> None:
    rep = _load(BACKTEST)
    st.header("Held-out backtest: all six models, Kalshi N+ thresholds")
    if rep is None:
        st.warning("No held-out backtest yet: `./venv/bin/python -m src.backtest.model_backtest`.")
    else:
        st.markdown("Models were refit on every season through 2025-26, so only **2026-27** is held out. "
                    "N+ = Kalshi-style P(stat ≥ N) at 7 strikes around each projection; favoured side. "
                    "Rows with null features are scored, never dropped (playoff-series features are null all "
                    "regular season by design); every drop is counted.")
        st.dataframe(model_frame(rep), hide_index=True, width='stretch')
        if buckets:
            stat = st.selectbox("Reliability buckets (N+ thresholds)", list(rep['stats']))
            e = rep['stats'][stat]
            if 'thresholds' in e:
                st.dataframe(pd.DataFrame(e['thresholds']['buckets']), hide_index=True, width='stretch')
    art = _load(CALIBRATION)
    st.subheader("Research card, Kalshi legs on held-out dates")
    if art is None:
        st.warning("No card calibration artifact: the card withholds Kalshi legs.")
        return
    split = art['kalshi']['time_split']
    st.caption(f"Replay test dates {split['test'][0]} → {split['test'][1]}. Calibrated rules use the map fit on "
               f"{split['fit'][0]} → {split['fit'][1]} only. P&L = hit − price − Kalshi fee, per $1 contract.")
    st.dataframe(card_frame(art), hide_index=True, width='stretch')
