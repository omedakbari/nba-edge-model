"""
Ablation study — AST calibration branch.

Diagnostic first established:
  - ROTATION AST actual/L10 median = 0.556 (well below line 0.769)
  - ROTATION hits only 42.1% at P≥55% — primary drag on overall AST
  - CORE AST actual/L10 = 0.769 (right at the line), hits 56.3% — marginal
  - STAR AST actual/L10 = 0.800 (above line), hits 60.3% — working well
  - Line ratio raise: invalid lever — same picks are selected (p_over computed
    with old line), raising line only lowers hit rate without filtering picks

Candidates tested:
  A. Line ratio: 0.769 → 0.79  (raise for all tiers; p_over recomputed)
  B. AST PRED_MULT — ROTATION only: 0.91 → 0.77
     (brings pred ≈ line for ROTATION, collapses ROTATION into sub-55% range)
  C. AST PRED_MULT — ROTATION + CORE: ROTATION 0.91→0.77, CORE 0.93→0.82
  D. AST PRED_MULT — all tiers uniform: STAR 0.92→0.88, CORE 0.93→0.88, ROT 0.91→0.77

Baseline = df15f00 (new PTS values, new STD_MULT, PRED_MULT hooked up).

Usage:
    python scripts/ablation_ast_branch.py [--save]
"""

import sys
import copy
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import warnings
warnings.filterwarnings("ignore")

import pandas as pd

SEASONS = ['2024-25', '2023-24', '2022-23', '2021-22']
THRESHOLDS = [0.55, 0.60, 0.65, 0.70]

# ── Baseline PRED_MULT (df15f00) ──────────────────────────────────────────────

