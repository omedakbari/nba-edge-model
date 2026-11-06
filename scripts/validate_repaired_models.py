"""Versioned temporal research for appearance population and cold-start teams.

Original models, DB, features, forecasts and grades are never written. No
artifact is promoted automatically from an exploratory holdout comparison.
"""
import argparse
from contextlib import redirect_stdout
from io import StringIO
import hashlib
import json
import pickle
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
import numpy as np
import pandas as pd
from scipy.special import expit,logit
from scipy.stats import norm
from sklearn.ensemble import HistGradientBoostingRegressor,HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from src.features.population import appearance_features,pregame_rotation,VERSION
from src.models import team_ratings as TR
from src.utils.slate import utc_now,revision_path
from audit_daily_run import metrics
from backtest_props_vs_market import load_market,_norm_name

STATS=['pts','reb','ast','fg3m']
COMPOSITES={'pra':['pts','reb','ast'],'reb_ast':['reb','ast']}


def paired_loss_interval(d,model,baseline,y='y',group='game_date',seed=42):
    """Paired date-cluster bootstrap; correlated slate observations stay together."""
    p=np.clip(d[model].to_numpy(float),1e-6,1-1e-6)
    q=np.clip(d[baseline].to_numpy(float),1e-6,1-1e-6)
    actual=d[y].to_numpy(float)
    result={}
    for name,delta in [('brier',(p-actual)**2-(q-actual)**2),
                       ('log_loss',-actual*np.log(p)-(1-actual)*np.log(1-p)
                        +actual*np.log(q)+(1-actual)*np.log(1-q))]:
        agg=pd.DataFrame(dict(group=d[group].to_numpy(),loss=delta)).groupby('group').loss.agg(['sum','count'])
        rng=np.random.default_rng(seed)
        chosen=rng.integers(0,len(agg),size=(2000,len(agg)))
        boot=agg['sum'].to_numpy()[chosen].sum(axis=1)/agg['count'].to_numpy()[chosen].sum(axis=1)
        result[name]=dict(model_minus_baseline=float(delta.mean()),
                          interval95=np.quantile(boot,[.025,.975]).tolist(),clusters=len(agg))
    return result


def scored(d,col):
    if not len(d):return {'n':0}
    return metrics(d.rename(columns={'over_hit':'y'}) if 'over_hit' in d else d,col)


def binomial_metrics(p,y):
    return metrics(pd.DataFrame(dict(p=np.asarray(p),y=np.asarray(y))), 'p')


