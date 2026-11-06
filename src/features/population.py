"""Version 2 target population: all appearances, pregame rotation eligibility.

Zero-minute rows are participation outcomes, never statistical under wins.
Team features always aggregate every appearance, including bench minutes.
"""
import pandas as pd
from src.features.player_features import build_player_features
from src.features.context_features import add_context_features
from src.features.team_features import add_team_features
from src.features.playoff_features import add_playoff_features

VERSION = 'appearance-v2'


def appearance_features(logs):
    d = logs[logs['min'].gt(0)].copy()
    d['game_date'] = pd.to_datetime(d['game_date'])
    d = build_player_features(d)
    d = add_context_features(d)
    d = add_team_features(d)
    return add_playoff_features(d)


def pregame_rotation(d, min_average=25.0):
    """Current game's minutes MUST NOT appear in this eligibility decision."""
    return d['min_L10'].ge(min_average) & d['pts_L10'].notna()


def appearance_prediction_features(logs, contexts, target_date, season):
    """Same player windows/context and full-team population as version-2 training.

    Today's outcomes remain unknown. Team aggregates are evaluated directly
    as of the cutoff, rather than fabricating a future box score or roster.
    """
    cutoff=pd.Timestamp(target_date)
    history=logs[(pd.to_datetime(logs.game_date)<cutoff)&logs['min'].gt(0)].copy()
    history['game_date']=pd.to_datetime(history.game_date)
    if len({c['player_id'] for c in contexts})!=len(contexts):
        raise ValueError('Ambiguous prediction contexts')
    future=[]
    for c in contexts:
        past=history[history.player_id==c['player_id']].sort_values('game_date')
        if past.empty:continue
        row=past.iloc[-1].to_dict();row.update(c,game_date=cutoff,season=season,season_type='Regular Season')
        for col in ['min','pts','reb','ast','stl','blk','fg3m','pra','pts_reb','reb_ast','pts_ast',
                    'fga','fg3a','fta','tov','pf']:
            row[col]=float('nan')
        future.append(row)
    if not future:return pd.DataFrame()
    selected=history[history.player_id.isin([r['player_id'] for r in future])]
    d=build_player_features(pd.concat([selected,pd.DataFrame(future)],ignore_index=True))
    d=add_context_features(d)
    d=d[d.game_date==cutoff].copy()
    defensive=history.groupby(['game_date','game_id','opp_abbr'])[['pts','reb','ast','fg3m']].sum().reset_index()
    team=history.groupby(['game_date','game_id','team_abbr'])[['fga','tov','fta']].sum().reset_index()
    team['pace']=team.fga+team.tov+.44*team.fta
    for idx,row in d.iterrows():
        defense=defensive[defensive.opp_abbr==row.opp_abbr].sort_values(['game_date','game_id']).tail(15)
        for stat in ['pts','reb','ast','fg3m']:
            d.loc[idx,f'{stat}_allowed_L15']=defense[stat].mean() if len(defense)>=5 else float('nan')
        for field,abbr in [('pace_L15',row.opp_abbr),('own_pace_L15',row.team_abbr)]:
            g=team[team.team_abbr==abbr].sort_values(['game_date','game_id']).tail(15)
            d.loc[idx,field]=g.pace.mean() if len(g)>=5 else float('nan')
    return d.reset_index(drop=True)


def settlement_outcome(actual, line, side, *, participated, dnp_rule='void'):
    if not participated:
        if dnp_rule == 'void':
            return 'void'
        return 'review'  # venue-specific rule must be explicitly implemented
    if actual is None or side not in ('over', 'under'):
        return 'review'
    if actual == line:
        return 'push'
    return 'hit' if (actual > line) == (side == 'over') else 'miss'
