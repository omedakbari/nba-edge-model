"""Offline, read-only replay. Writes only versioned files under data/audits.

Saved quotes are replay inputs, never represented as fresh executable prices.
No training, grading, API calls, or production prediction writes occur here.
"""
import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
import numpy as np
import pandas as pd
from scipy.special import logit, expit
from scipy.optimize import minimize
from scipy.stats import norm
from src.models import team_ratings as TR
from src.betting.ev import ev, breakeven
from src.betting.eligibility import approval
from src.utils.slate import revision_path, utc_now
import find_bets as FB
import generate_todays_picks as P


def metrics(frame, col):
    p = np.clip(frame[col].to_numpy(float), .0001, .9999)
    y = frame.y.to_numpy(float)
    bins = []
    for lo in np.arange(0, 1, .1):
        mask = (p >= lo) & (p < lo + .1)
        if mask.any():
            bins.append(dict(lo=round(float(lo), 1), n=int(mask.sum()),
                             prediction=float(p[mask].mean()), observed=float(y[mask].mean())))
    return dict(n=len(y), brier=float(np.mean((p-y)**2)),
                log_loss=float(-np.mean(y*np.log(p)+(1-y)*np.log(1-p))),
                accuracy=float(np.mean((p>.5)==y)),
                ece=float(sum(b['n']*abs(b['prediction']-b['observed']) for b in bins)/len(y)),
                reliability=bins)


def walk_forward(conn, out):
    # Cache only immutable raw tables. Every prediction still calls the actual
    # production engine with an exclusive date bound; no inline rating clone.
    original_load = TR._load_team_games
    all_games = original_load(conn, TR._ALL_SEASONS, ['Regular Season'])
    def cached_load(_conn, seasons, season_types=None, through_date=None):
        d = all_games[all_games.season.isin(seasons)]
        if season_types:
            d = d[d.season_type.isin(season_types)]
        if through_date:
            d = d[d.game_date < pd.Timestamp(through_date)]
        return d.copy()
    original_hca = TR.compute_team_hca
    hca = {s: original_hca(conn, s) for s in ('2024-25', '2025-26')}
    TR._load_team_games = cached_load
    TR.compute_team_hca = lambda c, s: hca[s]
    rows = []
    try:
        for season in ('2024-25', '2025-26'):
            season_games = all_games[(all_games.season == season) & (all_games.is_home == 1)]
            for dt, daily in season_games.groupby('game_date'):
                day = str(dt.date())
                engine = TR.TeamRatingEngine(conn, season, through_date=day)
                # An expanding, date-bounded unconditional home-win baseline.
                past = all_games[(all_games.game_date < dt) & (all_games.is_home == 1)]
                baseline = float((past.margin > 0).mean())
                for g in daily.itertuples():
                    if min(engine._games.loc[engine._games.team == t, 'game_id'].nunique()
                           for t in (g.team, g.opp)) < TR.SRS_MIN_GAMES:
                        continue  # no trustworthy historical cold-start snapshots
                    margin = engine.predict_spread(g.team, g.opp, game_date=day)
                    p = engine.win_probability(g.team, g.opp, game_date=day)
                    rows.append(dict(season=season, date=day, game_id=g.game_id,
                                     home=g.team, away=g.opp, margin=margin, actual_margin=g.margin,
                                     y=int(g.margin>0), legacy=p, raw=float(norm.cdf(margin/TR.SPREAD_STD)),
                                     baseline=baseline))
                if len(rows) and len(rows) % 100 < len(daily):
                    print(f'  Walk-forward: {season} {day}, {len(rows)} games', flush=True)
    finally:
        TR._load_team_games = original_load
        TR.compute_team_hca = original_hca
    d = pd.DataFrame(rows)
    d.to_csv(revision_path(out / 'team_walk_forward.csv'), index=False)
    train, test = d[d.season=='2024-25'].copy(), d[d.season=='2025-26'].copy()
    x, y = logit(np.clip(train.raw, .02, .98)), train.y.to_numpy()
    def loss(v):
        p = np.clip(expit(v[0] + v[1]*x), 1e-8, 1-1e-8)
        return -np.mean(y*np.log(p)+(1-y)*np.log(1-p))
    fit = minimize(loss, [0., TR.WIN_PROB_SCALE], method='BFGS')
    test['research_platt'] = expit(fit.x[0]+fit.x[1]*logit(np.clip(test.raw, .02, .98)))
    result = dict(fit_season='2024-25', test_season='2025-26',
                  fit_success=bool(fit.success), research_only_parameters=fit.x.tolist(),
                  production_calibration_changed=False,
                  limitations=['Post-ten-game regular-season evaluation only; no opening-week validation',
                               'Current-season mutable aggregate snapshots excluded when captured after prediction date',
                               'Previous-season final aggregates assumed available by next season; no archived release snapshots',
                               'Retrospective box-score archive, not contemporaneously archived inputs; design selected after historical seasons'],
                  test={col: metrics(test, col) for col in ('legacy','raw','baseline','research_platt')})
    return result


