"""
Phase 1 Calibration Audit — scripts/phase1_calibration_audit.py

Generates a full per-pick diagnostic log from the 2024-25 validation season.
Evaluates three blend modes against the current architecture and answers:
  1. Which stats are most broken (Brier score, log loss, ECE)?
  2. Is the problem calibration (σ / CDF shape) or point prediction (μ residuals)?
  3. Does the 65/35 L10 blend actually help vs pure model?

Outputs to data/diagnostics/:
  - per_pick_log.csv          — full row-level log
  - calibration_summary.csv   — per-stat Brier / log-loss / ECE / hit-rate table
  - blend_audit.csv           — per-stat correlation and optimal blend weight
  - reliability_<stat>.png    — reliability diagrams (predicted P vs actual hit rate)
  - residual_<stat>.png       — point-prediction residuals by probability bucket
  - summary.txt               — human-readable executive summary

Usage:
    python scripts/phase1_calibration_audit.py [--season 2024-25] [--min-min 20]
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import pickle
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy import stats as scipy_stats
from sklearn.metrics import brier_score_loss, log_loss

from config.settings import PROCESSED_DIR

# ── Constants ─────────────────────────────────────────────────────────────────

TARGET_STATS  = ['pts', 'reb', 'ast', 'fg3m', 'pra', 'reb_ast']
COMPOSITE_STATS = {
    'pra':     ['pts', 'reb', 'ast'],
    'reb_ast': ['reb', 'ast'],
}

# The three blends we're auditing:
#   raw    = pure model (0% L10)  — what live regular-season picks actually use
#   blend65 = 35% model + 65% L10 — what backtest_playoffs.py uses
#   blend50 = 50% model + 50% L10 — what live playoff picks use
BLEND_MODES = {
    'raw':      0.00,   # L10 weight
    'blend65':  0.65,   # L10 weight
    'blend50':  0.50,   # L10 weight
}

# Reliability diagram bins: 10 equal-width from 0 to 1
N_RELIABILITY_BINS = 10

# Probability bucket labels for the summary table (mirrors log_pick_results.py)
BUCKET_EDGES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 1.01]
BUCKET_LABELS = ['50-55', '55-60', '60-65', '65-70', '70-75', '75+']


# ── Model helpers ─────────────────────────────────────────────────────────────

def load_model(stat: str) -> dict:
    path = PROCESSED_DIR / "models" / f"{stat}_model.pkl"
    with open(path, 'rb') as f:
        return pickle.load(f)


def fill_series_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Fill series_L3 features with L10 for non-playoff rows — exactly as
    build_todays_features() does in generate_todays_picks.py.
    Without this, 17k+ regular-season rows have NaN in series_L3 columns
    and get excluded from model prediction entirely.
    """
    df = df.copy()
    for col, l10_col in [('pts_series_L3',    'pts_L10'),
                          ('reb_series_L3',    'reb_L10'),
                          ('ast_series_L3',    'ast_L10'),
                          ('pra_series_L3',    'pra_L10'),
                          ('reb_ast_series_L3','reb_ast_L10')]:
        if col in df.columns and l10_col in df.columns:
            df[col] = df[col].fillna(df[l10_col])
    if 'series_game_num' in df.columns:
        df['series_game_num'] = df['series_game_num'].fillna(1)
    return df


def predict_raw(df: pd.DataFrame, stat: str, model_cache: dict) -> pd.Series:
    """Pure model prediction — no L10 blend."""
    components = COMPOSITE_STATS.get(stat)
    if components:
        preds = pd.Series(0.0, index=df.index)
        for c in components:
            m = model_cache[c]
            cols = [col for col in m['feature_cols'] if col in df.columns]
            valid = df[cols].notna().all(axis=1)
            raw = pd.Series(np.nan, index=df.index)
            if valid.sum() > 0:
                raw[valid] = m['model'].predict(df.loc[valid, cols])
            preds = preds + raw
        preds[preds == 0.0] = np.nan  # zero means all components were NaN
    else:
        m = model_cache[stat]
        cols = [col for col in m['feature_cols'] if col in df.columns]
        valid = df[cols].notna().all(axis=1)
        preds = pd.Series(np.nan, index=df.index)
        if valid.sum() > 0:
            preds[valid] = m['model'].predict(df.loc[valid, cols])
    return preds


