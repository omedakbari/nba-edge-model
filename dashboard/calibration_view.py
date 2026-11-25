"""Calibration page, card section: reliability of the research card's p, raw vs calibrated
(artifact: data/processed/models/card_calibration.json, fit by src/models/calibration.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / 'data/processed/models/card_calibration.json'


def load(path: Path = ARTIFACT) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def summary_frame(art: dict) -> pd.DataFrame:
    """One row per (venue, subset, stat): n, hit, raw p / Brier / log loss, calibrated p / Brier / log loss."""
    rows = []
    for r in art['report']['kalshi_test'] + art['report']['ledger']:
        raw = r['raw']['overall']
        cal = (r.get('calibrated') or {}).get('overall') or {}
        rows.append({'venue': r['venue'], 'subset': r['subset'], 'stat': r['stat'], 'n': raw['n'],
                     'hit rate': raw['hit'], 'raw p': raw['pred'], 'raw Brier': raw['brier'],
                     'raw log loss': raw['logloss'], 'cal p': cal.get('pred'), 'cal Brier': cal.get('brier'),
                     'cal log loss': cal.get('logloss'), 'market mid Brier': r.get('market_mid_brier'),
                     'P&L / contract (after fee)': r.get('pnl_per_contract_after_fee'),
                     'pass rules raw': r.get('clears_card_rules_raw'),
                     'pass rules cal': r.get('clears_card_rules_calibrated')})
    return pd.DataFrame(rows)


def bucket_frame(entry: dict) -> pd.DataFrame:
    """Reliability buckets for one report entry, raw and calibrated side by side (bucketed by each's own p)."""
    out = []
    for kind in ('raw', 'calibrated'):
        for b in ((entry.get(kind) or {}).get('buckets') or []):
            out.append({'p': kind, 'bucket': f"{b['lo']:.2f}-{b['hi']:.2f}", 'n': b['n'], 'predicted': b['pred'],
                        'actual': b['hit'], 'Brier': b['brier'], 'log loss': b['logloss']})
    return pd.DataFrame(out)


def render(st) -> None:
    st.header("Research card calibration (what the card uses)")
    art = load()
    if art is None:
        st.warning("No card calibration yet. Build it: `cd worker && ../venv/bin/python -m prop_worker.replay`, "
                   "then `./venv/bin/python -m src.models.calibration --fit`. Until then the card's p is raw.")
        return
    k = art['kalshi']
    pooled = k['maps']['_pooled']
    st.markdown(
        f"Fit **{art['fit_date'][:16].replace('T', ' ')} UTC** on **{k.get('n_slates', '?')} slates**. "
        f"Kalshi: no-lookahead replay of the card formula on "
        f"{k['dates'][0]} → {k['dates'][1]} ({k['n_region']:,} candidate sides in the card region, "
        f"{k['n_players_games']:,} player-games). Time split: fit {k['time_split']['fit'][0]} → "
        f"{k['time_split']['fit'][1]}, **tested on {k['time_split']['test'][0]} → {k['time_split']['test'][1]}** "
        f"(the tables below). Method: `{pooled['method']}` (market mid + history disagreement; "
        f"b_mid {pooled.get('b_mid', 0):.2f}, b_dev {pooled.get('b_dev', 0):+.2f}). "
        f"Sleeper: `{art['sleeper']['method']}` {art['sleeper'].get('offset', 0):+.4f} from n={art['sleeper']['n']} "
        "graded ledger legs (shrunk). See DECISIONS.md D9–D13.")
    from src.models.calibration import MAX_AGE_DAYS, MIN_SLATES, health
    for w in health():
        st.warning(f"{w}  (refit at least every {MAX_AGE_DAYS} days; under {MIN_SLATES} slates is provisional)")
    df = summary_frame(art)
    st.dataframe(df, hide_index=True, width='stretch', column_config={
        c: {'format': '%.3f'} for c in ('hit rate', 'raw p', 'cal p')})
    st.caption("Card region = sides where raw p > Kalshi ask (the only place the card can pick). "
               "Selected (raw rules) = what the old card would have shown; selected (calibrated rules) = what it "
               "shows now. Market mid Brier is the bar to beat: a lower Brier there means the hit-rate history adds "
               "nothing over the market price.")
    options = [f"{r['venue']} · {r['subset']} · {r['stat']}" for r in art['report']['kalshi_test'] + art['report']['ledger']]
    default = next((i for i, o in enumerate(options) if 'selected (raw rules) · all' in o), 0)
    pick = st.selectbox("Reliability buckets for", options, index=default)
    entry = (art['report']['kalshi_test'] + art['report']['ledger'])[options.index(pick)]
    st.dataframe(bucket_frame(entry), hide_index=True, width='stretch')