def replay(conn, day, out):
    saved = json.loads((ROOT/f'data/fair/{day}.json').read_text())
    candidates = json.loads((ROOT/f'data/market_picks/{day}.json').read_text())
    teams = json.loads((ROOT/f'data/team_picks/{day}.json').read_text())['picks']
    selected = [r for r in saved if any(r['player_id']==p['player_id'] for p in candidates['picks'])]
    contexts = []
    for pid in sorted({r['player_id'] for r in selected}):
        rec = next(r for r in selected if r['player_id']==pid)
        g = next(g for g in teams if {g['home'],g['away']} == {rec['team'],rec['opp']})
        home = rec['team']==g['home']
        contexts.append(dict(player_id=pid,team_abbr=rec['team'],opp_abbr=rec['opp'],
                             is_home=int(home),game_id=g['game_id'],
                             matchup=f"{rec['team']}{' vs. ' if home else ' @ '}{rec['opp']}"))
    features = pd.read_parquet(ROOT/'data/processed/features.parquet')
    with redirect_stdout(StringIO()):
        df = P.build_todays_features(features, contexts, day, season=candidates['season'])
        preds = {s:P.predict_stat_raw(df,s) for s in ('pts','reb','ast','fg3m')}
    input_differences = {}
    baseline_source = out/'source_before/scripts/generate_todays_picks.py'
    if baseline_source.exists():
        spec = importlib.util.spec_from_file_location('original_oct25_pipeline', baseline_source)
        original = importlib.util.module_from_spec(spec);spec.loader.exec_module(original)
        with redirect_stdout(StringIO()):
            before = original.build_todays_features(features, contexts, day, season=candidates['season'])
        for pid in df.player_id:
            old, new = before[before.player_id==pid].iloc[0], df[df.player_id==pid].iloc[0]
            fields = ['game_id','season_game_num','pace_L15','own_pace_L15','team_abbr','opp_abbr','is_home','is_playoff']
            input_differences[str(pid)] = {f:dict(before=str(old.get(f)),after=str(new.get(f)))
                                            for f in fields if str(old.get(f))!=str(new.get(f))}
    comps = {'pra':['pts','reb','ast'],'reb_ast':['reb','ast']}
    results = []
    for pick in candidates['picks']:
        rec = dict(next(r for r in saved if r['player_id']==pick['player_id'] and r['stat']==pick['stat_key']))
        idx = df.index[df.player_id==pick['player_id']][0]
        row = df.loc[idx]; stat=rec['stat']
        raw = sum(preds[s].loc[idx] for s in comps.get(stat,[stat]))
        corrected_pred = round(FB.BLEND_L10_WEIGHT*row[f'{stat}_L10']+(1-FB.BLEND_L10_WEIGHT)*raw,2)
        original_p = FB.fair_p(rec,pick['line'],pick['direction'].lower())
        rec.update(pred=corrected_pred,std=float(row[f'{stat}_std_L10']))
        p = FB.fair_p(rec,pick['line'],pick['direction'].lower())
        dec=pick['decimal_odds']
        results.append(dict(player=pick['player_name'],stat=stat,line=pick['line'],side=pick['direction'],
                            original_prediction=pick['model_pred'],replay_original_probability=original_p,
                            saved_probability=pick['prob'],corrected_prediction=corrected_pred,
                            corrected_probability=p,break_even=breakeven(dec),
                            original_ev=pick['ev'],corrected_ev=ev(p,dec),probability_advantage=p-1/dec,
                            approval=approval(rec,p,dec),
                            limitation='Corrected diagnostic replay using saved rounded quotes; not validated or executable'))
    df.to_json(revision_path(out/'corrected_features.json'),orient='records',indent=2,date_format='iso')
    # Inspect association, participation, and data-population bias without
    # making a causal numerical injury adjustment.
    logs = pd.read_sql('SELECT gl.*,p.full_name FROM game_logs gl JOIN players p USING(player_id) '
                       'WHERE game_date<?',conn,params=[day])
    role = {}
    for pid in sorted({r['player_id'] for r in selected}):
        history = logs[logs.player_id==pid].sort_values('game_date').tail(10)
        role[str(pid)] = dict(n=len(history),
                             last10_all_appearances=history[['min','pts','reb','ast','fga','fta','tov']].mean().to_dict(),
                             short_minutes_fraction=float((history['min']<12).mean()))
    # Name-based descriptive research only. Model code never hardcodes injuries.
    w = logs[(logs.full_name=='Franz Wagner') & (logs.season.isin(['2025-26','2026-27']))]
    partner = set(logs[(logs.full_name=='Paolo Banchero') & (logs['min']>0)].game_id)
    role['wagner_with_without_banchero'] = {}
    for label, mask in [('appeared',w.game_id.isin(partner)),('no_recorded_appearance',~w.game_id.isin(partner))]:
        g=w[mask]
        role['wagner_with_without_banchero'][label]=dict(n=len(g),means=g[['min','pts','reb','ast','fga','fta','tov']].mean().to_dict())
    role['caution']='Absence of a recorded appearance is not verified injury status. Observational averages are not causal adjustments or expected usage.'
    integrity = dict(matchup_mismatches=int((logs.matchup != logs.team_abbr+np.where(logs.is_home==1,' vs. ',' @ ')+logs.opp_abbr).sum()),
                     min_filter_excluded_fraction=float(((logs['min']>0)&(logs['min']<12)).sum()/(logs['min']>0).sum()),
                     current_team_snapshot=conn.execute('SELECT min(updated_at),max(updated_at) FROM team_stats WHERE season=?',(candidates['season'],)).fetchone())
    engine=TR.TeamRatingEngine(conn,candidates['season'],through_date=day,
                               as_of=candidates['generated_at'])
    team_replay=[]
    for g in teams:
        margin=engine.predict_spread(g['home'],g['away'],game_date=day)
        p=engine.win_probability(g['home'],g['away'],game_date=day)
        team_replay.append(dict(home=g['home'],away=g['away'],expected_home_margin=margin,
                                original_margin=g['model_spread'],probability=p,original_probability=g['model_wp'],
                                probability_validated=False,approved=False,stake_pct=0))
    return dict(date=day,generated_at=utc_now(),replay_kind='offline corrected diagnostic',
                original_vs_corrected_inputs=input_differences,
                original_generated_at=candidates['generated_at'],candidates=results,
                teams=team_replay,roles=role,integrity=integrity,approved_picks=0,
                reproduction_limitations=['No archived raw player quotes, roster snapshot, or timestamped injury payload at forecast time',
                                          'Saved team prices have capture time but not provider quote-update time',
                                          'Current roster/started-game recheck unavailable without network; approval fails closed'])


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--date',default='2026-10-25')
    ap.add_argument('--walk-forward',action='store_true')
    args=ap.parse_args()
    out=ROOT/'data/audits'/args.date;out.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(f'file:{ROOT / "data/nba_edge.db"}?mode=ro',uri=True) as conn:
        result=replay(conn,args.date,out)
        if args.walk_forward:
            result['walk_forward']=walk_forward(conn,out)
    manifest=out/'original_manifest.json'
    if manifest.exists():
        result['original_artifacts_unchanged']={p: hashlib.file_digest(open(ROOT/p,'rb'),'sha256').hexdigest()==h
                                               for p,h in json.loads(manifest.read_text()).items()}
        assert all(result['original_artifacts_unchanged'].values()), 'Original artifact changed'
    path=revision_path(out/'corrected_replay.json')
    path.write_text(json.dumps(result,indent=2,allow_nan=False))
    print(f'Wrote {path}; approved picks: 0')


if __name__=='__main__':
    main()