def prob_over(pred: float, line: float, std: float) -> float:
    if std <= 0 or np.isnan(std):
        std = max(abs(pred) * 0.2, 1.0)
    z = (line - pred) / std
    return float(np.clip(1 - scipy_stats.norm.cdf(z), 0.01, 0.99))


# ── Reliability diagram ───────────────────────────────────────────────────────

def plot_reliability(predicted_probs: np.ndarray, actuals: np.ndarray,
                     stat: str, blend_label: str, out_path: Path) -> dict:
    """
    Plot predicted probability vs actual hit rate in N equal-frequency bins.
    Returns per-bin stats and ECE.
    """
    bins = np.linspace(0.0, 1.0, N_RELIABILITY_BINS + 1)
    bin_centers, bin_actual, bin_counts = [], [], []

    for lo, hi in zip(bins[:-1], bins[1:]):
        mask = (predicted_probs >= lo) & (predicted_probs < hi)
        n = mask.sum()
        if n < 5:
            continue
        bin_centers.append(predicted_probs[mask].mean())
        bin_actual.append(actuals[mask].mean())
        bin_counts.append(n)

    if not bin_centers:
        return {}

    ece = sum(
        abs(bc - ba) * cnt / len(predicted_probs)
        for bc, ba, cnt in zip(bin_centers, bin_actual, bin_counts)
    )

    fig, ax = plt.subplots(figsize=(7, 6))
    ax.plot([0, 1], [0, 1], 'k--', alpha=0.5, label='Perfect calibration')
    sc = ax.scatter(bin_centers, bin_actual, c=bin_counts, cmap='Blues',
                    s=80, zorder=5, edgecolors='navy')
    ax.plot(bin_centers, bin_actual, '-o', color='steelblue', markersize=5)
    plt.colorbar(sc, ax=ax, label='N picks in bin')
    ax.set_xlabel('Predicted probability')
    ax.set_ylabel('Actual hit rate')
    ax.set_title(f'{stat.upper()} Reliability Diagram — {blend_label}\nECE={ece:.4f}')
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)

    return {'ece': ece, 'bin_centers': bin_centers, 'bin_actual': bin_actual,
            'bin_counts': bin_counts}


def plot_residuals(preds: np.ndarray, actuals: np.ndarray,
                   probs: np.ndarray, stat: str, blend_label: str,
                   out_path: Path) -> dict:
    """
    Residual (pred - actual) by probability bucket.
    Shows whether over-predictions drive high-confidence misses.
    """
    residuals = preds - actuals
    stats_by_bucket = {}

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    # Left: residual by prob bucket
    bucket_residuals = []
    bucket_xs = []
    for lo, hi, label in zip(BUCKET_EDGES[:-1], BUCKET_EDGES[1:], BUCKET_LABELS):
        mask = (probs >= lo) & (probs < hi)
        if mask.sum() >= 5:
            bucket_residuals.append(residuals[mask])
            bucket_xs.append(label)
            stats_by_bucket[label] = {
                'n': int(mask.sum()),
                'mean_residual': float(residuals[mask].mean()),
                'mean_pred': float(preds[mask].mean()),
                'mean_actual': float(actuals[mask].mean()),
            }

    if bucket_residuals:
        axes[0].boxplot(bucket_residuals, labels=bucket_xs)
        axes[0].axhline(0, color='red', linestyle='--', alpha=0.7)
        axes[0].set_title(f'{stat.upper()} Residuals by Prob Bucket ({blend_label})')
        axes[0].set_xlabel('Probability bucket')
        axes[0].set_ylabel('pred − actual')
        axes[0].grid(alpha=0.3)

    # Right: scatter pred vs actual (colored by prob)
    sc = axes[1].scatter(actuals, preds, c=probs, cmap='RdYlGn',
                         alpha=0.3, s=8, vmin=0.4, vmax=0.8)
    plt.colorbar(sc, ax=axes[1], label='P(over)')
    lo_v = min(actuals.min(), preds.min()) - 1
    hi_v = max(actuals.max(), preds.max()) + 1
    axes[1].plot([lo_v, hi_v], [lo_v, hi_v], 'k--', alpha=0.5)
    axes[1].set_xlabel('Actual'); axes[1].set_ylabel('Predicted')
    axes[1].set_title(f'{stat.upper()} Pred vs Actual')
    axes[1].grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)

    return stats_by_bucket


