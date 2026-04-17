"""
Phase 2 Experiment — FG3M Isotonic Calibration
scripts/phase2_isotonic_fg3m.py

Hypothesis: The Gaussian CDF systematically overestimates P(over) for FG3M
at production thresholds (55-65%), causing the -3.6% calibration gap measured
in Phase 1 (56.2% actual vs 59.9% predicted across 1,280 production picks).

Experiment:
  - Temporal split: fit isotonic regressor on Oct-22 to Mar-07 (first 127 dates)
  - Evaluate on Mar-08 to Jun-22 (last 86 dates)
  - FG3M only — other stats are well-calibrated at production thresholds
  - Use prob_blend65 as input (35% model + 65% L10, the backtest blend)

Decision gate:
  - If eval-half 55-65% gap improves from -6% toward ≤ -2%: wire into production
  - If not: root cause is mu (point prediction bias), not CDF — different fix needed

Usage:
    python scripts/phase2_isotonic_fg3m.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import pickle
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.calibration import calibration_curve

OUT_DIR  = Path(__file__).parent.parent / "data" / "diagnostics"
CAL_DIR  = Path(__file__).parent.parent / "data" / "processed" / "models" / "calibrators"
LOG_PATH = OUT_DIR / "per_pick_log.csv"

BUCKET_EDGES  = [0.50, 0.55, 0.57, 0.60, 0.65, 0.70, 1.01]
BUCKET_LABELS = ['50-55', '55-57', '57-60', '60-65', '65-70', '70+']


def bucket_table(probs: np.ndarray, hits: np.ndarray, label: str) -> pd.DataFrame:
    rows = []
    for lo, hi, blabel in zip(BUCKET_EDGES[:-1], BUCKET_EDGES[1:], BUCKET_LABELS):
        mask = (probs >= lo) & (probs < hi)
        n = mask.sum()
        if n < 5:
            continue
        avg_p = float(probs[mask].mean())
        hr    = float(hits[mask].mean())
        rows.append({
            'model': label, 'bucket': blabel, 'n': int(n),
            'avg_model_p': round(avg_p, 4),
            'actual_hr':   round(hr, 4),
            'gap':         round(hr - avg_p, 4),
        })
    return pd.DataFrame(rows)


def brier_ece(probs: np.ndarray, hits: np.ndarray) -> tuple[float, float]:
    brier = float(brier_score_loss(hits, probs))
    bins  = np.linspace(0, 1, 11)
    ece   = 0.0
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (probs >= lo) & (probs < hi)
        if m.sum() >= 5:
            ece += abs(probs[m].mean() - hits[m].mean()) * m.sum() / len(probs)
    return brier, ece


def plot_reliability_comparison(probs_raw: np.ndarray, probs_cal: np.ndarray,
                                hits: np.ndarray, out_path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, probs, label, color in [
        (axes[0], probs_raw, 'Before (Gaussian)', 'steelblue'),
        (axes[1], probs_cal, 'After (Isotonic)', 'seagreen'),
    ]:
        frac_pos, mean_pred = calibration_curve(hits, probs, n_bins=10, strategy='quantile')
        ax.plot([0, 1], [0, 1], 'k--', alpha=0.4, label='Perfect')
        ax.plot(mean_pred, frac_pos, 'o-', color=color, label=label)
        ax.set_title(f'FG3M — {label}')
        ax.set_xlabel('Mean predicted probability')
        ax.set_ylabel('Fraction of positives')
        ax.set_xlim(0.3, 0.8); ax.set_ylim(0.3, 0.8)
        ax.grid(alpha=0.3)
        ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def main() -> None:
    print("\n  Phase 2 — FG3M Isotonic Calibration Experiment")
    print("  " + "=" * 60)

    if not LOG_PATH.exists():
        print(f"  ERROR: {LOG_PATH} not found. Run phase1_calibration_audit.py first.")
        sys.exit(1)

    log = pd.read_csv(LOG_PATH)
    fg  = log[log['stat'] == 'fg3m'].copy()
    fg['date'] = pd.to_datetime(fg['date'])
    fg = fg.sort_values('date').reset_index(drop=True)

    print(f"\n  FG3M rows: {len(fg):,}  ({fg['date'].min().date()} – {fg['date'].max().date()})")

    # Temporal split
    dates     = sorted(fg['date'].unique())
    n_dates   = len(dates)
    split_idx = int(n_dates * 0.60)
    split_dt  = dates[split_idx]

    cal_df  = fg[fg['date'] < split_dt].copy()
    eval_df = fg[fg['date'] >= split_dt].copy()

    print(f"  Cal:  {len(cal_df):>5} rows  ({cal_df['date'].min().date()} – {cal_df['date'].max().date()})  "
          f"[{split_idx} dates]")
    print(f"  Eval: {len(eval_df):>5} rows  ({eval_df['date'].min().date()} – {eval_df['date'].max().date()})  "
          f"[{n_dates - split_idx} dates]")

    # Drop rows where prob or hit are missing
    cal_df  = cal_df.dropna(subset=['prob_blend65', 'hit'])
    eval_df = eval_df.dropna(subset=['prob_blend65', 'hit'])

    cal_probs = cal_df['prob_blend65'].values.astype(float)
    cal_hits  = cal_df['hit'].values.astype(int)
    ev_probs  = eval_df['prob_blend65'].values.astype(float)
    ev_hits   = eval_df['hit'].values.astype(int)

    # ── Baseline (no calibration) on eval set ────────────────────────────────
    brier_before, ece_before = brier_ece(ev_probs, ev_hits)
    bt_before = bucket_table(ev_probs, ev_hits, 'gaussian_before')

    print(f"\n  Cal half stats  — hits={cal_hits.mean():.1%},  "
          f"mean_p={cal_probs.mean():.1%},  n={len(cal_df):,}")
    print(f"  Eval half stats — hits={ev_hits.mean():.1%},  "
          f"mean_p={ev_probs.mean():.1%},  n={len(eval_df):,}")

    # ── Fit isotonic on calibration half ─────────────────────────────────────
    print(f"\n  Fitting IsotonicRegression on cal half...")
    iso = IsotonicRegression(out_of_bounds='clip', y_min=0.01, y_max=0.99,
                             increasing=True)
    iso.fit(cal_probs, cal_hits)

    # Sanity: calibration performance on cal half (will look good — same data)
    cal_probs_iso = iso.predict(cal_probs)
    brier_cal_iso, ece_cal_iso = brier_ece(cal_probs_iso, cal_hits)
    brier_cal_raw, ece_cal_raw = brier_ece(cal_probs, cal_hits)
    print(f"  Cal half — raw:   Brier={brier_cal_raw:.5f}  ECE={ece_cal_raw:.5f}")
    print(f"  Cal half — iso:   Brier={brier_cal_iso:.5f}  ECE={ece_cal_iso:.5f}  (in-sample, expected to look good)")

    # ── Apply to eval half — this is the real test ────────────────────────────
    ev_probs_iso = iso.predict(ev_probs)
    brier_after, ece_after = brier_ece(ev_probs_iso, ev_hits)
    bt_after = bucket_table(ev_probs_iso, ev_hits, 'isotonic_after')

    # ── Inspect the learned mapping ───────────────────────────────────────────
    probe = np.arange(0.40, 0.85, 0.01)
    mapped = iso.predict(probe)
    print(f"\n  Learned probability mapping (Gaussian → Isotonic):")
    print(f"  {'Gaussian P':>12}  {'→ Iso P':>9}")
    for g, c in zip(probe[::5], mapped[::5]):
        shift = c - g
        print(f"  {g:>12.2f}  {c:>9.3f}  ({shift:+.3f})")

    # ── Results ────────────────────────────────────────────────────────────────
    db = brier_after - brier_before
    de = ece_after - ece_before

    print(f"\n  {'─'*60}")
    print(f"  EVAL HALF RESULTS (temporal holdout — honest comparison)")
    print(f"  {'─'*60}")
    print(f"  {'Metric':<15} {'Before (Gaussian)':>18} {'After (Isotonic)':>18} {'Delta':>8}")
    print(f"  {'-'*60}")
    print(f"  {'Brier score':<15} {brier_before:>18.5f} {brier_after:>18.5f} {db:>+8.5f}")
    print(f"  {'ECE':<15} {ece_before:>18.5f} {ece_after:>18.5f} {de:>+8.5f}")

    # Decision gate
    improved = brier_after < brier_before
    print(f"\n  Brier {'IMPROVED' if improved else 'DID NOT IMPROVE'} on holdout.")

    print(f"\n  Bucket hit rates — EVAL HALF:")
    merged = pd.merge(bt_before, bt_after, on='bucket', suffixes=('_before', '_after'), how='outer')
    print(f"  {'Bucket':>8}  {'n_b':>5}  {'Model_b':>9}  {'HR_b':>7}  {'Gap_b':>7}  │  "
          f"{'n_a':>5}  {'Model_a':>9}  {'HR_a':>7}  {'Gap_a':>7}  {'Δgap':>7}")
    print(f"  {'-'*90}")
    for _, row in merged.iterrows():
        bkt = row['bucket']
        if pd.isna(row.get('n_before', np.nan)):
            continue
        nb = int(row.get('n_before', 0))
        pb = float(row.get('avg_model_p_before', 0))
        hrb = float(row.get('actual_hr_before', 0))
        gb = float(row.get('gap_before', 0))
        na = int(row.get('n_after', 0)) if not pd.isna(row.get('n_after', np.nan)) else 0
        pa = float(row.get('avg_model_p_after', 0)) if not pd.isna(row.get('avg_model_p_after', np.nan)) else np.nan
        hra = float(row.get('actual_hr_after', 0)) if not pd.isna(row.get('actual_hr_after', np.nan)) else np.nan
        ga = float(row.get('gap_after', 0)) if not pd.isna(row.get('gap_after', np.nan)) else np.nan
        d_gap = ga - gb if not np.isnan(ga) else float('nan')
        flag = ' ✅' if (not np.isnan(d_gap) and abs(ga) < abs(gb) and abs(d_gap) > 0.005) else (
               ' ❌' if (not np.isnan(d_gap) and abs(ga) > abs(gb)) else '')
        print(f"  {bkt:>8}  {nb:>5}  {pb:>9.1%}  {hrb:>7.1%}  {gb:>+7.1%}  │  "
              f"{na:>5}  {pa:>9.1%}  {hra:>7.1%}  {ga:>+7.1%}  {d_gap:>+7.1%}{flag}")

    # Focus on the production bucket
    print(f"\n  KEY BUCKETS (production range 57-65%):")
    prod_before = bt_before[bt_before['bucket'].isin(['57-60', '60-65'])]
    prod_after  = bt_after[bt_after['bucket'].isin(['57-60', '60-65'])]

    for bkt in ['57-60', '60-65']:
        row_b = prod_before[prod_before['bucket'] == bkt]
        row_a = prod_after[prod_after['bucket'] == bkt]
        if row_b.empty:
            continue
        gb = float(row_b['gap'].iloc[0])
        ga = float(row_a['gap'].iloc[0]) if not row_a.empty else np.nan
        hrb = float(row_b['actual_hr'].iloc[0])
        nb = int(row_b['n'].iloc[0])
        better = abs(ga) < abs(gb) if not np.isnan(ga) else False
        print(f"    {bkt}: n={nb}  gap before={gb:+.1%}  gap after={ga:+.1%}  "
              f"{'→ IMPROVED ✅' if better else '→ no improvement ⚠️'}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    prod_bt_before = bt_before[bt_before['bucket'].isin(['55-57', '57-60', '60-65'])]
    prod_bt_after  = bt_after[bt_after['bucket'].isin(['55-57', '57-60', '60-65'])]

    # Pick counts in production range before/after — used to detect pick-elimination
    n_prod_before = int(prod_bt_before['n'].sum()) if not prod_bt_before.empty else 0
    n_prod_after  = int(prod_bt_after['n'].sum()) if not prod_bt_after.empty else 0
    elimination_ratio = (n_prod_before - n_prod_after) / max(n_prod_before, 1)
    gap_before_prod = float(prod_bt_before['gap'].mean()) if not prod_bt_before.empty else 0
    gap_after_prod  = float(prod_bt_after['gap'].mean()) if not prod_bt_after.empty else 0

    print(f"\n  Production pick volume (57-65%): {n_prod_before} → {n_prod_after} "
          f"({elimination_ratio:.0%} eliminated)")

    print(f"\n  {'─'*60}")
    print(f"  VERDICT")
    print(f"  {'─'*60}")
    if elimination_ratio >= 0.50:
        # Isotonic improved Brier by eliminating picks, not by making picks better.
        # This is a mu problem: the 57-65% FG3M picks don't actually have edge.
        verdict = (f"MU PROBLEM: Isotonic improved Brier by eliminating {elimination_ratio:.0%} of\n"
                   f"  production picks — not by making those picks more accurate.\n"
                   f"  The 57-65% FG3M range has genuine edge only at the Gaussian 65%+ tail.\n"
                   f"  Root cause: model mu predictions in the 57-65% range are not reliable\n"
                   f"  enough for production. Options:\n"
                   f"    1. Keep FG3M excluded at 57% threshold (current correct behavior)\n"
                   f"    2. Raise FG3M production threshold to 65%+ (few picks, but real edge)\n"
                   f"    3. Add FG3M-specific features (opponent 3P defense, shot profile)\n"
                   f"  Do NOT wire this calibrator into production — it would cut pick volume\n"
                   f"  by {elimination_ratio:.0%} without making the remaining picks better.")
        save = False
    elif improved and abs(gap_after_prod) < abs(gap_before_prod):
        verdict = ("PROCEED: isotonic calibration improves FG3M at production thresholds\n"
                   "  without eliminating most picks. Wire calibrator into production.")
        save = True
    elif improved:
        verdict = ("PARTIAL: Brier improves but production-threshold gap did not close.\n"
                   "  Root cause may be mu (point prediction), not CDF shape.")
        save = True
    else:
        verdict = ("HOLD: Isotonic did not improve eval-half Brier.\n"
                   "  Root cause is likely mu (point prediction bias), not the CDF.")
        save = False

    print(f"  {verdict}")

    # ── Save calibrator and plots ─────────────────────────────────────────────
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    cal_path = CAL_DIR / "fg3m_isotonic.pkl"
    with open(cal_path, 'wb') as f:
        pickle.dump({'calibrator': iso, 'split_date': str(split_dt.date()),
                     'brier_before': brier_before, 'brier_after': brier_after,
                     'ece_before': ece_before, 'ece_after': ece_after}, f)
    print(f"\n  Calibrator saved → {cal_path}")

    plot_path = OUT_DIR / "reliability_fg3m_isotonic_comparison.png"
    plot_reliability_comparison(ev_probs, ev_probs_iso, ev_hits, plot_path)
    print(f"  Plot saved → {plot_path}")

    # Save bucket comparison CSV
    bt_before['phase'] = 'before'
    bt_after['phase']  = 'after'
    comparison = pd.concat([bt_before, bt_after], ignore_index=True)
    comparison.to_csv(OUT_DIR / "fg3m_isotonic_bucket_comparison.csv", index=False)
    print(f"  Bucket table → {OUT_DIR / 'fg3m_isotonic_bucket_comparison.csv'}")

    print(f"\n  Brier: {brier_before:.5f} → {brier_after:.5f} ({db:+.5f})")
    print(f"  ECE:   {ece_before:.5f} → {ece_after:.5f} ({de:+.5f})")
    print()


if __name__ == "__main__":
    main()
