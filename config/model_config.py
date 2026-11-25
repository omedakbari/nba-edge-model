"""Loader for config/model.yaml: every hand-set blend weight, validated. One source of truth.

    from config.model_config import WEIGHTS
    WEIGHTS['card']['hist_weight']   # 0.35
"""
from __future__ import annotations

from pathlib import Path

import yaml

PATH = Path(__file__).with_name('model.yaml')
REQUIRED = {
    'props_model': ('l10_weight',),
    'card': ('hist_weight', 'no_market_shrink'),
    'team_ratings': ('season_weight',),
    'prop_prior': ('signal_weight',),
}


def load(path: Path = PATH) -> dict:
    data = yaml.safe_load(Path(path).read_text())
    for section, keys in REQUIRED.items():
        for key in keys:
            v = (data.get(section) or {}).get(key)
            if not isinstance(v, (int, float)) or not 0.0 <= float(v) <= 1.0:
                raise ValueError(f'{path}: {section}.{key} must be a number in [0, 1], got {v!r}')
            data[section][key] = float(v)
    return data


WEIGHTS = load()


def describe() -> str:
    """One line for UIs: what the blends currently are."""
    w = WEIGHTS
    l10 = w['props_model']['l10_weight']
    h = w['card']['hist_weight']
    return (f"ML props: {1 - l10:.0%} model / {l10:.0%} L10 · "
            f"card: {h:.0%} hit-rate history / {1 - h:.0%} market (no-vig or Kalshi mid)")