BASELINE_PRED_MULT = {
    'STAR': {
        'pts': 0.94, 'reb': 1.00, 'ast': 0.92,
        'fg3m': 0.94, 'pra': 0.97, 'reb_ast': 0.96,
    },
    'CORE': {
        'pts': 0.93, 'reb': 0.99, 'ast': 0.93,
        'fg3m': 0.95, 'pra': 0.97, 'reb_ast': 0.96,
    },
    'ROTATION': {
        'pts': 0.89, 'reb': 0.95, 'ast': 0.91,
        'fg3m': 0.92, 'pra': 0.93, 'reb_ast': 0.93,
    },
    'FRINGE': {s: 0.85 for s in ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']},
}

BASELINE_STD_MULT  = {'STAR': 1.20, 'CORE': 1.07, 'ROTATION': 1.10}
BASELINE_LINE_RATIO = {
    'pts': 0.909, 'reb': 0.986, 'ast': 0.769, 'pra': 0.939, 'reb_ast': 0.899,
}


def _make_pred_mult(ast_overrides: dict[str, float]) -> dict:
    pm = copy.deepcopy(BASELINE_PRED_MULT)
    for tier, val in ast_overrides.items():
        pm[tier]['ast'] = val
    return pm


# ── Candidate configs ─────────────────────────────────────────────────────────

CONFIGS = {
    '0_baseline': {
        'label': 'Baseline (df15f00)',
        'pred_mult': BASELINE_PRED_MULT,
        'std_mult': BASELINE_STD_MULT,
        'line_ratio': BASELINE_LINE_RATIO,
    },
    'A_line_ratio': {
        'label': 'A. AST line ratio 0.769 → 0.79 (all tiers)',
        'pred_mult': BASELINE_PRED_MULT,
        'std_mult': BASELINE_STD_MULT,
        'line_ratio': {**BASELINE_LINE_RATIO, 'ast': 0.79},
    },
    'B_rot_only': {
        'label': 'B. AST PRED_MULT ROTATION only: 0.91 → 0.77',
        'pred_mult': _make_pred_mult({'ROTATION': 0.77}),
        'std_mult': BASELINE_STD_MULT,
        'line_ratio': BASELINE_LINE_RATIO,
    },
    'C_rot_core': {
        'label': 'C. AST PRED_MULT ROTATION 0.91→0.77, CORE 0.93→0.82',
        'pred_mult': _make_pred_mult({'ROTATION': 0.77, 'CORE': 0.82}),
        'std_mult': BASELINE_STD_MULT,
        'line_ratio': BASELINE_LINE_RATIO,
    },
    'D_all_tiers': {
        'label': 'D. AST PRED_MULT all: STAR 0.92→0.88, CORE 0.93→0.88, ROT 0.91→0.77',
        'pred_mult': _make_pred_mult({'STAR': 0.88, 'CORE': 0.88, 'ROTATION': 0.77}),
        'std_mult': BASELINE_STD_MULT,
        'line_ratio': BASELINE_LINE_RATIO,
    },
}


# ── Harness runner ────────────────────────────────────────────────────────────

def run_with(config: dict) -> pd.DataFrame:
    import src.backtest.playoff_backtest as hm
    orig_pm   = hm.PRED_MULT_PLAYOFF
    orig_std  = hm.STD_MULT_PLAYOFF
    orig_line = hm.PLAYOFF_LINE_RATIO
    try:
        hm.PRED_MULT_PLAYOFF  = config['pred_mult']
        hm.STD_MULT_PLAYOFF   = config['std_mult']
        hm.PLAYOFF_LINE_RATIO = config['line_ratio']
        df = hm.run_playoff_backtest(seasons=SEASONS, verbose=False)
    finally:
        hm.PRED_MULT_PLAYOFF  = orig_pm
        hm.STD_MULT_PLAYOFF   = orig_std
        hm.PLAYOFF_LINE_RATIO = orig_line
    return df


# ── Metrics ───────────────────────────────────────────────────────────────────

def threshold_row(df: pd.DataFrame) -> dict:
    out = {}
    for t in THRESHOLDS:
        picks = df[df['p_over'] >= t]
        out[t] = (picks['hit'].mean() if len(picks) > 5 else None, len(picks))
    return out


def stat_row(df: pd.DataFrame, stat: str, by_tier: bool = False) -> dict:
    s = df[df['stat'] == stat]
    p55 = s[s['p_over'] >= 0.55]
    p65 = s[s['p_over'] >= 0.65]
    res = s['pred_blended'] - s['actual']
    out = {
        'hr55': p55['hit'].mean() if len(p55) > 5 else None,
        'n55':  len(p55),
        'hr65': p65['hit'].mean() if len(p65) > 5 else None,
        'n65':  len(p65),
        'mae':  res.abs().mean(),
        'mean_err': res.mean(),
        'med_err':  res.median(),
    }
    if by_tier:
        for tier in ['STAR', 'CORE', 'ROTATION']:
            t = s[s['tier'] == tier]
            t55 = t[t['p_over'] >= 0.55]
            out[f'{tier}_hr55'] = t55['hit'].mean() if len(t55) > 5 else None
            out[f'{tier}_n55']  = len(t55)
    return out


def _hr(x): return f"{x:.1%}" if x is not None else "  N/A"
def _n(x):  return f"n={x:,}"


def print_block(label: str, df: pd.DataFrame) -> None:
    tm  = threshold_row(df)
    ast = stat_row(df, 'ast', by_tier=True)
    pts = stat_row(df, 'pts')
    pra = stat_row(df, 'pra')

    print(f"\n{'─'*65}")
    print(f"  {label}")
    print(f"{'─'*65}")
    print(f"\n  Overall (all stats):")
    for t in THRESHOLDS:
        hr, n = tm[t]
        print(f"    P≥{t:.0%}: {_hr(hr):>6}  ({_n(n)})")
    print(f"\n  AST by tier (P≥55%):")
    for tier in ['STAR', 'CORE', 'ROTATION', 'all']:
        if tier == 'all':
            print(f"    {'ALL':>8}: {_hr(ast['hr55']):>6}  ({_n(ast['n55'])})  P≥65%: {_hr(ast['hr65'])} ({_n(ast['n65'])})")
        else:
            print(f"    {tier:>8}: {_hr(ast[f'{tier}_hr55']):>6}  ({_n(ast[f'{tier}_n55'])})")
    print(f"\n  AST residuals:  mean {ast['mean_err']:+.3f}  median {ast['med_err']:+.3f}  MAE {ast['mae']:.3f}")
    print(f"  PTS P≥55%: {_hr(pts['hr55'])} ({_n(pts['n55'])})   PRA: {_hr(pra['hr55'])} ({_n(pra['n55'])})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*65}")
    print(f"  ABLATION — AST Branch (from df15f00 baseline)")
    print(f"  Seasons: {', '.join(SEASONS)}")
    print(f"{'='*65}")

    results = {}
    for key, cfg in CONFIGS.items():
        print(f"\n  Running: {cfg['label'][:55]}...", end='', flush=True)
        df = run_with(cfg)
        results[key] = df
        print(f" done ({len(df):,} rows)")

    for key, cfg in CONFIGS.items():
        print_block(cfg['label'], results[key])

    # Delta table vs baseline
    base = results['0_baseline']
    base_tm  = threshold_row(base)
    base_ast = stat_row(base, 'ast', by_tier=True)

    print(f"\n\n{'='*65}")
    print(f"  DELTA vs Baseline (df15f00)")
    print(f"{'='*65}")

    hdrs = ["Config", "OverallP55 Δ", "OverallP70 Δ", "AST HR55 Δ", "AST N55", "AST HR65 Δ", "AST MAE Δ",
            "STAR Δ", "CORE Δ", "ROT Δ"]
    rows = []
    for key, cfg in CONFIGS.items():
        if key == '0_baseline':
            continue
        df = results[key]
        tm = threshold_row(df)
        ast = stat_row(df, 'ast', by_tier=True)
        b55hr, _ = base_tm[0.55]; b70hr, _ = base_tm[0.70]
        t55hr, _ = tm[0.55];      t70hr, _ = tm[0.70]
        rows.append([
            cfg['label'].split('.')[0].strip(),
            f"{(t55hr or 0) - (b55hr or 0):+.1%}",
            f"{(t70hr or 0) - (b70hr or 0):+.1%}",
            f"{(ast['hr55'] or 0) - (base_ast['hr55'] or 0):+.1%}",
            f"{ast['n55']:,}",
            f"{(ast['hr65'] or 0) - (base_ast['hr65'] or 0):+.1%}",
            f"{ast['mae'] - base_ast['mae']:+.4f}",
            f"{((ast['STAR_hr55'] or 0) - (base_ast['STAR_hr55'] or 0)):+.1%}",
            f"{((ast['CORE_hr55'] or 0) - (base_ast['CORE_hr55'] or 0)):+.1%}",
            f"{((ast['ROTATION_hr55'] or 0) - (base_ast['ROTATION_hr55'] or 0)):+.1%}",
        ])

    cw = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(hdrs)]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in cw)
    print("\n" + fmt.format(*hdrs))
    print("  " + "  ".join("─" * w for w in cw))
    for row in rows:
        print(fmt.format(*row))

    # Recommendation
    best_key = max(
        [k for k in results if k != '0_baseline'],
        key=lambda k: (stat_row(results[k], 'ast')['hr55'] or 0)
    )
    print(f"\n  → Recommended: {CONFIGS[best_key]['label']}")

    if args.save:
        rows_csv = []
        for key, cfg in CONFIGS.items():
            df = results[key]
            for stat in ['pts', 'reb', 'ast', 'pra', 'reb_ast']:
                sm = stat_row(df, stat)
                for t in THRESHOLDS:
                    hr, n = threshold_row(df)[t]
                    rows_csv.append({'config': cfg['label'], 'stat': stat,
                                     'threshold': t, 'overall_hr': hr, 'overall_n': n,
                                     **{f'stat_{k}': v for k, v in sm.items()}})
        out = Path(__file__).parent.parent / 'data' / 'processed' / 'ablation_ast_branch.csv'
        pd.DataFrame(rows_csv).to_csv(out, index=False)
        print(f"  Saved: {out}")

    print(f"\n{'='*65}\n")


if __name__ == '__main__':
    main()
