"""calibration: apply the card calibration artifact (fit by src/models/calibration.py) without sklearn.

Artifact: data/processed/models/card_calibration.json (fit locally; gitignored; the cloud never builds the
card, DECISIONS.md D14). Missing or unreadable -> FAIL CLOSED: `missing` is True, the card withholds every
Kalshi leg, labels Sleeper legs "UNCALIBRATED (raw p)" and warns (the run goes WARN). Calibrator.identity()
is the explicit raw-p calibrator for tests and research only. Methods (same math as src/models/calibration.apply_map):
  logit2    p_cal = sigmoid(b0 + b_mid * logit(mid) + b_dev * (p_raw - mid) / w_hist)   (Kalshi; mid = side's mid)
  isotonic  piecewise-linear interpolation through (x, y), clipped at the ends
  offset    clip(p_raw + offset, .01, .99)                                              (Sleeper)
  identity  p_raw
Maps are fit on the favoured side (p >= .5); a side below .5 is mapped as 1 - f(1 - p, 1 - mid).
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from pathlib import Path

ARTIFACT = Path(__file__).resolve().parents[2] / "data" / "processed" / "models" / "card_calibration.json"
EPS = 1e-4


def _logit(p: float) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


def _interp(x: float, xs: list[float], ys: list[float]) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_right(xs, x)
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def apply(m: dict, p: float, mid: float | None = None) -> float:
    method = m["method"]
    if method == "logit2":
        if mid is None:
            raise ValueError("logit2 calibration needs the market mid")
        z = m["b0"] + m["b_mid"] * _logit(mid) + m["b_dev"] * (p - mid) / m["w_hist"]
        return 1 / (1 + math.exp(-z))
    if method == "isotonic":
        return _interp(p, m["x"], m["y"])
    if method == "offset":
        return min(max(p + m["offset"], 0.01), 0.99)
    if method == "identity":
        return p
    raise ValueError(f"unknown calibration method {method!r}")


class Calibrator:
    def __init__(self, artifact: dict | None, path: Path | None = None):
        self.art = artifact or {}
        self.ok = bool(artifact)
        self.missing = not artifact                 # fail closed: card withholds Kalshi legs
        self.path = path or ARTIFACT

    @classmethod
    def identity(cls) -> "Calibrator":
        """Explicit raw p (tests / research). Never what the live card uses when the artifact is missing."""
        c = cls({"identity": True})
        c.ok = False
        c.missing = False
        return c

    @classmethod
    def load(cls, path: Path = ARTIFACT) -> "Calibrator":
        try:
            return cls(json.loads(Path(path).read_text()), path)
        except (OSError, ValueError):
            return cls(None, path)

    def _map(self, venue: str, stat: str) -> dict:
        if not self.ok:
            return {"method": "identity"}
        if venue == "kalshi":
            maps = self.art.get("kalshi", {}).get("maps", {})
            m = maps.get(stat) or {"method": "pooled"}
            return maps.get("_pooled", {"method": "identity"}) if m["method"] == "pooled" else m
        return self.art.get(venue) or {"method": "identity"}

    def __call__(self, venue: str, stat: str, p: float, mid: float | None = None) -> float:
        m = self._map(venue, stat)
        if p >= 0.5:
            return apply(m, p, mid)
        return 1 - apply(m, 1 - p, None if mid is None else 1 - mid)

    def label(self) -> str:
        if self.missing:
            return f"UNCALIBRATED (raw p): calibration artifact missing ({self.path}); Kalshi legs withheld"
        if not self.ok:
            return "raw p (identity calibrator)"
        k = self.art.get("kalshi", {})
        return (f"calibrated {self.art.get('fit_date', '?')[:10]}: Kalshi replay {k.get('dates', ['?', '?'])[0]}.."
                f"{k.get('dates', ['?', '?'])[1]} n={k.get('n_region', '?')}; Sleeper ledger n="
                f"{self.art.get('sleeper', {}).get('n', '?')}")