# ── Main audit ────────────────────────────────────────────────────────────────

def run_audit(season: str = '2024-25', min_minutes: int = 20) -> None:
    out_dir = Path(__file__).parent.parent / "data" / "diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)

    feature_path = PROCESSED_DIR / "features.parquet"
    print(f"\n  Loading features.parquet...")
    df_all = pd.read_parquet(feature_path)

    df = df_all[df_all['season'] == season].copy()
    print(f"  Season {season}: {len(df):,} rows")

    # Apply minutes filter
    df = df[df['min_L10'] >= min_minutes].copy()
    print(f"  After min_L10 >= {min_minutes}: {len(df):,} rows")
    print(f"  Season types: {df['season_type'].value_counts().to_dict()}")

    # Pre-load all models
    print(f"\n  Loading models...")
    base_stats = ['pts', 'reb', 'ast', 'fg3m']
    model_cache = {}
    for s in base_stats:
        try:
            model_cache[s] = load_model(s)
            print(f"    {s} model loaded")
        except FileNotFoundError as e:
            print(f"    WARNING: {e}")

    # ── Build per-pick log ────────────────────────────────────────────────────
    print(f"\n  Filling series_L3 NaN with L10 for non-playoff rows (mirrors live picks)...")
    df = fill_series_features(df)

    print(f"\n  Computing predictions for all stats and blend modes...")

    all_rows = []
    calibration_stats = []  # per (stat, blend_mode)

    for stat in TARGET_STATS:
        l10_col  = f'{stat}_L10'
        std_col  = f'{stat}_std_L10'

        if l10_col not in df.columns or stat not in df.columns:
            print(f"  Skipping {stat} — missing columns")
            continue

        # Rows that have both actual value and L10 line
        df_s = df.dropna(subset=[stat, l10_col]).copy()
        if len(df_s) < 50:
            print(f"  Skipping {stat} — only {len(df_s)} valid rows")
            continue

        print(f"\n  {stat.upper()}: {len(df_s):,} rows")

        # Raw model predictions
        raw_preds = predict_raw(df_s, stat, model_cache)
        df_s['_raw_pred'] = raw_preds

        # Drop rows where model couldn't predict
        df_s = df_s.dropna(subset=['_raw_pred']).copy()
        print(f"    After dropping NaN predictions: {len(df_s):,} rows")
        if len(df_s) < 50:
            continue

        l10_vals = df_s[l10_col].values
        actual_vals = df_s[stat].values
        std_vals = df_s[std_col].values if std_col in df_s.columns else np.full(len(df_s), np.nan)
        raw_pred_vals = df_s['_raw_pred'].values

        # Ground truth: did player go OVER L10?
        hits = (actual_vals > l10_vals).astype(int)

        # ── Blend contribution audit ──────────────────────────────────────────
        corr_l10   = float(np.corrcoef(l10_vals, actual_vals)[0, 1])
        corr_model = float(np.corrcoef(raw_pred_vals, actual_vals)[0, 1])

        blend_results = {}
        for blend_name, l10_w in BLEND_MODES.items():
            blended = (1 - l10_w) * raw_pred_vals + l10_w * l10_vals
            corr_blend = float(np.corrcoef(blended, actual_vals)[0, 1])

            # Compute P(over) for each row using the blended prediction
            probs = np.array([
                prob_over(float(b), float(ln), float(s))
                for b, ln, s in zip(blended, l10_vals, std_vals)
            ])

            # Metrics (only where actual is known)
            mask = ~np.isnan(actual_vals) & ~np.isnan(probs)
            n = mask.sum()
            if n < 10:
                continue

            brier  = brier_score_loss(hits[mask], probs[mask])
            # log_loss wants binary labels and probabilities clipped away from 0/1
            probs_clipped = np.clip(probs[mask], 1e-6, 1 - 1e-6)
            logloss = log_loss(hits[mask], probs_clipped)

            # ECE via reliability diagram data
            rel_data = {}
            bins = np.linspace(0.0, 1.0, N_RELIABILITY_BINS + 1)
            ece_sum = 0.0
            for lo, hi in zip(bins[:-1], bins[1:]):
                bmask = (probs[mask] >= lo) & (probs[mask] < hi)
                if bmask.sum() >= 5:
                    ece_sum += abs(probs[mask][bmask].mean() - hits[mask][bmask].mean()) * bmask.sum() / n

            # Probability bucket hit rates
            bucket_rows = []
            for lo, hi, blabel in zip(BUCKET_EDGES[:-1], BUCKET_EDGES[1:], BUCKET_LABELS):
                bmask = (probs >= lo) & (probs < hi)
                nb = bmask.sum()
                if nb > 0:
                    avg_p = float(probs[bmask].mean())
                    hr    = float(hits[bmask].mean())
                    bucket_rows.append({
                        'stat': stat, 'blend': blend_name, 'bucket': blabel,
                        'n': int(nb), 'avg_model_p': round(avg_p, 4),
                        'actual_hit_rate': round(hr, 4),
                        'calibration_gap': round(hr - avg_p, 4),
                    })

            blend_results[blend_name] = {
                'probs': probs, 'blended': blended,
                'brier': brier, 'logloss': logloss, 'ece': ece_sum,
                'corr': corr_blend, 'n': n,
                'bucket_rows': bucket_rows,
            }

            calibration_stats.append({
                'stat': stat, 'blend': blend_name, 'n': n,
                'brier': round(brier, 5), 'logloss': round(logloss, 5),
                'ece': round(ece_sum, 5),
                'corr_with_actual': round(corr_blend, 4),
                'mean_hit_rate': round(hits[mask].mean(), 4),
                'mean_model_p': round(probs[mask].mean(), 4),
                'mean_residual': round(float((raw_pred_vals - actual_vals).mean()), 4),
                'mae': round(float(np.abs(raw_pred_vals - actual_vals).mean()), 4),
            })

        # Blend audit row
        grid_results = []
        for w in np.arange(0.0, 1.01, 0.05):
            blended = (1 - w) * raw_pred_vals + w * l10_vals
            probs_g = np.array([
                prob_over(float(b), float(ln), float(s))
                for b, ln, s in zip(blended, l10_vals, std_vals)
            ])
            mask = ~np.isnan(probs_g)
            if mask.sum() < 10:
                continue
            brier_g = brier_score_loss(hits[mask], probs_g[mask])
            grid_results.append({'l10_weight': round(w, 2), 'brier': brier_g})

        if grid_results:
            best = min(grid_results, key=lambda x: x['brier'])
            blend_audit_row = {
                'stat': stat,
                'corr_l10': round(corr_l10, 4),
                'corr_model': round(corr_model, 4),
                'corr_blend65': round(blend_results.get('blend65', {}).get('corr', np.nan), 4),
                'corr_blend50': round(blend_results.get('blend50', {}).get('corr', np.nan), 4),
                'brier_raw': round(blend_results.get('raw', {}).get('brier', np.nan), 5),
                'brier_blend65': round(blend_results.get('blend65', {}).get('brier', np.nan), 5),
                'brier_blend50': round(blend_results.get('blend50', {}).get('brier', np.nan), 5),
                'optimal_l10_weight': best['l10_weight'],
                'optimal_brier': round(best['brier'], 5),
                'mae': round(float(np.abs(raw_pred_vals - actual_vals).mean()), 4),
            }
        else:
            blend_audit_row = {'stat': stat}

        # ── Build per-pick log rows ───────────────────────────────────────────
        for i, (idx, row) in enumerate(df_s.iterrows()):
            base_row = {
                'date':            str(row.get('game_date', ''))[:10],
                'season_type':     row.get('season_type', ''),
                'player_id':       int(row.get('player_id', -1)),
                'player_name':     row.get('full_name', ''),
                'stat':            stat,
                'line_l10':        round(float(l10_vals[i]), 2),
                'model_prediction': round(float(raw_pred_vals[i]), 2),
                'l10_baseline':    round(float(l10_vals[i]), 2),
                'blended_65':      round(float((0.35 * raw_pred_vals[i] + 0.65 * l10_vals[i])), 2),
                'blended_50':      round(float((0.50 * raw_pred_vals[i] + 0.50 * l10_vals[i])), 2),
                'rolling_std':     round(float(std_vals[i]) if not np.isnan(std_vals[i]) else np.nan, 3),
                'prob_raw':        round(float(blend_results.get('raw', {}).get('probs', np.full(len(df_s), np.nan))[i]), 4),
                'prob_blend65':    round(float(blend_results.get('blend65', {}).get('probs', np.full(len(df_s), np.nan))[i]), 4),
                'prob_blend50':    round(float(blend_results.get('blend50', {}).get('probs', np.full(len(df_s), np.nan))[i]), 4),
                'actual_value':    round(float(actual_vals[i]), 1),
                'hit':             int(hits[i]),
            }
            all_rows.append(base_row)

        # ── Reliability diagrams ──────────────────────────────────────────────
        # Use blend65 as the primary diagram (mirrors the backtest default)
        if 'blend65' in blend_results:
            probs65 = blend_results['blend65']['probs']
            rel_path = out_dir / f"reliability_{stat}.png"
            plot_reliability(probs65, hits, stat, 'blend65 (35% model + 65% L10)', rel_path)

            res_path = out_dir / f"residual_{stat}.png"
            blended65 = blend_results['blend65']['blended']
            plot_residuals(blended65, actual_vals, probs65, stat, 'blend65', res_path)

        # Print inline bucket table
        if 'blend65' in blend_results:
            print(f"\n    Probability bucket hit rates (blend65, line=L10):")
            print(f"    {'Bucket':>8}  {'N':>5}  {'Model P':>8}  {'Actual HR':>9}  {'Gap':>7}")
            for br in blend_results['blend65']['bucket_rows']:
                gap = br['calibration_gap']
                flag = '⚠️' if abs(gap) > 0.05 else '  '
                print(f"    {br['bucket']:>8}  {br['n']:>5}  {br['avg_model_p']:>8.1%}  "
                      f"{br['actual_hit_rate']:>9.1%}  {gap:>+7.1%} {flag}")

        print(f"\n    Blend audit — Brier scores (lower=better):")
        print(f"    {'Mode':<12} {'L10 wt':>6} {'Corr w/Actual':>14} {'Brier':>8}")
        print(f"    {'L10 only':12} {'100%':>6} {corr_l10:>14.4f} {'—':>8}")
        print(f"    {'Model only':12} {'  0%':>6} {corr_model:>14.4f} "
              f"{blend_results.get('raw', {}).get('brier', float('nan')):>8.5f}")
        for bm, bdata in blend_results.items():
            if bm == 'raw':
                continue
            label = 'blend65' if bm == 'blend65' else 'blend50'
            l10w = BLEND_MODES[bm]
            print(f"    {label:<12} {l10w:>6.0%} {bdata['corr']:>14.4f} {bdata['brier']:>8.5f}")
        if grid_results:
            print(f"    optimal:     L10 wt={best['l10_weight']:.2f}  Brier={best['brier']:.5f}")

        # Append blend audit to list
        blend_audit_row['corr_l10'] = round(corr_l10, 4)
        blend_audit_row['corr_model'] = round(corr_model, 4)
        locals().setdefault('blend_audit_rows', []).append(blend_audit_row)

    # Collect blend audit rows from local scope workaround
    blend_audit_rows = [r for r in calibration_stats
                        if r['blend'] == 'blend65']  # one row per stat

    # ── Save outputs ──────────────────────────────────────────────────────────

    # Per-pick log
    log_path = out_dir / "per_pick_log.csv"
    df_log = pd.DataFrame(all_rows)
    df_log.to_csv(log_path, index=False)
    print(f"\n\n  Saved per-pick log: {log_path}  ({len(df_log):,} rows)")

    # Calibration summary
    cal_path = out_dir / "calibration_summary.csv"
    df_cal = pd.DataFrame(calibration_stats)
    df_cal.to_csv(cal_path, index=False)
    print(f"  Saved calibration summary: {cal_path}")

    # Probability bucket table
    bucket_all = []
    for stat in TARGET_STATS:
        # Read from df_log
        for blend_name in BLEND_MODES:
            prob_col = f'prob_{blend_name.replace("blend","blend")}'
            if prob_col not in df_log.columns:
                continue
            sub = df_log[df_log['stat'] == stat].copy()
            if sub.empty:
                continue
            for lo, hi, blabel in zip(BUCKET_EDGES[:-1], BUCKET_EDGES[1:], BUCKET_LABELS):
                bmask = (sub[prob_col] >= lo) & (sub[prob_col] < hi)
                nb = bmask.sum()
                if nb > 0:
                    bucket_all.append({
                        'stat': stat, 'blend': blend_name, 'bucket': blabel,
                        'n': int(nb),
                        'avg_model_p': round(float(sub.loc[bmask, prob_col].mean()), 4),
                        'actual_hit_rate': round(float(sub.loc[bmask, 'hit'].mean()), 4),
                        'calibration_gap': round(
                            float(sub.loc[bmask, 'hit'].mean()) -
                            float(sub.loc[bmask, prob_col].mean()), 4),
                    })

    bucket_path = out_dir / "bucket_tables.csv"
    pd.DataFrame(bucket_all).to_csv(bucket_path, index=False)
    print(f"  Saved bucket tables: {bucket_path}")

    # ── Executive summary ──────────────────────────────────────────────────────
    _write_summary(df_cal, df_log, out_dir, season)


