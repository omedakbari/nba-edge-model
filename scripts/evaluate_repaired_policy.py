"""Fixed-policy economics on saved temporal holdout; never executable approvals."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
import numpy as np
import pandas as pd
from backtest_props_vs_market import load_market,_norm_name
from src.utils.slate import revision_path


def summarize(d):
    if d.empty:return {'n':0}
    returns=d.actual_hit*d.decimal-1
    aggregate=pd.DataFrame(dict(day=d.game_date,profit=returns)).groupby('day').profit.agg(['sum','count'])
    rng=np.random.default_rng(42)
    chosen=rng.integers(0,len(aggregate),size=(2000,len(aggregate)))
    boot=aggregate['sum'].to_numpy()[chosen].sum(axis=1)/aggregate['count'].to_numpy()[chosen].sum(axis=1)
    return dict(n=len(d),dates=d.game_date.nunique(),mean_probability=float(d.p.mean()),
                realized_hit_rate=float(d.actual_hit.mean()),mean_expected_return=float(d.ev.mean()),
                hypothetical_unit_roi=float(returns.mean()),roi_interval95=np.quantile(boot,[.025,.975]).tolist())


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--evaluation',type=Path,required=True)
    args=ap.parse_args()
    d=pd.read_parquet(args.evaluation/'player_holdout.parquet')
    with sqlite3.connect(f'file:{ROOT/"data/nba_edge.db"}?mode=ro',uri=True) as c:
        names=dict(c.execute('SELECT player_id,full_name FROM players'))
        market=load_market(c,(d.game_date.min().strftime('%Y-%m-%d'),d.game_date.max().strftime('%Y-%m-%d')))
    d['name_key']=d.player_id.map(names).map(_norm_name)
    keys=['game_date','name_key','stat_key','line']
    price_cols=keys+['best_over','best_under']
    if market.duplicated(keys).any():raise ValueError('Ambiguous player/event price identity')
    d=d.merge(market[price_cols],on=keys,validate='many_to_one')
    legs=[]
    for side in ('over','under'):
        r=d.copy();r['side']=side
        r['p']=r.v2_p if side=='over' else 1-r.v2_p
        r['decimal']=r['best_'+side]
        r['actual_hit']=(r.actual>r.line).astype(int) if side=='over' else (r.actual<r.line).astype(int)
        r['ev']=r.p*r.decimal-1;r['kea_margin']=r.p-1/r.decimal
        legs.append(r)
    legs=pd.concat(legs,ignore_index=True)
    ev_only=legs[legs.ev>=.04].copy()
    screened=ev_only[ev_only.kea_margin>=.03].copy()
    singles=screened.sort_values('ev',ascending=False).drop_duplicates(['player_id','game_date'])
    report=dict(policy=dict(min_ev=.04,kea_probability_margin=.03),
                ev_only=summarize(ev_only),ev_and_kea=summarize(screened),
                one_candidate_per_player_date=summarize(singles),
                overlapping_player_dates=int(screened.duplicated(['player_id','game_date'],keep=False).groupby(
                    [screened.player_id,screened.game_date]).any().sum()) if len(screened) else 0,
                approved_picks=[],maximum_entry_cost=0,
                limitations=['Historical opening quotes have no verified issue timestamp or guaranteed concurrent cross-book execution',
                             'Some side labels were inferred; returns are not executable profitability evidence',
                             'Conditional on appearance; DNP-void assumption, pushes excluded',
                             'No stake allocation or combination recommendation; ROI is hypothetical one-unit arithmetic',
                             'No extra venue fees included; exact settlement and all-in cost require verification'])
    path=revision_path(args.evaluation/'policy_evaluation.json');path.write_text(json.dumps(report,indent=2))
    screened.to_csv(revision_path(args.evaluation/'policy_candidates.csv'),index=False)
    print(path)


if __name__=='__main__':main()