def player_validation(conn,out):
    print('Building appearance-v2 features from complete positive-minute box scores',flush=True)
    logs=pd.read_sql('SELECT g.*,p.full_name FROM game_logs g JOIN players p USING(player_id) '
                     "WHERE game_date<'2026-10-25' AND season>='2021-22' ORDER BY player_id,game_date",conn)
    with redirect_stdout(StringIO()):
        d=appearance_features(logs)
    d['name_key']=d.full_name.map(_norm_name)
    d['eligible']=pregame_rotation(d)
    cohorts=d[d.eligible & (d.season_type=='Regular Season')].copy()
    report={'version':VERSION,'target':'statistics conditional on positive-minute participation; DNP void separately',
            'population':{},'training_through':'2023-24','calibration':'2024-25','test':'2025-26'}
    for season,g in cohorts.groupby('season'):
        report['population'][season]=dict(n=len(g),under12=int((g['min']<12).sum()),
            under12_fraction=float((g['min']<12).mean()),
            pts_all=float(g.pts.mean()),pts_after_target_filter=float(g[g['min']>=12].pts.mean()))
    # Explicit feature schema: no current minutes, scores, status or future roster.
    feature_names=[f'{s}_L{w}' for s in STATS+['min','fga','fta','tov'] for w in [5,10,20]]
    feature_names+=['min_std_L10','min_trend','low_min_count_L5','days_rest','is_b2b','is_home',
                    'game_month','own_pace_L15','pace_L15','pts_allowed_L15','reb_allowed_L15',
                    'ast_allowed_L15','fg3m_allowed_L15']
    train=cohorts[cohorts.season<='2023-24'].copy()
    cal=cohorts[cohorts.season=='2024-25'].copy()
    test=cohorts[cohorts.season=='2025-26'].copy()
    # Fixed recipe, no test-driven tuning. Only the affected four component
    # models and new minutes models are fitted; old composite artifacts untouched.
    models={}
    for stat in STATS+['min']:
        model=HistGradientBoostingRegressor(max_iter=100,max_depth=3,min_samples_leaf=40,
                    l2_regularization=5,learning_rate=.05,early_stopping=False,random_state=42)
        model.fit(train[feature_names],train[stat])
        models[stat]=model
        for g in (cal,test):g[f'{stat}_pred_v2']=model.predict(g[feature_names])
        print(f'  fitted appearance-v2 {stat}, {len(train)} training appearances',flush=True)
    minutes=HistGradientBoostingClassifier(max_iter=100,max_depth=2,min_samples_leaf=80,
                          l2_regularization=5,early_stopping=False,random_state=42)
    minutes.fit(train[feature_names],(train['min']<12).astype(int))
    for g in (cal,test):g['short_minutes_p']=minutes.predict_proba(g[feature_names])[:,1]
    baseline=float((train['min']<12).mean())
    report['minutes_test']={'model':binomial_metrics(test.short_minutes_p,test['min']<12),
                             'constant_train_rate':binomial_metrics(np.full(len(test),baseline),test['min']<12),
                             'minutes_mae':float(np.abs(test['min']-test.min_pred_v2).mean())}
    # Quantify the population error with the same recipe fit on the filtered
    # target. This is one affected-model ablation, not unrelated retraining.
    selected=train[train['min']>=12]
    filtered=HistGradientBoostingRegressor(max_iter=100,max_depth=3,min_samples_leaf=40,
                    l2_regularization=5,learning_rate=.05,early_stopping=False,random_state=42)
    filtered.fit(selected[feature_names],selected.pts)
    test['pts_pred_filtered_ablation']=filtered.predict(test[feature_names])
    report['target_filter_ablation']={}
    for label,g in [('all',test),('sub12',test[test['min']<12]),('at_least12',test[test['min']>=12])]:
        report['target_filter_ablation'][label]={'n':len(g),
            'appearance_v2_pts_mae':float(np.abs(g.pts-g.pts_pred_v2).mean()),
            'filtered_target_pts_mae':float(np.abs(g.pts-g.pts_pred_filtered_ablation).mean()),
            'appearance_v2_pts_bias':float((g.pts_pred_v2-g.pts).mean()),
            'filtered_target_pts_bias':float((g.pts_pred_filtered_ablation-g.pts).mean())}
    market=load_market(conn,('2024-10-01','2026-06-11'))
    # Validate ESPN event orientation using archived game identity; reject name
    # ambiguity before joining. Both statistical lines are still historical.
    events=pd.read_sql('SELECT DISTINCT espn_event_id,game_date,home_abbr,away_abbr FROM historical_odds',conn)
    events['game_date']=pd.to_datetime(events.game_date)
    market=market.merge(events,on=['espn_event_id','game_date'],validate='many_to_one')
    combined=pd.concat([cal,test],ignore_index=True)
    identity=['name_key','game_date']
    if combined.duplicated(identity).any():raise ValueError('Ambiguous player/date identity')
    boards=[]
    for stat in STATS+list(COMPOSITES):
        m=market[market.stat_key==stat]
        g=m.merge(combined,on=identity,validate='many_to_one')
        g=g[((g.team_abbr==g.home_abbr)&(g.opp_abbr==g.away_abbr)&(g.is_home==1))|
            ((g.team_abbr==g.away_abbr)&(g.opp_abbr==g.home_abbr)&(g.is_home==0))].copy()
        pred=sum(g[f'{s}_pred_v2'] for s in COMPOSITES.get(stat,[stat]))
        g['model_prediction']=pred
        sigma=g.get(f'{stat}_std_L10',pd.Series(np.nan,index=g.index))
        sigma=sigma.where(sigma.gt(0),np.maximum(pred.abs()*.2,1)).fillna(1)
        g['raw_p']=np.clip(norm.sf((g.line-pred)/sigma),.01,.99)
        g['actual']=g[stat]
        g=g[g.actual!=g.line].copy()  # pushes are reported separately, never a loss
        g['over_hit']=(g.actual>g.line).astype(int)
        tr=g[g.season=='2024-25'];te=g[g.season=='2025-26'].copy()
        if len(tr)<100 or len(te)<100:
            report.setdefault('unsupported_markets',{})[stat]=dict(calibration_n=len(tr),test_n=len(te),
                                                                  reason='Insufficient temporal calibration pairs')
            continue
        columns=['market_logit','model_disagreement','minutes_risk']
        for frame in (tr,te):
            frame['market_logit']=logit(np.clip(frame.mkt_p_over,.01,.99))
            frame['model_disagreement']=logit(frame.raw_p)-frame.market_logit
            frame['minutes_risk']=frame.short_minutes_p
        blend=LogisticRegression(C=1,max_iter=1000).fit(tr[columns],tr.over_hit)
        te['v2_p']=blend.predict_proba(te[columns])[:,1]
        report.setdefault('props_test',{})[stat]={'calibration_n':len(tr),'v2':scored(te,'v2_p'),
                   'market':scored(te,'mkt_p_over'),'raw':scored(te,'raw_p'),
                   'sub12':{c:scored(te[te['min']<12],c) for c in ['v2_p','mkt_p_over']},
                   'paired_market_comparison':paired_loss_interval(te,'v2_p','mkt_p_over',y='over_hit'),
                   'parameters':dict(intercept=blend.intercept_.tolist(),coef=blend.coef_.tolist())}
        models[f'{stat}_blend']=blend
        boards.append(te[['player_id','game_date','season','stat_key','line','actual','min',
                          'mkt_p_over','raw_p','v2_p','short_minutes_p']])
    pd.concat(boards).to_parquet(out/'player_holdout.parquet',index=False)
    models['minutes_risk']=minutes
    with open(out/'appearance_v2_research.pkl','wb') as f:
        pickle.dump(dict(models=models,features=feature_names,version=VERSION,
                         training_through='2023-24',calibration_season='2024-25',
                         deployed=False,probability_validated=False),f)
    report['deployment']='Research only: pending role evidence, exact quote-side validation and held-out calibration assessment'
    report['limitations']=['Historical opening prop sides after 2024-25 may use an inferred ordering',
                          'Historical quote issue times unavailable; opening designation is not an archived as-of fetch',
                          'This is retrospective temporal validation; no causal injury model is implied',
                          'DNP coverage incomplete; probabilities conditional on appearance require a venue DNP-void rule']
    return report


