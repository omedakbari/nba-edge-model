"""Chronological validation of timestamp-verified conditional role estimates."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from src.models.role_context import load_evidence,role_estimate
from src.utils.slate import revision_path,utc_now


def validate(logs,subject,absent,evidence,test_season):
    ids={str(e['game_id']) for e in evidence if int(e['player_id']) in absent}
    test=logs[(logs.player_id==subject)&(logs.season==test_season)&logs['min'].gt(0)&
              logs.game_id.astype(str).isin(ids)].sort_values('game_date')
    rows=[]
    for g in test.itertuples():
        result=role_estimate(logs,subject,absent,g.game_date,evidence)
        if not result['evidence_games']:continue
        # Current OUT assertion was observed before this particular game.
        current=[e for e in evidence if str(e['game_id'])==str(g.game_id) and int(e['player_id']) in absent]
        if not all(pd.Timestamp(e['published_at'])<pd.Timestamp(e['start_time']) for e in current):continue
        row=dict(game_id=g.game_id,date=g.game_date,training_cases=result['evidence_games'])
        team=logs[(logs.game_id==g.game_id)&(logs.team_abbr==g.team_abbr)]
        opportunities=team.fga.sum()+.44*team.fta.sum()+team.tov.sum()
        row.update(opportunity_share=(g.fga+.44*g.fta+g.tov)/opportunities,
                   opportunity_share_role=result['team_opportunity_share'],
                   opportunity_share_baseline=result['baseline_team_opportunity_share'])
        for stat in ['min','pts','reb','ast','fga','fta','tov']:
            row.update({stat:getattr(g,stat),f'{stat}_role':result['conditional_means'][stat],
                        f'{stat}_baseline':result['baseline_last10'][stat]})
        rows.append(row)
    d=pd.DataFrame(rows)
    result=dict(player_id=subject,absent_ids=sorted(absent),test_season=test_season,
                test_n=len(d),predictions=rows,metrics={},approved=False,
                limitations=['Conditional on participation; DNP is not an under outcome',
                             'Selected verified absence games, not a complete league-wide injury archive',
                             'Other simultaneous absences, role and roster changes confound transferability',
                             'Point-stat validation is not validation of prop-line probabilities'])
    for stat in ['min','pts','reb','ast','fga','fta','tov','opportunity_share']:
        if not len(d):continue
        delta=np.abs(d[f'{stat}_role']-d[stat])-np.abs(d[f'{stat}_baseline']-d[stat])
        rng=np.random.default_rng(42)
        boot=delta.to_numpy()[rng.integers(0,len(d),size=(2000,len(d)))].mean(axis=1)
        result['metrics'][stat]=dict(conditional_mae=float(np.abs(d[f'{stat}_role']-d[stat]).mean()),
                    baseline_mae=float(np.abs(d[f'{stat}_baseline']-d[stat]).mean()),
                    paired_mae_difference=float(delta.mean()),interval95=np.quantile(boot,[.025,.975]).tolist())
    result['latest_conditional_estimate']=role_estimate(logs,subject,absent,'2026-10-25',evidence)
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--player-id',type=int,required=True)
    ap.add_argument('--absent-id',type=int,action='append',required=True)
    ap.add_argument('--test-season',default='2025-26')
    args=ap.parse_args()
    evidence=load_evidence(ROOT/'data/role_evidence')
    with sqlite3.connect(f'file:{ROOT/"data/nba_edge.db"}?mode=ro',uri=True) as c:
        logs=pd.read_sql("SELECT * FROM game_logs WHERE game_date<'2026-10-25'",c)
    result=validate(logs,args.player_id,set(args.absent_id),evidence,args.test_season)
    path=revision_path(ROOT/'data/audits/2026-10-25/followup/role_validation.json')
    path.write_text(json.dumps(result,indent=2,allow_nan=False));print(path)


if __name__=='__main__':main()
