"""
Ablation study — STAR PTS confidence inversion.

Diagnostic summary (from saved playoff_backtest.csv, STAR PTS only):
  Band breakdown:
    [50%,55%): n=222, hit=50.5%, act/l10=0.909, res_med=+0.62   ← borderline
    [55%,60%): n=45,  hit=55.6%, act/l10=0.965, res_med=-0.17   ← ok
    [60%,65%): n=143, hit=58.0%, act/l10=1.051, res_med=-0.08   ← GOOD (working)
    [65%,70%): n=129, hit=39.5%, act/l10=0.833, res_med=+5.11   ← BAD (target)
    [70%,80%): n=74,  hit=62.2%, act/l10=1.029, res_med=+1.29   ← fine
    [80%,100%): n=25, hit=76.0%, act/l10=1.202, res_med=-0.56   ← fine

  Root cause: high elevation ratios (~1.13) push picks into 65-70% band.
    PRED_MULT=0.94 discounts prediction, but elev_ratio×0.94 can overshoot.
    p_over correlates with pred/l10 (r=+0.955) but NOT actual/l10 (r=+0.193).
    High-L10 STAR players (above median 24.4 pts) drive the inversion:
      L10 < 24.4 pts: P≥55% hit=58.4%, P≥65% hit=59.2%  [monotone, fine]
      L10 ≥ 24.4 pts: P≥55% hit=48.7%, P≥65% hit=39.8%  [inverted, broken]

  Mechanism: elevation ratios were earned in exceptional past seasons.
    For superstars, those seasons are not predictive going forward — opponent
    focus, age, load management in deep series all compress actual output.
    The synthetic line already discounts via PLAYOFF_LINE_RATIO=0.909 (91% of L10).
    Adding elev_ratio=1.13 on top of PRED_MULT=0.94 → effective mult=1.064×L10,
    which sits above L10 while actual for this group averages 0.833×L10.

Configs tested:
  0. Baseline:        STAR PTS elev cap=1.25 (current), STD_MULT=1.20
  A. No elev:         STAR PTS elev cap=1.00 (no positive elevation)
  B. Cap 1.05:        STAR PTS elev cap=1.05 (allow 5% boost)
  C. Cap 1.10:        STAR PTS elev cap=1.10 (allow 10% boost)
  D. STD wide:        elev cap=1.25 (unchanged), STD_MULT STAR=1.35
  E. Combined B+STD:  elev cap=1.05, STD_MULT STAR=1.25

Primary metric: monotonicity of STAR PTS hit rate across confidence bands.
Secondary: overall P≥55% and P≥65% hit rates, total N (want to preserve volume).

Usage:
    python scripts/ablation_star_pts.py [--save]
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

SEASONS    = ['2024-25', '2023-24', '2022-23', '2021-22']
THRESHOLDS = [0.55, 0.60, 0.65, 0.70]
BANDS      = [(0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.80), (0.80, 1.01)]


CONFIGS = {
    '0_baseline': {
        'label':             'Baseline (92899eb) — elev cap 1.25, STD 1.20',
        'star_pts_elev_cap': 1.25,
        'star_std_mult':     None,   # use harness default (1.20)
    },
    'A_no_elev': {
        'label':             'A. No positive elevation — cap 1.00',
        'star_pts_elev_cap': 1.00,
        'star_std_mult':     None,
    },
    'B_cap_105': {
        'label':             'B. Cap 1.05 — allow 5% boost',
        'star_pts_elev_cap': 1.05,
        'star_std_mult':     None,
    },
    'C_cap_110': {
        'label':             'C. Cap 1.10 — allow 10% boost',
        'star_pts_elev_cap': 1.10,
        'star_std_mult':     None,
    },
    'D_std_135': {
        'label':             'D. STD_MULT STAR → 1.35 (elev cap unchanged 1.25)',
        'star_pts_elev_cap': 1.25,
        'star_std_mult':     1.35,
    },
    'E_combined': {
        'label':             'E. Combined: cap 1.05 + STD 1.25',
        'star_pts_elev_cap': 1.05,
        'star_std_mult':     1.25,
    },
}


# ── Runner ────────────────────────────────────────────────────────────────────

def run_with(cfg: dict) -> pd.DataFrame:
    import src.backtest.playoff_backtest as hm
    return hm.run_playoff_backtest(
        seasons=SEASONS,
        verbose=False,
        star_pts_elev_cap=cfg['star_pts_elev_cap'],
        star_std_mult_override=cfg['star_std_mult'],
    )


# ── Metrics ───────────────────────────────────────────────────────────────────

def _hr(x):   return f"{x:.1%}" if x is not None and not np.isnan(x) else "  N/A"
def _sign(x): return f"{x:+.1%}" if x is not None and not np.isnan(x) else "  N/A"


def band_table(df: pd.DataFrame, stat: str, tier: str | None = None) -> list[dict]:
    s = df[(df['stat'] == stat)]
    if tier:
        s = s[s['tier'] == tier]
    rows = []
    for lo, hi in BANDS:
        sub = s[(s['p_over'] >= lo) & (s['p_over'] < hi)]
        rows.append({
            'band':    f"[{lo:.0%},{hi:.0%})",
            'n':       len(sub),
            'hr':      sub['hit'].mean() if len(sub) > 5 else None,
            'act_l10': (sub['actual'] / sub['l10_mean']).mean() if len(sub) > 5 else None,
            'pred_l10':(sub['pred_blended'] / sub['l10_mean']).mean() if len(sub) > 5 else None,
            'res_med': (sub['pred_blended'] - sub['actual']).median() if len(sub) > 5 else None,
        })
    return rows


def threshold_metrics(df: pd.DataFrame) -> dict:
    out = {}
    for t in THRESHOLDS:
        picks = df[df['p_over'] >= t]
        out[t] = (picks['hit'].mean() if len(picks) > 5 else None, len(picks))
    return out


def star_pts_metrics(df: pd.DataFrame) -> dict:
    sp = df[(df['stat'] == 'pts') & (df['tier'] == 'STAR')]
    out = {}
    for t in THRESHOLDS:
        picks = sp[sp['p_over'] >= t]
        out[f'sp_hr{int(t*100)}'] = picks['hit'].mean() if len(picks) > 5 else None
        out[f'sp_n{int(t*100)}']  = len(picks)
    # Band-level for monotonicity check
    for lo, hi in BANDS:
        sub = sp[(sp['p_over'] >= lo) & (sp['p_over'] < hi)]
        out[f'band_{int(lo*100)}_{int(hi*100)}_hr'] = sub['hit'].mean() if len(sub) > 5 else None
        out[f'band_{int(lo*100)}_{int(hi*100)}_n']  = len(sub)
    return out


def print_config_block(label: str, df: pd.DataFrame, base_df: pd.DataFrame | None = None) -> None:
    tm   = threshold_metrics(df)
    spm  = star_pts_metrics(df)
    base_tm  = threshold_metrics(base_df) if base_df is not None else None
    base_spm = star_pts_metrics(base_df) if base_df is not None else None

    print(f"\n{'─'*70}")
    print(f"  {label}")
    print(f"  Total picks: {len(df):,}")
    print(f"{'─'*70}")

    print(f"\n  Overall threshold performance (all stats, all tiers):")
    for t in THRESHOLDS:
        hr, n = tm[t]
        delta = ''
        if base_tm is not None:
            bhr, _ = base_tm[t]
            if hr is not None and bhr is not None:
                delta = f"  (Δ{hr-bhr:+.1%} vs baseline)"
        print(f"    P≥{t:.0%}: {_hr(hr):>6}  (n={n:,}){delta}")

    print(f"\n  STAR PTS threshold performance:")
    for t in THRESHOLDS:
        hr  = spm[f'sp_hr{int(t*100)}']
        n   = spm[f'sp_n{int(t*100)}']
        delta = ''
        if base_spm is not None:
            bhr = base_spm[f'sp_hr{int(t*100)}']
            if hr is not None and bhr is not None:
                delta = f"  (Δ{hr-bhr:+.1%})"
        print(f"    P≥{t:.0%}: {_hr(hr):>6}  (n={n:,}){delta}")

    print(f"\n  STAR PTS band breakdown (monotonicity check):")
    bands = band_table(df, 'pts', tier='STAR')
    for b in bands:
        hr_str     = f"{b['hr']:.1%}" if b['hr'] is not None else " N/A"
        al_str     = f"{b['act_l10']:.3f}" if b['act_l10'] is not None else " N/A "
        pl_str     = f"{b['pred_l10']:.3f}" if b['pred_l10'] is not None else " N/A "
        res_str    = f"{b['res_med']:+.2f}" if b['res_med'] is not None else "  N/A "
        delta = ''
        if base_df is not None:
            base_bands = band_table(base_df, 'pts', tier='STAR')
            bband = next((bb for bb in base_bands if bb['band'] == b['band']), None)
            if bband and b['hr'] is not None and bband['hr'] is not None:
                delta = f"  (Δ{b['hr']-bband['hr']:+.1%})"
        print(f"    {b['band']:>12}: hit={hr_str:>6} (n={b['n']:3d})  act/l10={al_str}  pred/l10={pl_str}  res_med={res_str}{delta}")

    # Spillover: did changes break other stats?
    other_stats = df[df['stat'] != 'pts']
    sp_others = {}
    for t in [0.55, 0.65]:
        picks = other_stats[other_stats['p_over'] >= t]
        sp_others[t] = picks['hit'].mean() if len(picks) > 5 else None
    base_sp_others = {}
    if base_df is not None:
        for t in [0.55, 0.65]:
            picks = base_df[base_df['stat'] != 'pts']
            picks = picks[picks['p_over'] >= t]
            base_sp_others[t] = picks['hit'].mean() if len(picks) > 5 else None
    print(f"\n  Spillover (non-PTS stats, should be ~zero):")
    for t in [0.55, 0.65]:
        hr = sp_others[t]
        delta = ''
        if base_df is not None and hr is not None and base_sp_others.get(t) is not None:
            delta = f"  (Δ{hr - base_sp_others[t]:+.1%})"
        print(f"    Non-PTS P≥{t:.0%}: {_hr(hr)}{delta}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  ABLATION — STAR PTS Confidence Inversion")
    print(f"  Seasons: {', '.join(SEASONS)}")
    print(f"  Key: 65-70% band at baseline hits 39.5% (n=129) — target of fix")
    print(f"{'='*70}")

    results = {}
    for key, cfg in CONFIGS.items():
        print(f"\n  Running: {cfg['label'][:62]}...", end='', flush=True)
        df = run_with(cfg)
        results[key] = df
        sp = df[(df['stat'] == 'pts') & (df['tier'] == 'STAR') & (df['p_over'] >= 0.55)]
        print(f" done ({len(df):,} rows, STAR PTS P≥55%: {sp['hit'].mean():.1%} n={len(sp)})")

    base_df = results['0_baseline']
    for key, cfg in CONFIGS.items():
        print_config_block(cfg['label'], results[key],
                           base_df=None if key == '0_baseline' else base_df)

    # ── Compact delta table ────────────────────────────────────────────────────
    print(f"\n\n{'='*70}")
    print(f"  DELTA TABLE vs Baseline")
    print(f"{'='*70}")

    base_tm  = threshold_metrics(base_df)
    base_spm = star_pts_metrics(base_df)

    hdrs = ["Config", "Total N", "All P55 HR", "All P55 Δ",
            "SP HR55", "SP HR55 Δ", "SP HR65", "SP HR65 Δ",
            "Band 65-70 HR", "Δ Band 65-70", "Band 65-70 N"]
    rows = []
    for key, cfg in CONFIGS.items():
        sm  = threshold_metrics(results[key])
        spm = star_pts_metrics(results[key])
        b65_hr = spm.get('band_65_70_hr')
        bb_hr  = base_spm.get('band_65_70_hr')
        sp55hr, _ = sm[0.55]
        bsp55hr, _ = base_tm[0.55]
        rows.append([
            cfg['label'].split('—')[0].strip()[:25],
            f"{len(results[key]):,}",
            _hr(sm[0.55][0]),
            '—' if key == '0_baseline' else _sign((sm[0.55][0] or 0) - (base_tm[0.55][0] or 0)),
            _hr(spm['sp_hr55']),
            '—' if key == '0_baseline' else _sign((spm['sp_hr55'] or 0) - (base_spm['sp_hr55'] or 0)),
            _hr(spm['sp_hr65']),
            '—' if key == '0_baseline' else _sign((spm['sp_hr65'] or 0) - (base_spm['sp_hr65'] or 0)),
            _hr(b65_hr),
            '—' if key == '0_baseline' else (_sign((b65_hr or 0) - (bb_hr or 0)) if b65_hr is not None and bb_hr is not None else 'N/A'),
            str(spm.get('band_65_70_n', 0)),
        ])

    cw = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(hdrs)]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in cw)
    print("\n" + fmt.format(*hdrs))
    print("  " + "  ".join("─" * w for w in cw))
    for row in rows:
        print(fmt.format(*row))

    # ── Monotonicity assessment ────────────────────────────────────────────────
    print(f"\n\n{'='*70}")
    print(f"  MONOTONICITY ASSESSMENT (STAR PTS bands, higher P→higher HR?)")
    print(f"{'='*70}")
    for key, cfg in CONFIGS.items():
        spm = star_pts_metrics(results[key])
        band_hrs = []
        for lo, hi in BANDS:
            hr = spm.get(f'band_{int(lo*100)}_{int(hi*100)}_hr')
            band_hrs.append(hr)
        # Check monotonicity from 55% onward (55,60,65,70,80)
        values = [spm.get(f'band_{int(lo*100)}_{int(hi*100)}_hr') for lo, hi in BANDS[1:]]
        values = [v for v in values if v is not None]
        is_monotone = all(values[i] <= values[i+1] for i in range(len(values)-1))
        violations  = sum(1 for i in range(len(values)-1) if values[i] > values[i+1])
        label_short = cfg['label'].split('—')[0].strip()
        mono_str = "MONOTONE" if is_monotone else f"NOT monotone ({violations} violation{'s' if violations > 1 else ''})"
        print(f"\n  {label_short}")
        print(f"    Bands [55%+]: {' → '.join(_hr(v) for v in values)}")
        print(f"    Assessment:   {mono_str}")

    # ── Recommendation ────────────────────────────────────────────────────────
    print(f"\n\n{'='*70}")
    print(f"  RECOMMENDATION")
    print(f"{'='*70}")
    # Best = highest STAR PTS HR at P≥55% with improved band 65-70 vs baseline
    base_b65 = base_spm.get('band_65_70_hr') or 0.0
    candidates = {
        k: star_pts_metrics(results[k]) for k in CONFIGS if k != '0_baseline'
    }
    # Filter: must improve 65-70 band AND not crater overall P≥55%
    base_all_hr, _ = threshold_metrics(base_df)[0.55]
    viable = {
        k: v for k, v in candidates.items()
        if (v.get('band_65_70_hr') or 0) > base_b65
        and (threshold_metrics(results[k])[0.55][0] or 0) >= (base_all_hr or 0) - 0.005
    }
    if viable:
        # Among viable: highest overall P≥55% HR
        best = max(viable, key=lambda k: threshold_metrics(results[k])[0.55][0] or 0)
        print(f"\n  Recommended: {CONFIGS[best]['label']}")
        print(f"  Reason: improves 65-70% band from {_hr(base_b65)} while preserving overall hit rate")
    else:
        # fallback: best STAR PTS P≥65%
        best = max(candidates, key=lambda k: candidates[k].get('sp_hr65') or 0)
        print(f"\n  Best available (no fully viable config): {CONFIGS[best]['label']}")
        print(f"  Reason: highest STAR PTS P≥65% HR")

    if args.save:
        all_rows = []
        for key, cfg in CONFIGS.items():
            spm = star_pts_metrics(results[key])
            tm  = threshold_metrics(results[key])
            row = {'config': cfg['label'], 'total_n': len(results[key])}
            row['all_p55_hr'] = tm[0.55][0]
            row['all_p65_hr'] = tm[0.65][0]
            row.update(spm)
            all_rows.append(row)
        out = Path(__file__).parent.parent / 'data' / 'processed' / 'ablation_star_pts.csv'
        pd.DataFrame(all_rows).to_csv(out, index=False)
        print(f"\n  Saved: {out}")

    print(f"\n{'='*70}\n")
    return results


if __name__ == '__main__':
    main()