def team_validation(conn,out):
    all_games=TR._load_team_games(conn,TR._ALL_SEASONS,['Regular Season'])
    load,hca_fn=TR._load_team_games,TR.compute_team_hca
    seasons=['2023-24','2024-25','2025-26']
    hca={s:hca_fn(conn,s) for s in seasons}
    def cached(_c,ss,season_types=None,through_date=None):
        d=all_games[all_games.season.isin(ss)]
        if through_date:d=d[d.game_date<pd.Timestamp(through_date)]
        return d.copy()
    odds=pd.read_sql('SELECT * FROM historical_odds WHERE open_home_ml IS NOT NULL AND open_away_ml IS NOT NULL',conn)
    # Stable book priority chosen without observing outcome/probability.
    odds['dk']=~odds.provider.str.contains('Draft',case=False)
    odds=odds.sort_values(['dk','provider_priority','provider']).drop_duplicates(['game_date','home_abbr','away_abbr'])
    def implied(x):return np.where(x>0,100/(x+100),-x/(-x+100))
    oh,oa=implied(odds.open_home_ml),implied(odds.open_away_ml)
    odds['book']=oh/(oh+oa)
    odds=odds[(odds.open_home_ml.abs()>=100)&(odds.open_away_ml.abs()>=100)&((oh+oa)>1)&((oh+oa)<1.2)]
    book={(g.game_date,g.home_abbr,g.away_abbr):(g.book,g.provider) for g in odds.itertuples()}
    TR._load_team_games=cached;TR.compute_team_hca=lambda c,s:hca[s]
    rows=[]
    try:
        for season in seasons:
            current=all_games[all_games.season==season]
            first=current.game_date.min()
            # Only cold-start games require a new replay; later-season original
            # evaluation already exists. Cohort includes first 10 games of either team.
            for dt,daily in current[current.is_home==1].groupby('game_date'):
                prior=current[current.game_date<dt]
                counts=prior.groupby('team').game_id.nunique().to_dict()
                early=daily[[min(counts.get(g.team,0),counts.get(g.opp,0))<10 for g in daily.itertuples()]]
                if early.empty:continue
                day=str(dt.date())
                engine=TR.TeamRatingEngine(conn,season,through_date=day,aggregate_mode='game_logs')
                old=all_games[(all_games.game_date<dt)&(all_games.is_home==1)]
                base=float((old.margin>0).mean())
                for g in early.itertuples():
                    if not all(engine.has_rating_history(t) for t in (g.team,g.opp)):continue
                    margin=engine.predict_spread(g.team,g.opp,game_date=day)
                    bp,provider=book.get((day,g.team,g.opp),(np.nan,None))
                    rows.append(dict(season=season,date=day,home=g.team,away=g.opp,
                      home_games=counts.get(g.team,0),away_games=counts.get(g.opp,0),
                      opening_week=(dt-first).days<7,y=int(g.margin>0),margin=margin,
                      raw=np.clip(norm.cdf(margin/TR.SPREAD_STD),.02,.98),
                      legacy=engine.win_probability(g.team,g.opp,game_date=day),
                      baseline=base,book=bp,book_provider=provider))
            print(f'  cold-start replay complete: {season}',flush=True)
    finally:TR._load_team_games=load;TR.compute_team_hca=hca_fn
    d=pd.DataFrame(rows)
    train=d[d.season<'2025-26'];test=d[d.season=='2025-26'].copy()
    fit=LogisticRegression(C=1,max_iter=1000).fit(logit(train[['raw']]),train.y)
    test['calibrated']=fit.predict_proba(logit(test[['raw']]))[:,1]
    report={'rating_version':'game-log cold-start v2; no mutable team_stats inputs',
            'train_seasons':['2023-24','2024-25'],'test_season':'2025-26','calibration_fit_n':len(train),
            'calibration_parameters':dict(intercept=fit.intercept_.tolist(),slope=fit.coef_.tolist()),
            'cohorts':{},'production_deployed':False,
            'limitations':['Historical bookmaker opening quotes have no original issue timestamps',
                           'Injury/roster changes not explicitly modeled',
                           'Retrospective holdout, not a prospective model-selection test']}
    for label,tr,te in [('opening_week',train[train.opening_week],test[test.opening_week]),
                         ('first_ten_games',train,test)]:
        matched=te[te.book.notna()]
        report['cohorts'][label]={'train_n':len(tr),'test_n':len(te),'book_matched_n':len(matched),
             'all':{c:scored(te,c) for c in ['legacy','raw','calibrated','baseline']},
             'matched':{c:scored(matched,c) for c in ['legacy','calibrated','baseline','book']}}
        report['cohorts'][label]['paired_market_comparison']=paired_loss_interval(
            matched,'calibrated','book',group='date')
    d.to_csv(out/'team_cold_start_predictions.csv',index=False)
    test.to_csv(out/'team_cold_start_holdout.csv',index=False)
    return report


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--players',action='store_true');ap.add_argument('--teams',action='store_true')
    ap.add_argument('--summarize',type=Path,help='extend saved evaluation with paired uncertainty; no fitting')
    args=ap.parse_args()
    if args.summarize:
        out=args.summarize
        report=json.loads((out/'validation.json').read_text())
        if 'players' in report:
            d=pd.read_parquet(out/'player_holdout.parquet')
            d['over_hit']=(d.actual>d.line).astype(int)
            for stat,g in d.groupby('stat_key'):
                report['players']['props_test'][stat]['paired_market_comparison']=paired_loss_interval(
                    g,'v2_p','mkt_p_over',y='over_hit')
        else:
            d=pd.read_csv(out/'team_cold_start_holdout.csv')
            for label,g in [('opening_week',d[d.opening_week]),('first_ten_games',d)]:
                report['teams']['cohorts'][label]['paired_market_comparison']=paired_loss_interval(
                    g[g.book.notna()],'calibrated','book',group='date')
        path=revision_path(out/'validation.extended.json')
        path.write_text(json.dumps(report,indent=2,allow_nan=False));print(path)
        return
    directory=ROOT/'data/audits/2026-10-25/followup'
    out=directory/utc_now().replace(':','').replace('+','_');out.mkdir(parents=True)
    report={'generated_at':utc_now(),'output_dir':str(out)}
    with sqlite3.connect(f'file:{ROOT/"data/nba_edge.db"}?mode=ro',uri=True) as conn:
        if args.players:report['players']=player_validation(conn,out)
        if args.teams:report['teams']=team_validation(conn,out)
    (out/'validation.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    print(f'Validation saved: {out}',flush=True)


if __name__=='__main__':main()
