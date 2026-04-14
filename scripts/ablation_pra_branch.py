"""
Ablation study — PRA calibration branch (from 9b01c4c baseline).

Diagnostic summary:
  STAR:     pred/l10=0.970  line/l10=0.939  actual/l10=0.938 → pred too generous by ~3%
            P≥55% hit=56.4%, soft spot at 60-65% (51.7%) → lower mult to 0.94
  CORE:     pred/l10=0.970  line/l10=0.939  actual/l10=0.935
            P≥55% hit=63.7%, properly monotone above 55%, residual ≈ 0 → DO NOT TOUCH
  ROTATION: pred/l10=0.930  line/l10=0.939  actual/l10=0.691
            pred already below line; 212 picks survive via positive elev_ratios but hit only 50.5%
            Need PRED_MULT ~0.87 so only players with elev_ratio ≥ 1.08 clear the line

Configs tested (CORE always unchanged):
  A. STAR only:        STAR 0.97 → 0.94
  B. ROTATION only:    ROTATION 0.93 → 0.87
  C. ROTATION aggressv: ROTATION 0.93 → 0.85
  D. Combined A+B:     STAR 0.97→0.94, ROTATION 0.93→0.87
  E. Combined A+C:     STAR 0.97→0.94, ROTATION 0.93→0.85

Spillover tracked: PTS, AST, REB_AST (should be zero — separate PRED_MULT entries).

Usage:
    python scripts/ablation_pra_branch.py [--save]
"""

import sys
import copy
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

SEASONS = ['2024-25', '2023-24', '2022-23', '2021-22']
THRESHOLDS = [0.55, 0.60, 0.65, 0.70]

# ── Baseline PRED_MULT (9b01c4c) ──────────────────────────────────────────────

