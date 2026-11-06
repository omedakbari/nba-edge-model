"""
Market-anchored prop probabilities.

The props model alone is ~5x overconfident against real sportsbook lines
(scripts/backtest_props_vs_market.py), but its disagreement with the market
does carry information. The most accurate probability found out-of-sample
(scripts/market_blend_research.py, held-out 2025-26) is:

    logit P(over) = a + b·logit(market) + c·(logit(model) − logit(market))

where `market` is the sportsbook consensus no-vig P(over), moved to the
pick'em line with a calibrated Normal line shift, and c ≈ 0.15-0.23.

Parameters live in data/processed/models/market_blend.json and are refit with:
    python -m src.models.market_blend --fit
"""

import json
import sys
from pathlib import Path

import numpy as np
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import PROCESSED_DIR

PARAMS_PATH = PROCESSED_DIR / 'models' / 'market_blend.json'
_params: dict | None = None


def _load() -> dict:
    global _params
    if _params is None:
        _params = json.loads(PARAMS_PATH.read_text())
    return _params


def _logit(p):
    p = np.clip(p, 0.01, 0.99)
    return np.log(p / (1 - p))


def shift_line(stat: str, book_line: float, book_p_over: float, target_line: float) -> float:
    """Move a no-vig P(over book_line) to P(over target_line) via a Normal with
    stat-specific sigma = s0 + s1·line, calibrated against real outcomes."""
    if target_line == book_line:
        return float(book_p_over)
    s0, s1 = _load()['line_shift'][stat]
    sig = max(s0 + s1 * book_line, 0.3)
    mu = book_line + sig * norm.ppf(np.clip(book_p_over, 0.02, 0.98))
    return float(np.clip(1 - norm.cdf((target_line - mu) / sig), 0.01, 0.99))


def blend(stat: str, market_p_over: float, model_p_over: float) -> float:
    """Final P(over) from market and model probabilities at the same line."""
    a, b, c = _load()['blend'][stat]
    m = _logit(market_p_over)
    z = a + b * m + c * (_logit(model_p_over) - m)
    return float(1 / (1 + np.exp(-z)))


def has_params(stat: str) -> bool:
    try:
        p = _load()
    except FileNotFoundError:
        return False
    return stat in p['blend'] and stat in p['line_shift']


# ── Fitting ───────────────────────────────────────────────────────────────────

def fit(dataset_path: Path = PROCESSED_DIR / 'market_blend_dataset.parquet') -> dict:
    """Fit blend + line-shift params on the walk-forward research dataset
    (built by scripts/market_blend_research.py)."""
    import pandas as pd
    from scipy.optimize import minimize

    d = pd.read_parquet(dataset_path)
    out = {'blend': {}, 'line_shift': {}, 'n': {},
           'seasons': sorted(d['season'].unique().tolist())}
    for stat, g in d.groupby('stat_key'):
        X = np.c_[np.ones(len(g)), _logit(g['mkt_p_over']),
                  _logit(g['model_p_over']) - _logit(g['mkt_p_over'])]
        y = g['over_hit'].values.astype(float)
        b = np.zeros(3)
        for _ in range(25):                                  # Newton-Raphson logistic fit
            p = 1 / (1 + np.exp(-X @ b))
            H = X.T @ (X * (p * (1 - p))[:, None])
            b = b + np.linalg.solve(H, X.T @ (y - p))
        out['blend'][stat] = [round(float(v), 4) for v in b]

        # Line shift: outcomes at book line ± 1, 2
        rows = []
        for dl in (-2, -1, 1, 2):
            x = g[['line', 'mkt_p_over', 'actual']].copy()
            x['tl'] = x['line'] + dl
            x = x[x['actual'] != x['tl']]
            x['y'] = (x['actual'] > x['tl']).astype(float)
            rows.append(x)
        x = pd.concat(rows)

        def nll(v):
            sig = np.maximum(v[0] + v[1] * x['line'], 0.3)
            mu = x['line'] + sig * norm.ppf(np.clip(x['mkt_p_over'], 0.02, 0.98))
            p = np.clip(1 - norm.cdf((x['tl'] - mu) / sig), 1e-4, 1 - 1e-4)
            return -np.mean(x['y'] * np.log(p) + (1 - x['y']) * np.log(1 - p))
        s = minimize(nll, [2.0, 0.2], method='Nelder-Mead').x
        out['line_shift'][stat] = [round(float(v), 4) for v in s]
        out['n'][stat] = int(len(g))
        print(f"  {stat:<8} n={len(g):>6,}  blend a={b[0]:+.3f} b={b[1]:.3f} c={b[2]:.3f}"
              f"   line-shift sigma={s[0]:.2f}+{s[1]:.3f}·line")

    PARAMS_PATH.write_text(json.dumps(out, indent=2))
    print(f"  saved → {PARAMS_PATH}")
    global _params
    _params = out
    return out


if __name__ == '__main__':
    if '--fit' in sys.argv:
        fit()
    else:
        print(__doc__)


def fair_p_over(stat: str, line: float, pred: float, std: float,
                book_line: float, book_p_over: float) -> float:
    """Our best P(stat > line) at ANY line: book consensus shifted to `line`,
    nudged by the model's own P(over line)."""
    from src.utils.probability import prob_over
    mkt = shift_line(stat, book_line, book_p_over, line)
    return blend(stat, mkt, prob_over(pred, line, std))
