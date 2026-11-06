"""Timestamped, identity-based teammate absence evidence.

Realized DNPs are not converted into pregame injury labels. Historical cases
must carry an explicit OUT/inactive assertion timestamped before tipoff.
Estimates describe conditional opportunity, not a causal absence effect.
"""
import numpy as np
import pandas as pd
import json
from pathlib import Path


def load_evidence(directory):
    evidence=[]
    for path in sorted(Path(directory).glob('*.json')):
        value=json.loads(path.read_text())
        if not isinstance(value,list):
            raise ValueError(f'Absence ledger must be a list: {path}')
        evidence.extend(value)
    return evidence


def eligible_absences(evidence, player_id, team, forecast_time):
    cutoff = pd.Timestamp(forecast_time)
    if cutoff.tzinfo is None:
        raise ValueError('forecast_time must have a timezone')
    accepted = {}
    for e in evidence:
        try:
            issued, start = pd.Timestamp(e['published_at']), pd.Timestamp(e['start_time'])
            if issued.tzinfo is None or start.tzinfo is None:
                continue
            if (int(e['player_id']) == int(player_id) and e['team'] == team
                    and issued <= cutoff < start and e.get('source_url')):
                key=(e.get('game_id'),e['start_time'])
                if key not in accepted or issued>pd.Timestamp(accepted[key]['published_at']):
                    accepted[key]=e
        except (KeyError, TypeError, ValueError):
            continue
    return [e for e in accepted.values() if e['status'].lower() in ('out','inactive')]


def role_estimate(logs, player_id, absent_ids, forecast_date, evidence):
    """Use only prior appearances with verified pre-tip absence evidence.

    Missing coverage is returned explicitly. No fallback treats missing box
    scores as proof of an injury, and no hardcoded player adjustment exists.
    """
    historical = logs[pd.to_datetime(logs.game_date) < pd.Timestamp(forecast_date)].copy()
    own = historical[(historical.player_id == player_id) & historical['min'].gt(0)].copy()
    cases = {}
    latest = {}
    forecast_cutoff = pd.Timestamp(forecast_date,tz='America/Los_Angeles')
    for e in evidence:
        try:
            published, start = pd.Timestamp(e['published_at']), pd.Timestamp(e['start_time'])
            if (published.tzinfo is None or start.tzinfo is None or published >= start
                    or published >= forecast_cutoff):
                continue
            if int(e['player_id']) in absent_ids and e.get('source_url'):
                key=(int(e['player_id']),e['team'],str(e['game_id']))
                if key not in latest or published>pd.Timestamp(latest[key]['published_at']):
                    latest[key]=e
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    for e in latest.values():
        try:
            start=pd.Timestamp(e['start_time'])
            if e['status'].lower() in ('out','inactive'):
                # Evidence refers to a canonical NBA game ID, never a name/date-only join.
                match = own[(own.game_id.astype(str) == str(e['game_id'])) & (own.team_abbr == e['team'])]
                match = match[pd.to_datetime(match.game_date).dt.date == start.tz_convert('America/Los_Angeles').date()]
                contradicted = historical[(historical.player_id==int(e['player_id'])) &
                    (historical.game_id.astype(str)==str(e['game_id'])) & historical['min'].gt(0)]
                if len(match) and contradicted.empty:
                    cases.setdefault(str(e['game_id']),set()).add(int(e['player_id']))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    cases = {game for game,ids in cases.items() if set(absent_ids) <= ids}
    matched = own[own.game_id.astype(str).isin(cases)]
    columns = ['min', 'pts', 'reb', 'ast', 'fga', 'fta', 'tov']
    means = matched[columns].mean().to_dict() if len(matched) else None
    # Usage share needs the team's actual opportunity totals, not merely shots
    # per minute. These are descriptive completed-game targets, not features.
    totals = historical.groupby(['game_id','team_abbr'])[['fga','fta','tov','ast']].sum()
    def share(frame):
        rates=[]
        for g in frame.itertuples():
            t=totals.loc[(g.game_id,g.team_abbr)]
            denom=t.fga+.44*t.fta+t.tov
            rates.append((g.fga+.44*g.fta+g.tov)/denom if denom else np.nan)
        return float(np.nanmean(rates)) if rates else None
    return dict(player_id=int(player_id), absent_ids=sorted(absent_ids),
                evidence_games=len(matched), conditional_means=means,
                baseline_last10=own.sort_values('game_date').tail(10)[columns].mean().to_dict() if len(own) else None,
                conditional_rates_per_minute={s:float(matched[s].sum()/matched['min'].sum())
                    for s in ['pts','reb','ast','fga','fta','tov']} if len(matched) else None,
                team_opportunity_share=share(matched),
                baseline_team_opportunity_share=share(own.sort_values('game_date').tail(10)),
                adjustment_validated=False,
                reasons=['No chronologically held-out role-probability validation',
                         'Missing timestamped historical absence coverage' if not len(matched)
                         else 'Conditional sample does not establish a transferable causal adjustment'])
