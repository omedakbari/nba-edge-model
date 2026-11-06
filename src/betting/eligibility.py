"""Approval is separate from expected return and model confidence.

No confidence bounds or injury adjustments are manufactured here. Missing
evidence blocks approval; descriptive point estimates remain available.
"""
import math
from datetime import datetime, timezone

KEA_MARGIN = 0.03
CSB_JOINT_MIN = 0.80
MAX_QUOTE_AGE_SECONDS = 900


def approval(rec, p, decimal, *, min_ev=0.04, margin=KEA_MARGIN, now=None):
    reasons = []
    if not (math.isfinite(p) and 0 <= p <= 1 and math.isfinite(decimal) and decimal > 1):
        return dict(approved=False, reasons=['invalid probability or price'], stake_pct=0.0)
    advantage = p - 1 / decimal
    if p * decimal - 1 < min_ev:
        reasons.append('EV below policy')
    if advantage < margin:
        reasons.append('KEA probability margin below policy')
    for field, reason in [('probability_validated', 'probability unvalidated'),
                          ('injury_role_reviewed', 'injury/role review required'),
                          ('mapping_verified', 'player/game/market mapping unverified'),
                          ('settlement_verified', 'settlement/fees unverified')]:
        if rec.get(field) is not True:
            reasons.append(reason)
    now = now or datetime.now(timezone.utc)
    try:
        captured = datetime.fromisoformat(rec['captured_at'].replace('Z', '+00:00'))
        updated = datetime.fromisoformat(rec['quote_updated_at'].replace('Z', '+00:00'))
        start = datetime.fromisoformat(rec['start_time'].replace('Z', '+00:00'))
        if captured.tzinfo is None or start.tzinfo is None or updated.tzinfo is None:
            raise ValueError('naive timestamp')
        age = (now - captured).total_seconds()
        if not 0 <= age <= MAX_QUOTE_AGE_SECONDS:
            reasons.append('quote stale or future dated')
        if not 0 <= (now - updated).total_seconds() <= MAX_QUOTE_AGE_SECONDS or updated > captured:
            reasons.append('provider quote stale or future dated')
        if start <= now or rec.get('status') != 'scheduled':
            reasons.append('game not confirmed pregame')
    except (KeyError, TypeError, ValueError, AttributeError):
        reasons.append('missing verified quote/start timestamps')
    from src.betting.ev import stake_pct
    return dict(approved=not reasons, reasons=reasons, probability_advantage=advantage,
                kea_margin=margin, stake_pct=stake_pct(p, decimal) if not reasons else 0.0)


def conservative_joint(lower_bounds):
    """Frechet/union bound, valid without assuming independence.

    Inputs must be externally validated simultaneous lower probability bounds.
    Point predictions are deliberately not substituted for missing bounds.
    """
    if not lower_bounds or any(p is None or not math.isfinite(p) or not 0 <= p <= 1
                               for p in lower_bounds):
        return None
    return max(0.0, 1 - sum(1 - p for p in lower_bounds))