BASELINE_PRED_MULT = {
    'STAR': {
        'pts': 0.94, 'reb': 1.00, 'ast': 0.92,
        'fg3m': 0.94, 'pra': 0.97, 'reb_ast': 0.96,
    },
    'CORE': {
        'pts': 0.93, 'reb': 0.99, 'ast': 0.82,
        'fg3m': 0.95, 'pra': 0.97, 'reb_ast': 0.96,
    },
    'ROTATION': {
        'pts': 0.89, 'reb': 0.95, 'ast': 0.77,
        'fg3m': 0.92, 'pra': 0.93, 'reb_ast': 0.93,
    },
    'FRINGE': {s: 0.85 for s in ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']},
}

BASELINE_STD_MULT  = {'STAR': 1.20, 'CORE': 1.07, 'ROTATION': 1.10}
BASELINE_LINE_RATIO = {
    'pts': 0.909, 'reb': 0.986, 'ast': 0.769, 'pra': 0.939, 'reb_ast': 0.899,
}


def _pm(star_pra: float = 0.97, rot_pra: float = 0.93) -> dict:
    pm = copy.deepcopy(BASELINE_PRED_MULT)
    pm['STAR']['pra']     = star_pra
    pm['ROTATION']['pra'] = rot_pra
    return pm


CONFIGS = {
    '0_baseline': {
        'label': 'Baseline (9b01c4c)',
        'pred_mult': _pm(0.97, 0.93),
    },
    'A_star_only': {
        'label': 'A. STAR PRA 0.97 → 0.94 only',
        'pred_mult': _pm(star_pra=0.94, rot_pra=0.93),
    },
    'B_rot_087': {
        'label': 'B. ROTATION PRA 0.93 → 0.87 only',
        'pred_mult': _pm(star_pra=0.97, rot_pra=0.87),
    },
    'C_rot_085': {
        'label': 'C. ROTATION PRA 0.93 → 0.85 only (aggressive)',
        'pred_mult': _pm(star_pra=0.97, rot_pra=0.85),
    },
    'D_combined_B': {
        'label': 'D. STAR 0.97→0.94 + ROTATION 0.93→0.87',
        'pred_mult': _pm(star_pra=0.94, rot_pra=0.87),
    },
    'E_combined_C': {
        'label': 'E. STAR 0.97→0.94 + ROTATION 0.93→0.85',
        'pred_mult': _pm(star_pra=0.94, rot_pra=0.85),
    },
}


# ── Harness runner ────────────────────────────────────────────────────────────

def run_with(cfg: dict) -> pd.DataFrame:
    import src.backtest.playoff_backtest as hm
    orig_pm   = hm.PRED_MULT_PLAYOFF
    orig_std  = hm.STD_MULT_PLAYOFF
    orig_line = hm.PLAYOFF_LINE_RATIO
    try:
        hm.PRED_MULT_PLAYOFF  = cfg['pred_mult']
        hm.STD_MULT_PLAYOFF   = BASELINE_STD_MULT
        hm.PLAYOFF_LINE_RATIO = BASELINE_LINE_RATIO
        df = hm.run_playoff_backtest(seasons=SEASONS, verbose=False)
    finally:
        hm.PRED_MULT_PLAYOFF  = orig_pm
        hm.STD_MULT_PLAYOFF   = orig_std
        hm.PLAYOFF_LINE_RATIO = orig_line
    return df


# ── Metrics ───────────────────────────────────────────────────────────────────

def threshold_row(df: pd.DataFrame) -> dict:
    return {t: (df[df.p_over >= t]['hit'].mean() if len(df[df.p_over >= t]) > 5 else None,
                len(df[df.p_over >= t]))
            for t in THRESHOLDS}


def stat_metrics(df: pd.DataFrame, stat: str, by_tier: bool = False) -> dict:
    s = df[df['stat'] == stat]
    p55, p65 = s[s.p_over >= 0.55], s[s.p_over >= 0.65]
    res = s['pred_blended'] - s['actual']
    out = {
        'hr55': p55['hit'].mean() if len(p55) > 5 else None, 'n55': len(p55),
        'hr65': p65['hit'].mean() if len(p65) > 5 else None, 'n65': len(p65),
        'mae': res.abs().mean(), 'mean_err': res.mean(), 'med_err': res.median(),
    }
    if by_tier:
        for tier in ['STAR', 'CORE', 'ROTATION']:
            t = s[s['tier'] == tier]
            t55 = t[t.p_over >= 0.55]
            out[f'{tier}_hr55'] = t55['hit'].mean() if len(t55) > 5 else None
            out[f'{tier}_n55']  = len(t55)
    return out


def _hr(x): return f"{x:.1%}" if x is not None else "  N/A"


def print_block(label: str, df: pd.DataFrame) -> None:
    tm  = threshold_row(df)
    pra = stat_metrics(df, 'pra', by_tier=True)
    pts = stat_metrics(df, 'pts')
    ast = stat_metrics(df, 'ast')
    ra  = stat_metrics(df, 'reb_ast')

    print(f"\n{'─'*65}")
    print(f"  {label}")
    print(f"{'─'*65}")
    print(f"  Overall (all stats):")
    for t in THRESHOLDS:
        hr, n = tm[t]
        print(f"    P≥{t:.0%}: {_hr(hr):>6}  (n={n:,})")
    print(f"\n  PRA by tier (P≥55%):")
    for tier in ['STAR', 'CORE', 'ROTATION', 'all']:
        if tier == 'all':
            print(f"    {'ALL':>8}: {_hr(pra['hr55']):>6}  (n={pra['n55']:,})  "
                  f"P≥65%: {_hr(pra['hr65'])} (n={pra['n65']:,})")
        else:
            print(f"    {tier:>8}: {_hr(pra[f'{tier}_hr55']):>6}  (n={pra[f'{tier}_n55']:,})")
    print(f"\n  PRA residuals: mean {pra['mean_err']:+.3f}  median {pra['med_err']:+.3f}  MAE {pra['mae']:.3f}")
    print(f"  Spillover — PTS: {_hr(pts['hr55'])} (n={pts['n55']:,})  "
          f"AST: {_hr(ast['hr55'])} (n={ast['n55']:,})  "
          f"REB_AST: {_hr(ra['hr55'])} (n={ra['n55']:,})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*65}")
    print(f"  ABLATION — PRA Branch (from 9b01c4c baseline)")
    print(f"  Seasons: {', '.join(SEASONS)}")
    print(f"  CORE PRA unchanged (0.97) in all configs — 63.7% at P≥55%")
    print(f"{'='*65}")

    results = {}
    for key, cfg in CONFIGS.items():
        print(f"\n  Running: {cfg['label'][:58]}...", end='', flush=True)
        df = run_with(cfg)
        results[key] = df
        print(f" done ({len(df):,} rows)")

    for key, cfg in CONFIGS.items():
        print_block(cfg['label'], results[key])

    # Delta table
    base = results['0_baseline']
    base_tm  = threshold_row(base)
    base_pra = stat_metrics(base, 'pra', by_tier=True)

    print(f"\n\n{'='*65}")
    print(f"  DELTA vs Baseline (9b01c4c)")
    print(f"{'='*65}")

    hdrs = ["Config", "P55 Δ", "P70 Δ", "PRA_HR55 Δ", "PRA_N55",
            "PRA_HR65 Δ", "PRA_MAE Δ", "STAR Δ", "CORE Δ", "ROT Δ"]
    rows = []
    for key, cfg in CONFIGS.items():
        if key == '0_baseline':
            continue
        df   = results[key]
        tm   = threshold_row(df)
        pra  = stat_metrics(df, 'pra', by_tier=True)
        b55h, _ = base_tm[0.55]; b70h, _ = base_tm[0.70]
        t55h, _ = tm[0.55];      t70h, _ = tm[0.70]
        rows.append([
            cfg['label'].split('.')[0].strip(),
            f"{(t55h or 0)-(b55h or 0):+.1%}",
            f"{(t70h or 0)-(b70h or 0):+.1%}",
            f"{(pra['hr55'] or 0)-(base_pra['hr55'] or 0):+.1%}",
            f"{pra['n55']:,}",
            f"{(pra['hr65'] or 0)-(base_pra['hr65'] or 0):+.1%}",
            f"{pra['mae']-base_pra['mae']:+.4f}",
            f"{((pra['STAR_hr55'] or 0)-(base_pra['STAR_hr55'] or 0)):+.1%}",
            f"{((pra['CORE_hr55'] or 0)-(base_pra['CORE_hr55'] or 0)):+.1%}",
            f"{((pra['ROTATION_hr55'] or 0)-(base_pra['ROTATION_hr55'] or 0)):+.1%}",
        ])

    cw = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(hdrs)]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in cw)
    print("\n" + fmt.format(*hdrs))
    print("  " + "  ".join("─" * w for w in cw))
    for row in rows:
        print(fmt.format(*row))

    # Pick winner: highest PRA HR at P≥55%
    best = max(
        [k for k in results if k != '0_baseline'],
        key=lambda k: stat_metrics(results[k], 'pra')['hr55'] or 0
    )
    print(f"\n  → Highest PRA HR55: {CONFIGS[best]['label']}")

    if args.save:
        rows_csv = []
        for key, cfg in CONFIGS.items():
            df = results[key]
            for stat in ['pts', 'reb', 'ast', 'pra', 'reb_ast']:
                sm = stat_metrics(df, stat)
                for t in THRESHOLDS:
                    hr, n = threshold_row(df)[t]
                    rows_csv.append({'config': cfg['label'], 'stat': stat,
                                     'threshold': t, 'overall_hr': hr, 'overall_n': n,
                                     **{f'stat_{k}': v for k, v in sm.items()}})
        out = Path(__file__).parent.parent / 'data' / 'processed' / 'ablation_pra_branch.csv'
        pd.DataFrame(rows_csv).to_csv(out, index=False)
        print(f"  Saved: {out}")

    print(f"\n{'='*65}\n")
    return results


if __name__ == '__main__':
    main()
