"""
Playoff Backtest Report Generator.

Runs the playoff backtest harness across historical seasons and produces a
markdown report with hit rates, calibration, slice analysis, residuals,
and L10/model/blend comparison.

Usage:
    python scripts/run_playoff_backtest.py
    python scripts/run_playoff_backtest.py --seasons 2024-25
    python scripts/run_playoff_backtest.py --seasons 2024-25 2023-24 2022-23
    python scripts/run_playoff_backtest.py --save-csv
"""

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from src.backtest.playoff_backtest import run_playoff_backtest, STATS


# ── Formatting helpers ────────────────────────────────────────────────────────

def _pct(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "  N/A"
    return f"{x:.1%}"


def _f(x, decimals=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "   N/A"
    return f"{x:.{decimals}f}"


def _sign(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "N/A"
    return f"{x:+.2f}"


# ── Section generators ────────────────────────────────────────────────────────

def section_hit_rates(df: pd.DataFrame) -> str:
    """Overall hit rates per stat at P≥65/70/75/80% — vs synthetic line."""
    lines = ["## 1. Hit Rates per Stat (synthetic market line)\n"]
    lines.append("*Line = L10 × playoff market ratio (PTS 0.909, REB 0.986, AST 0.769, "
                 "PRA 0.939, REB_AST 0.899)*\n")

    thresholds = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
    header = "| Stat | " + " | ".join(f"P≥{int(t*100)}% (N, HR)" for t in thresholds) + " |"
    sep    = "| --- |" + " --- |" * len(thresholds)
    lines.append(header)
    lines.append(sep)

    for stat in STATS:
        s_df = df[df['stat'] == stat]
        cells = []
        for t in thresholds:
            picks = s_df[s_df['p_over'] >= t]
            n = len(picks)
            if n < 10:
                cells.append("—")
            else:
                hr = picks['hit'].mean()
                cells.append(f"{n}, {_pct(hr)}")
        lines.append("| " + stat.upper() + " | " + " | ".join(cells) + " |")

    return "\n".join(lines)


def section_calibration(df: pd.DataFrame) -> str:
    """Decile calibration table: predicted p_over vs actual hit rate."""
    lines = ["## 2. Calibration by Decile (Predicted vs Actual)\n"]
    lines.append("*Does higher model confidence correspond to higher actual hit rate?*\n")

    for stat in STATS:
        s_df = df[df['stat'] == stat].copy()
        if len(s_df) < 50:
            continue
        lines.append(f"\n### {stat.upper()} (n={len(s_df):,})\n")
        lines.append("| Decile | N | Pred P(over) | Actual HR | Error |")
        lines.append("| --- | --- | --- | --- | --- |")
        try:
            s_df['decile'] = pd.qcut(s_df['p_over'], q=10, labels=False, duplicates='drop')
            cal = s_df.groupby('decile').agg(
                n=('hit', 'count'),
                pred=('p_over', 'mean'),
                actual=('hit', 'mean'),
            ).reset_index()
            for _, row in cal.iterrows():
                err = row['actual'] - row['pred']
                lines.append(f"| {int(row['decile'])} | {int(row['n'])} | "
                              f"{_pct(row['pred'])} | {_pct(row['actual'])} | {err:+.1%} |")
        except Exception as e:
            lines.append(f"| Error: {e} |")

    return "\n".join(lines)


def section_slice_analysis(df: pd.DataFrame) -> str:
    """Hit rate by round, game number, star/non-star, def tier, rotation tightening."""
    lines = ["## 3. Slice Analysis\n"]

    def _slice_table(df_sub: pd.DataFrame, slice_col: str, slice_vals: list,
                     slice_labels: dict, title: str, min_n: int = 30) -> list:
        out = [f"\n### {title}\n"]
        out.append("| Slice | " + " | ".join(f"{s.upper()} HR (N)" for s in STATS) + " |")
        out.append("| --- |" + " --- |" * len(STATS))
        for val in slice_vals:
            label = slice_labels.get(val, str(val))
            cells = []
            for stat in STATS:
                sub = df_sub[(df_sub[slice_col] == val) & (df_sub['stat'] == stat)]
                picks = sub[sub['p_over'] >= 0.55]
                if len(picks) < min_n:
                    cells.append("—")
                else:
                    cells.append(f"{picks['hit'].mean():.1%} ({len(picks)})")
            out.append(f"| {label} | " + " | ".join(cells) + " |")
        return out

    # 3a. By round
    round_vals  = ['R1', 'CSF', 'CF', 'Finals']
    round_labels = {v: v for v in round_vals}
    lines.extend(_slice_table(df, 'round', round_vals, round_labels,
                               "3a. By Round (P≥55%)"))

    # 3b. By game number in series
    g_vals   = [1, 2, 3, 4, 5, 6, 7]
    g_labels = {g: f"G{g}" for g in g_vals}
    lines.extend(_slice_table(df, 'game_in_series', g_vals, g_labels,
                               "3b. By Game Number in Series (P≥55%)", min_n=15))

    # 3c. Star vs non-star
    lines.extend(_slice_table(df, 'is_star', [1, 0],
                               {1: 'Star (≥32 min)', 0: 'Non-star'},
                               "3c. Star vs Non-Star (P≥55%)"))

    # 3d. Opponent defensive tier
    lines.extend(_slice_table(df, 'def_tier', ['top', 'mid', 'bot'],
                               {'top': 'Top-10 Def', 'mid': 'Mid Def', 'bot': 'Bot-10 Def'},
                               "3d. Opponent Defensive Tier (P≥55%)"))

    # 3e. Rotation tightening
    lines.extend(_slice_table(df, 'rotation_tightened', [1, 0],
                               {1: 'Rotation Tightened', 0: 'Normal Rotation'},
                               "3e. Rotation Tightening (P≥55%)"))

    return "\n".join(lines)


def section_residuals(df: pd.DataFrame) -> str:
    """Mean and median signed error (predicted - actual) per stat and slice."""
    lines = ["## 4. Residual Analysis (pred_blended - actual)\n"]
    lines.append("*Positive = model over-predicts. "
                 "Negative = model under-predicts.*\n")

    lines.append("| Stat | Mean Error | Median Error | MAE | N |")
    lines.append("| --- | --- | --- | --- | --- |")
    for stat in STATS:
        s_df = df[df['stat'] == stat]
        err = s_df['pred_blended'] - s_df['actual']
        lines.append(f"| {stat.upper()} | {_sign(err.mean())} | "
                     f"{_sign(err.median())} | {_f(err.abs().mean())} | {len(s_df)} |")

    lines.append("\n### 4a. Residuals by Round\n")
    lines.append("| Round | " + " | ".join(f"{s.upper()} MeanErr" for s in STATS) + " |")
    lines.append("| --- |" + " --- |" * len(STATS))
    for rnd in ['R1', 'CSF', 'CF', 'Finals']:
        cells = []
        for stat in STATS:
            sub = df[(df['round'] == rnd) & (df['stat'] == stat)]
            if len(sub) < 10:
                cells.append("—")
            else:
                err = (sub['pred_blended'] - sub['actual']).mean()
                cells.append(_sign(err))
        lines.append(f"| {rnd} | " + " | ".join(cells) + " |")

    lines.append("\n### 4b. Residuals by Star Status\n")
    lines.append("| Tier | " + " | ".join(f"{s.upper()} MeanErr" for s in STATS) + " |")
    lines.append("| --- |" + " --- |" * len(STATS))
    for star, label in [(1, 'Star (≥32 min)'), (0, 'Non-star')]:
        cells = []
        for stat in STATS:
            sub = df[(df['is_star'] == star) & (df['stat'] == stat)]
            if len(sub) < 10:
                cells.append("—")
            else:
                err = (sub['pred_blended'] - sub['actual']).mean()
                cells.append(_sign(err))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines.append("\n### 4c. Residuals by Game Number\n")
    lines.append("| Game | " + " | ".join(f"{s.upper()} MeanErr" for s in STATS) + " |")
    lines.append("| --- |" + " --- |" * len(STATS))
    for g in [1, 2, 3, 4, '5+']:
        cells = []
        for stat in STATS:
            if g == '5+':
                sub = df[(df['game_in_series'] >= 5) & (df['stat'] == stat)]
            else:
                sub = df[(df['game_in_series'] == g) & (df['stat'] == stat)]
            if len(sub) < 10:
                cells.append("—")
            else:
                err = (sub['pred_blended'] - sub['actual']).mean()
                cells.append(_sign(err))
        lines.append(f"| G{g} | " + " | ".join(cells) + " |")

    return "\n".join(lines)


def section_blend_comparison(df: pd.DataFrame) -> str:
    """Compare L10-only, model-only, and blended predictions."""
    lines = ["## 5. Blend Comparison: L10 vs Model vs 65/35 Blend\n"]

    lines.append("| Stat | L10 MAE | Model MAE | Blend MAE | Best | N |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for stat in STATS:
        s_df = df[df['stat'] == stat].dropna(subset=['pred_model_only', 'pred_l10_only'])
        if len(s_df) < 20:
            continue
        mae_l10   = (s_df['pred_l10_only']   - s_df['actual']).abs().mean()
        mae_model = (s_df['pred_model_only']  - s_df['actual']).abs().mean()
        mae_blend = (s_df['pred_blended']     - s_df['actual']).abs().mean()
        best = min([('L10', mae_l10), ('Model', mae_model), ('Blend', mae_blend)],
                   key=lambda x: x[1])[0]
        lines.append(f"| {stat.upper()} | {_f(mae_l10)} | {_f(mae_model)} | "
                     f"{_f(mae_blend)} | **{best}** | {len(s_df)} |")

    lines.append("\n### 5a. Hit Rate Comparison at P≥55% (by prediction source)\n")
    lines.append("*Each source's probability is computed independently against synthetic line.*\n")
    lines.append("| Stat | Blend HR (N) | L10 HR (N) | Blend Δ |")
    lines.append("| --- | --- | --- | --- |")
    for stat in STATS:
        s_df = df[df['stat'] == stat]
        from scipy import stats as scipy_stats
        # L10-only probability
        l10_p = s_df.apply(lambda r: float(np.clip(
            1 - scipy_stats.norm.cdf(
                (r['line_synthetic'] - r['pred_l10_only']) / max(r['l10_std'] * 1.10, 0.5)
            ), 0.01, 0.99
        )), axis=1)
        blend_picks = s_df[s_df['p_over'] >= 0.55]
        l10_picks   = s_df[l10_p >= 0.55]
        if len(blend_picks) < 10 or len(l10_picks) < 10:
            lines.append(f"| {stat.upper()} | — | — | — |")
            continue
        blend_hr = blend_picks['hit'].mean()
        l10_hr   = l10_picks['hit'].mean()
        delta = blend_hr - l10_hr
        lines.append(f"| {stat.upper()} | {_pct(blend_hr)} ({len(blend_picks)}) | "
                     f"{_pct(l10_hr)} ({len(l10_picks)}) | {delta:+.1%} |")

    return "\n".join(lines)


def section_summary(df: pd.DataFrame) -> str:
    """Executive summary: degraded stats, key slices, calibration verdict, blend verdict."""
    lines = ["## 6. Executive Summary\n"]

    # Which stats are most degraded (highest mean negative residual)
    lines.append("### Degradation by stat (pred_blended vs actual, median residual)\n")
    lines.append("| Stat | Median Residual | Over L10% | Assessment |")
    lines.append("| --- | --- | --- | --- |")
    for stat in STATS:
        s_df = df[df['stat'] == stat]
        med_err = (s_df['pred_blended'] - s_df['actual']).median()
        over_rate = (s_df['actual'] > s_df['line_synthetic']).mean()
        if over_rate < 0.45:
            assessment = "⚠️ Under-predicts (lower line or avoid)"
        elif over_rate > 0.55:
            assessment = "✅ Good OVER opportunity"
        else:
            assessment = "Neutral"
        lines.append(f"| {stat.upper()} | {_sign(med_err)} | {_pct(over_rate)} | {assessment} |")

    # Overall hit rates at best thresholds
    lines.append("\n### Overall at P≥55% and P≥60%\n")
    lines.append("| Threshold | N Picks | Hit Rate |")
    lines.append("| --- | --- | --- |")
    for t in [0.55, 0.60, 0.65]:
        picks = df[df['p_over'] >= t]
        if len(picks) < 10:
            lines.append(f"| P≥{int(t*100)}% | — | — |")
        else:
            lines.append(f"| P≥{int(t*100)}% | {len(picks)} | {_pct(picks['hit'].mean())} |")

    # Highest-leverage slices
    lines.append("\n### Highest-leverage slices (most degraded at P≥55%)\n")
    top_degraded = []
    for stat in STATS:
        for rnd in ['R1', 'CSF', 'CF', 'Finals']:
            sub = df[(df['stat'] == stat) & (df['round'] == rnd) & (df['p_over'] >= 0.55)]
            if len(sub) < 15:
                continue
            hr = sub['hit'].mean()
            mean_err = (sub['pred_blended'] - sub['actual']).mean()
            top_degraded.append((stat, rnd, len(sub), hr, mean_err))

    top_degraded.sort(key=lambda x: x[3])  # lowest hit rate first
    for stat, rnd, n, hr, err in top_degraded[:5]:
        lines.append(f"- **{stat.upper()} in {rnd}**: hit rate {_pct(hr)} (n={n}), "
                     f"mean error {_sign(err)}")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seasons', nargs='+', default=['2024-25'],
                        help='Playoff seasons to evaluate')
    parser.add_argument('--save-csv', action='store_true',
                        help='Save raw results CSV (see --csv-path for destination)')
    parser.add_argument('--save-report', action='store_true',
                        help='Save markdown report (see --report-path for destination)')
    parser.add_argument('--csv-path', type=str, default=None,
                        help='Override CSV output path (default: data/processed/playoff_backtest.csv)')
    parser.add_argument('--report-path', type=str, default=None,
                        help='Override report output path (default: PLAYOFF_BACKTEST_REPORT.md)')
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  PLAYOFF BACKTEST — {', '.join(args.seasons)}")
    print(f"  Synthetic lines | 65/35 blend (L10/model) | Per-stat elevation | Expanded stdev")
    print(f"{'='*70}\n")

    df = run_playoff_backtest(seasons=args.seasons, verbose=True)

    if df.empty:
        print("No results — check that playoff data is in the DB.")
        sys.exit(1)

    print(f"\n  Total rows in results: {len(df):,}")
    print(f"  Seasons:  {sorted(df['season'].unique())}")
    print(f"  Stats:    {sorted(df['stat'].unique())}")
    print(f"  Rounds:   {sorted(df['round'].unique())}")

    # Print key numbers to console
    print(f"\n{'='*70}")
    print(f"  QUICK RESULTS (P≥55% threshold, synthetic line)")
    print(f"{'='*70}")
    for stat in STATS:
        s_df = df[df['stat'] == stat]
        picks = s_df[s_df['p_over'] >= 0.55]
        if len(picks) < 10:
            print(f"  {stat.upper():<10}: {len(picks)} picks (insufficient)")
            continue
        hr = picks['hit'].mean()
        marker = '✅' if hr >= 0.57 else '⚠️'
        print(f"  {stat.upper():<10}: {len(picks):>4} picks, hit rate {hr:.1%} {marker}")

    print(f"\n{'='*70}")
    print(f"  THRESHOLD ANALYSIS (all stats combined)")
    print(f"{'='*70}")
    for t in [0.55, 0.57, 0.60, 0.63, 0.65, 0.70]:
        picks = df[df['p_over'] >= t]
        if len(picks) < 10:
            continue
        hr = picks['hit'].mean()
        marker = '✅' if hr >= 0.57 else '⚠️'
        print(f"  P≥{t:.0%}: {len(picks):>5} picks, hit rate {hr:.1%} {marker}")

    # Build full report
    report_sections = [
        f"# Playoff Backtest Report\n\n**Seasons:** {', '.join(args.seasons)}  \n"
        f"**Total eligible player-games:** {len(df):,}  \n"
        f"**Line proxy:** Synthetic market line (L10 × stat ratio)  \n"
        f"**Prediction:** 65% L10 + 35% model (with per-stat elevation)  \n"
        f"**Defensive tier:** Live — `team_stats` populated with regular-season DRTG for 2021-22 through 2025-26 "
        f"(150 rows, 5 seasons × 30 teams). Top-10/bot-10 DRTG classification active.  \n",
        section_hit_rates(df),
        section_calibration(df),
        section_slice_analysis(df),
        section_residuals(df),
        section_blend_comparison(df),
        section_summary(df),
    ]
    report = "\n\n---\n\n".join(report_sections)

    root = Path(__file__).parent.parent
    if args.save_report:
        out = Path(args.report_path) if args.report_path else root / "PLAYOFF_BACKTEST_REPORT.md"
        out.write_text(report)
        print(f"\n  Report saved to {out}")

    if args.save_csv:
        csv_path = Path(args.csv_path) if args.csv_path else root / "data" / "processed" / "playoff_backtest.csv"
        df.to_csv(csv_path, index=False)
        print(f"  Raw results saved to {csv_path}")

    print(f"\n{'='*70}")
    print(f"  Done. Add --save-report --save-csv to persist outputs.")
    print(f"{'='*70}\n")

    return df


if __name__ == "__main__":
    main()
