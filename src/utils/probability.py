"""
Shared probability/model-loading helpers used by the daily picks pipeline and
the diagnostic/backtest scripts.

These two functions were previously copy-pasted identically across
scripts/generate_todays_picks.py, scripts/backtest_playoffs.py,
scripts/phase1_calibration_audit.py, and scripts/validate_playoffs.py.
Consolidated here — behavior unchanged, single source of truth.

Usage:
    from src.utils.probability import prob_over, load_model
"""

import pickle

import numpy as np
from scipy import stats as scipy_stats

from config.settings import PROCESSED_DIR


def prob_over(pred: float, line: float, std: float) -> float:
    """
    P(actual > line) under a Gaussian centered at `pred` with stdev `std`.

    Falls back to std = max(|pred| * 0.2, 1.0) when std is missing/non-positive
    (e.g. insufficient rolling-window history for a real stdev estimate).
    Clipped to [0.01, 0.99] — never fully certain either direction.
    """
    if std <= 0 or np.isnan(std):
        std = max(abs(pred) * 0.2, 1.0)
    z = (line - pred) / std
    return float(np.clip(1 - scipy_stats.norm.cdf(z), 0.01, 0.99))


def load_model(stat: str) -> dict:
    """
    Load a trained stat model pickle from data/processed/models/.
    Raises FileNotFoundError with a clear message (including the retrain
    command) if the model hasn't been trained yet.
    """
    path = PROCESSED_DIR / "models" / f"{stat}_model.pkl"
    if not path.exists():
        raise FileNotFoundError(f"Model not found: {path}. Run: python -m src.models.player_props")
    with open(path, 'rb') as f:
        return pickle.load(f)
