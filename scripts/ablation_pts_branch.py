"""
Ablation study — PTS calibration branch (df15f00).

Isolates the contribution of each of the three changes made:
  A) PRED_MULT hookup (was entirely missing from the harness before df15f00)
  B) PTS tier multiplier cuts (STAR 0.97→0.94, CORE 0.97→0.93, ROT 0.93→0.89)
  C) STAR STD_MULT raise (1.13→1.20)

Four configurations tested:
  1. PRED_MULT hookup only       — original tier values, original STD_MULT
  2. PTS cuts only               — new PTS values, original STD_MULT
  3. STAR STD_MULT only          — original PTS values, new STD_MULT
  4. Full combined (df15f00)     — new PTS values, new STD_MULT

Usage:
    python scripts/ablation_pts_branch.py [--save]
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

# ── Pre-change (original) constants ──────────────────────────────────────────

ORIG_PRED_MULT = {
    'STAR': {
        'pts': 0.97, 'reb': 1.00, 'ast': 0.92,
        'fg3m': 0.94, 'pra': 0.97, 'reb_ast': 0.96,
    },
    'CORE': {
        'pts': 0.97, 'reb': 0.99, 'ast': 0.93,
        'fg3m': 0.95, 'pra': 0.97, 'reb_ast': 0.96,
    },
    'ROTATION': {
        'pts': 0.93, 'reb': 0.95, 'ast': 0.91,
        'fg3m': 0.92, 'pra': 0.93, 'reb_ast': 0.93,
    },
    'FRINGE': {s: 0.85 for s in ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']},
}

# Post-change PTS tier cuts only (other stats unchanged)
NEW_PRED_MULT = copy.deepcopy(ORIG_PRED_MULT)
NEW_PRED_MULT['STAR']['pts']     = 0.94
NEW_PRED_MULT['CORE']['pts']     = 0.93
NEW_PRED_MULT['ROTATION']['pts'] = 0.89

ORIG_STD_MULT = {'STAR': 1.13, 'CORE': 1.07, 'ROTATION': 1.10}
NEW_STD_MULT  = {'STAR': 1.20, 'CORE': 1.07, 'ROTATION': 1.10}

# "No-op" PRED_MULT — all 1.0 — represents the pre-hookup harness state
# where PRED_MULT was not applied at all (baseline before df15f00)
NOOP_PRED_MULT = {
    t: {s: 1.0 for s in ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']}
    for t in ['STAR', 'CORE', 'ROTATION', 'FRINGE']
}

SEASONS = ['2024-25', '2023-24', '2022-23', '2021-22']
THRESHOLDS = [0.55, 0.60, 0.65, 0.70]


# ── Run harness with injected constants ───────────────────────────────────────

def run_with(pred_mult: dict, std_mult: dict, label: str) -> pd.DataFrame:
    """
    Run the playoff backtest harness with overridden PRED_MULT and STD_MULT.
    Patches module-level names in src.backtest.playoff_backtest before running.
    """
    import src.backtest.playoff_backtest as harness_mod
    orig_pm  = harness_mod.PRED_MULT_PLAYOFF
    orig_std = harness_mod.STD_MULT_PLAYOFF
    try:
        harness_mod.PRED_MULT_PLAYOFF = pred_mult
        harness_mod.STD_MULT_PLAYOFF  = std_mult
        df = harness_mod.run_playoff_backtest(seasons=SEASONS, verbose=False)
    finally:
        harness_mod.PRED_MULT_PLAYOFF = orig_pm
        harness_mod.STD_MULT_PLAYOFF  = orig_std
    df['config'] = label
    return df


# ── Metrics extraction ────────────────────────────────────────────────────────

def threshold_metrics(df: pd.DataFrame) -> dict:
    """Hit rates at each threshold (all stats combined)."""
    out = {}
    for t in THRESHOLDS:
        picks = df[df['p_over'] >= t]
        out[t] = {'hr': picks['hit'].mean() if len(picks) > 10 else None, 'n': len(picks)}
    return out


def stat_metrics(df: pd.DataFrame, stat: str) -> dict:
    """Hit rate at P≥55%, residual mean/median, MAE for a given stat."""
    s = df[df['stat'] == stat]
    picks55 = s[s['p_over'] >= 0.55]
    res = s['pred_blended'] - s['actual']
    return {
        'hr55':   picks55['hit'].mean() if len(picks55) > 10 else None,
        'n55':    len(picks55),
        'mean_err': res.mean(),
        'med_err':  res.median(),
        'mae':    res.abs().mean(),
    }


def print_config_block(label: str, df: pd.DataFrame) -> None:
    tm = threshold_metrics(df)
    pts = stat_metrics(df, 'pts')
    pra = stat_metrics(df, 'pra')
    ast = stat_metrics(df, 'ast')
    total = len(df[df['stat'] == 'pts'])  # rows per stat

    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")

    print(f"\n  Overall (all stats):")
    for t in THRESHOLDS:
        m = tm[t]
        hr = f"{m['hr']:.1%}" if m['hr'] is not None else "  N/A"
        print(f"    P≥{t:.0%}: {hr:>6}  (n={m['n']:,})")

    print(f"\n  PTS  (n={total:,} total rows):")
    print(f"    P≥55% HR:  {pts['hr55']:.1%} (n={pts['n55']:,})")
    print(f"    MAE:       {pts['mae']:.3f}")
    print(f"    Mean err:  {pts['mean_err']:+.3f}")
    print(f"    Med  err:  {pts['med_err']:+.3f}")

    print(f"\n  PRA  (spillover):")
    print(f"    P≥55% HR:  {pra['hr55']:.1%} (n={pra['n55']:,})")
    print(f"    MAE:       {pra['mae']:.3f}")
    print(f"    Mean err:  {pra['mean_err']:+.3f}")
    print(f"    Med  err:  {pra['med_err']:+.3f}")

    print(f"\n  AST  (spillover):")
    print(f"    P≥55% HR:  {ast['hr55']:.1%} (n={ast['n55']:,})")
    print(f"    MAE:       {ast['mae']:.3f}")
    print(f"    Mean err:  {ast['mean_err']:+.3f}")
    print(f"    Med  err:  {ast['med_err']:+.3f}")


def build_summary_table(results: list[tuple[str, pd.DataFrame]]) -> pd.DataFrame:
    """Compact summary DataFrame for CSV export."""
    rows = []
    for label, df in results:
        tm = threshold_metrics(df)
        for stat in ['pts', 'pra', 'ast', 'reb', 'reb_ast']:
            sm = stat_metrics(df, stat)
            for t in THRESHOLDS:
                m = tm[t]
                rows.append({
                    'config': label,
                    'stat': stat,
                    'threshold': t,
                    'hr': sm['hr55'] if t == 0.55 else None,
                    'n': sm['n55'] if t == 0.55 else None,
                    'overall_hr': m['hr'],
                    'overall_n': m['n'],
                    'mae': sm['mae'],
                    'mean_err': sm['mean_err'],
                    'med_err': sm['med_err'],
                })
    return pd.DataFrame(rows)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--save', action='store_true', help='Save summary CSV')
    args = parser.parse_args()

    configs = [
        # (label, pred_mult, std_mult)
        ("1. PRED_MULT hookup only (orig values, orig STD)",  ORIG_PRED_MULT, ORIG_STD_MULT),
        ("2. PTS cuts only (new PTS values, orig STD)",       NEW_PRED_MULT,  ORIG_STD_MULT),
        ("3. STAR STD_MULT only (orig PTS values, new STD)",  ORIG_PRED_MULT, NEW_STD_MULT),
        ("4. Full combined — df15f00 (new PTS, new STD)",     NEW_PRED_MULT,  NEW_STD_MULT),
    ]

    print(f"\n{'='*60}")
    print(f"  ABLATION — PTS Calibration Branch (df15f00)")
    print(f"  Seasons: {', '.join(SEASONS)}")
    print(f"{'='*60}")

    results = []
    for label, pm, sm in configs:
        print(f"\n  Running: {label[:40]}...", end='', flush=True)
        df = run_with(pm, sm, label)
        results.append((label, df))
        print(f" done ({len(df):,} rows)")

    # Print per-config blocks
    for label, df in results:
        print_config_block(label, df)

    # Delta table vs config 1 (PRED_MULT hookup only baseline)
    _, base_df = results[0]
    base_pts = stat_metrics(base_df, 'pts')
    base_tm  = threshold_metrics(base_df)

    print(f"\n\n{'='*60}")
    print(f"  DELTA vs Config 1 (PRED_MULT hookup baseline)")
    print(f"{'='*60}")

    headers = ["Config", "P≥55% Δ", "P≥60% Δ", "P≥65% Δ", "P≥70% Δ",
               "PTS HR55 Δ", "PTS MAE Δ", "PRA HR55 Δ", "AST HR55 Δ"]
    rows_data = []
    for label, df in results[1:]:
        tm = threshold_metrics(df)
        pts = stat_metrics(df, 'pts')
        pra = stat_metrics(df, 'pra')
        ast = stat_metrics(df, 'ast')

        base_pra = stat_metrics(base_df, 'pra')
        base_ast = stat_metrics(base_df, 'ast')

        row = [
            label.split("(")[0].strip(),
            f"{(tm[0.55]['hr'] or 0) - (base_tm[0.55]['hr'] or 0):+.1%}",
            f"{(tm[0.60]['hr'] or 0) - (base_tm[0.60]['hr'] or 0):+.1%}",
            f"{(tm[0.65]['hr'] or 0) - (base_tm[0.65]['hr'] or 0):+.1%}",
            f"{(tm[0.70]['hr'] or 0) - (base_tm[0.70]['hr'] or 0):+.1%}",
            f"{(pts['hr55'] or 0) - (base_pts['hr55'] or 0):+.1%}",
            f"{pts['mae'] - base_pts['mae']:+.3f}",
            f"{(pra['hr55'] or 0) - (base_pra['hr55'] or 0):+.1%}",
            f"{(ast['hr55'] or 0) - (base_ast['hr55'] or 0):+.1%}",
        ]
        rows_data.append(row)

    col_widths = [max(len(h), max(len(r[i]) for r in rows_data)) for i, h in enumerate(headers)]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in col_widths)
    print("\n" + fmt.format(*headers))
    print("  " + "  ".join("-" * w for w in col_widths))
    for row in rows_data:
        print(fmt.format(*row))

    if args.save:
        summary = build_summary_table(results)
        out = Path(__file__).parent.parent / "data" / "processed" / "ablation_pts_branch.csv"
        summary.to_csv(out, index=False)
        print(f"\n  Saved: {out}")

    print(f"\n{'='*60}\n")


if __name__ == '__main__':
    main()
