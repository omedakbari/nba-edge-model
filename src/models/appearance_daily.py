"""Daily appearance-v2 research inference. Approval always remains disabled."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.special import logit
from scipy.stats import norm
from src.features.population import appearance_prediction_features, pregame_rotation, VERSION

COMPONENTS = {'pra': ['pts','reb','ast'], 'reb_ast': ['reb','ast'],
              'pts_reb': ['pts','reb'], 'pts_ast': ['pts','ast']}


def load_bundle(path):
    # Only a locally configured, trusted training artifact; never a downloaded pickle.
    with Path(path).open('rb') as stream:
        bundle = pickle.load(stream)
    if bundle.get('version') != VERSION or bundle.get('probability_validated') is not False:
        raise ValueError('Expected an explicitly unvalidated appearance-v2 research bundle')
    return bundle


def predict(logs, contexts, day, season, bundle):
    if int(season.split('-')[0]) <= int(bundle['calibration_season'].split('-')[0]):
        raise ValueError('Research bundle calibration must precede the forecast season')
    frame = appearance_prediction_features(logs, contexts, day, season)
    if frame.empty:
        return frame, {}
    frame = frame[pregame_rotation(frame)].reset_index(drop=True)
    if frame.empty:
        return frame, {}
    X = frame[bundle['features']]
    estimates = {s: pd.Series(bundle['models'][s].predict(X), index=frame.index) for s in ['pts','reb','ast','fg3m','min']}
    frame['expected_minutes'] = estimates.pop('min')
    frame['short_minutes_risk'] = bundle['models']['minutes_risk'].predict_proba(X)[:,1]
    return frame, estimates


def probability(bundle, stat, line, pred, std, market, minutes_risk):
    calibrator = bundle['models'].get(stat + '_blend')
    if calibrator is None:
        return None
    sigma = std if np.isfinite(std) and std > 0 else max(abs(pred)*.2, 1)
    raw = float(np.clip(norm.sf((line-pred)/sigma), .01, .99))
    anchor = float(logit(np.clip(market, .01, .99)))
    X = pd.DataFrame([dict(market_logit=anchor, model_disagreement=logit(raw)-anchor,
                           minutes_risk=minutes_risk)])
    return float(calibrator.predict_proba(X)[0,1])
