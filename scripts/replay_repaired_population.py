"""Replay saved lines through the versioned population model, never current picks."""
import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import pickle
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from scipy.special import logit
from scipy.stats import norm
from src.features.population import appearance_prediction_features
from src.betting.eligibility import approval
from src.betting.ev import ev
from src.utils.slate import utc_now,revision_path


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bundle',type=Path,required=True);ap.add_argument('--date',default='2026-10-25')
    args=ap.parse_args()
    with open(args.bundle,'rb') as f:bundle=pickle.load(f)
    fair=json.loads((ROOT/f'data/fair/{args.date}.json').read_text())
    payload=json.loads((ROOT/f'data/market_picks/{args.date}.json').read_text())
    teams=json.loads((ROOT/f'data/team_picks/{args.date}.json').read_text())['picks']
    contexts=[]
    for pid in {p['player_id'] for p in payload['picks']}:
        rec=next(p for p in payload['picks'] if p['player_id']==pid)
        game=next(g for g in teams if {g['home'],g['away']}=={rec['team'],rec['opp']})
        contexts.append(dict(player_id=pid,game_id=game['game_id'],team_abbr=rec['team'],
                             opp_abbr=rec['opp'],is_home=int(rec['team']==game['home'])))
    with sqlite3.connect(f'file:{ROOT/"data/nba_edge.db"}?mode=ro',uri=True) as c:
        logs=pd.read_sql('SELECT g.*,p.full_name FROM game_logs g JOIN players p USING(player_id) '
                         'WHERE game_date<?',c,params=[args.date])
    with redirect_stdout(StringIO()):
        frame=appearance_prediction_features(logs,contexts,args.date,payload['season'])
    X=frame[bundle['features']]
    preds={s:bundle['models'][s].predict(X) for s in ['pts','reb','ast','fg3m','min']}
    risk=bundle['models']['minutes_risk'].predict_proba(X)[:,1]
    results=[]
    for old in payload['picks']:
        stat=old['stat_key'];idx=frame.index[frame.player_id==old['player_id']][0]
        anchor=next(r for r in fair if r['player_id']==old['player_id'] and r['stat']==stat)
        blend=bundle['models'].get(stat+'_blend')
        rec=dict(player=old['player_name'],stat=stat,line=old['line'],side=old['direction'],
                 original_probability=old['prob'],expected_minutes=float(preds['min'][idx]),
                 short_minutes_risk=float(risk[idx]),approved=False,maximum_entry_cost=0)
        if blend is None or old['line']!=anchor['book_line']:
            rec.update(probability=None,reasons=['No supported temporally fitted calibration at this exact market/line'])
        else:
            comps={'pra':['pts','reb','ast'],'reb_ast':['reb','ast']}.get(stat,[stat])
            pred=sum(preds[s][idx] for s in comps)
            sigma=frame.loc[idx,f'{stat}_std_L10']
            if not np.isfinite(sigma) or sigma<=0:sigma=max(abs(pred)*.2,1)
            raw=float(np.clip(norm.sf((old['line']-pred)/sigma),.01,.99))
            market=float(np.clip(anchor['book_p_over'],.01,.99))
            features=pd.DataFrame([dict(market_logit=logit(market),model_disagreement=logit(raw)-logit(market),
                                        minutes_risk=risk[idx])])
            over=float(blend.predict_proba(features)[0,1]);p=over if old['direction']=='OVER' else 1-over
            gate=approval({},p,old['decimal_odds'])
            rec.update(prediction=float(pred),probability=p,diagnostic_ev_at_saved_price=ev(p,old['decimal_odds']),
                       probability_advantage=p-1/old['decimal_odds'],approval=gate)
        results.append(rec)
    result=dict(kind='historical saved-line research replay; NOT current pregame recommendations',
                generated_at=utc_now(),date=args.date,source_forecast_time=payload['generated_at'],
                research_bundle=str(args.bundle),injury_adjustment_applied=False,
                candidates=results,approved_picks=[],maximum_entry_cost=0)
    path=revision_path(ROOT/'data/audits/2026-10-25/followup/population_v2_replay.json')
    path.write_text(json.dumps(result,indent=2,allow_nan=False));print(path)


if __name__=='__main__':main()