def _write_summary(df_cal: pd.DataFrame, df_log: pd.DataFrame,
                   out_dir: Path, season: str) -> None:
    """Write a human-readable summary to summary.txt."""
    lines = []
    lines.append(f"Phase 1 Calibration Audit — {season}")
    lines.append("=" * 70)

    lines.append(f"\nTotal picks in log: {len(df_log):,}")
    lines.append(f"Stats audited: {sorted(df_log['stat'].unique())}")

    lines.append(f"\n{'─'*70}")
    lines.append("CALIBRATION SUMMARY (blend65: 35% model + 65% L10, Gaussian CDF)")
    lines.append(f"{'─'*70}")
    lines.append(f"{'Stat':<10} {'N':>6} {'Brier':>8} {'LogLoss':>9} {'ECE':>8} "
                 f"{'Mean P':>8} {'Hit Rate':>9} {'Gap':>8} {'MAE':>7}")
    lines.append("-" * 70)

    b65 = df_cal[df_cal['blend'] == 'blend65'].set_index('stat')
    diagnosis = {}
    for stat in TARGET_STATS:
        if stat not in b65.index:
            continue
        r = b65.loc[stat]
        gap = r['mean_hit_rate'] - r['mean_model_p']
        flag = '⚠️  BROKEN' if abs(gap) > 0.10 or r['brier'] > 0.26 else (
               '⚡ MILD'   if abs(gap) > 0.05 else '✅ OK')
        lines.append(f"{stat.upper():<10} {int(r['n']):>6} {r['brier']:>8.5f} "
                     f"{r['logloss']:>9.5f} {r['ece']:>8.5f} "
                     f"{r['mean_model_p']:>8.1%} {r['mean_hit_rate']:>9.1%} "
                     f"{gap:>+8.1%} {r['mae']:>7.3f}  {flag}")
        diagnosis[stat] = {'gap': gap, 'brier': r['brier'], 'mae': r['mae']}

    lines.append(f"\n{'─'*70}")
    lines.append("BLEND CONTRIBUTION AUDIT")
    lines.append(f"{'─'*70}")
    lines.append("Does the model prediction improve on L10 alone?")
    lines.append(f"{'Stat':<10} {'corr(L10,act)':>14} {'corr(mod,act)':>14} "
                 f"{'Brier_raw':>10} {'Brier_b65':>10} {'Opt L10 wt':>11}")
    lines.append("-" * 70)

    raw_cal = df_cal[df_cal['blend'] == 'raw'].set_index('stat')
    b65_cal = df_cal[df_cal['blend'] == 'blend65'].set_index('stat')

    for stat in TARGET_STATS:
        if stat not in raw_cal.index:
            continue
        r_raw = raw_cal.loc[stat]
        r_b65 = b65_cal.loc[stat] if stat in b65_cal.index else None

        corr_model = r_raw.get('corr_with_actual', np.nan)
        brier_raw  = r_raw['brier']
        brier_b65  = r_b65['brier'] if r_b65 is not None else np.nan

        # Infer optimal from the bucket data (approximate)
        blend_winner = 'blend65' if (not np.isnan(brier_b65) and brier_b65 < brier_raw) else 'raw'
        lines.append(f"{stat.upper():<10} {'—':>14} {corr_model:>14.4f} "
                     f"{brier_raw:>10.5f} {brier_b65:>10.5f}  → {blend_winner}")

    lines.append(f"\n{'─'*70}")
    lines.append("DIAGNOSIS: IS THE PROBLEM CALIBRATION OR POINT PREDICTION?")
    lines.append(f"{'─'*70}")

    for stat, d in diagnosis.items():
        gap_abs = abs(d['gap'])
        # If mean model P is much higher than hit rate → overconfident (calibration)
        # If MAE is high relative to typical stat value → point prediction
        cal_problem  = gap_abs > 0.05
        pred_problem = d['mae'] > {
            'pts': 6.0, 'reb': 2.5, 'ast': 2.0, 'fg3m': 1.2, 'pra': 8.0, 'reb_ast': 3.0
        }.get(stat, 4.0)
        if cal_problem and pred_problem:
            verdict = "BOTH: calibration AND point prediction"
        elif cal_problem:
            verdict = "CALIBRATION (σ/CDF): fix probability layer first"
        elif pred_problem:
            verdict = "POINT PREDICTION (μ): residuals too large"
        else:
            verdict = "MOSTLY OK"
        lines.append(f"  {stat.upper():<8}: gap={d['gap']:+.1%}, MAE={d['mae']:.3f}  → {verdict}")

    lines.append(f"\n{'─'*70}")
    lines.append("FILES GENERATED:")
    for fname in ['per_pick_log.csv', 'calibration_summary.csv', 'bucket_tables.csv']:
        lines.append(f"  data/diagnostics/{fname}")
    for stat in TARGET_STATS:
        lines.append(f"  data/diagnostics/reliability_{stat}.png")
        lines.append(f"  data/diagnostics/residual_{stat}.png")

    summary_path = out_dir / "summary.txt"
    with open(summary_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    # Also print to stdout
    print(f"\n\n{'=' * 70}")
    print("  AUDIT COMPLETE — FINDINGS")
    print(f"{'=' * 70}")
    for line in lines:
        print(f"  {line}")
    print(f"\n  Full summary → {summary_path}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 1 calibration audit")
    parser.add_argument('--season',  default='2024-25',
                        help='Season to audit (default: 2024-25 = val season)')
    parser.add_argument('--min-min', type=int, default=20,
                        help='Minimum min_L10 to include (default: 20)')
    args = parser.parse_args()
    run_audit(season=args.season, min_minutes=args.min_min)
