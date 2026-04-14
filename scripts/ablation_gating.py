"""
Policy-gating ablation — playoff pick filtering by tier threshold.

Operates entirely on the saved backtest CSV (no harness re-run needed —
p_over values are fixed; only the pick inclusion filter changes).

Diagnostic observations driving config design:
  CORE:     61.6%@55%, 62.6%@60%, 63.4%@63%, 65.4%@65%, 69.4%@70%  → strong throughout
  ROTATION: 54.8%@55%, 55.9%@60%, 60.1%@65%, 64.5%@70%            → meaningful jump at P≥65%
  STAR:     57.5%@55%, 57.7%@60%, 58.3%@65%, 61.2%@70%            → flat until P≥70%

  ROTATION per-stat at P≥55%:
    PTS:    49.0% — worst; non-monotone at low thresholds
    REB:    53.5% — monotone, fine at P≥60%
    AST:    58.1% — already well-filtered by PRED_MULT branch (no picks at P≥63%)
    PRA:    55.2% — 70.0% at P≥65%
    REB_AST:57.6% — solid

  CORE per-stat at P≥55%: all ≥59% (REB) to 65.5% (PTS). Raising threshold wastes good picks.
  STAR per-stat: PTS 53.8% is the drag. Everything else ≥55.8%.

Configs tested:
  0. Baseline:           STAR≥55%, CORE≥55%, ROTATION≥55%
  1. Modest ROTATION:    STAR≥55%, CORE≥55%, ROTATION≥60%
  2. Aggressive ROTATION:STAR≥55%, CORE≥55%, ROTATION≥65%
  3. ROTATION suppressed:STAR≥55%, CORE≥55%, ROTATION≥70%
  4. Dual-gate:          STAR≥55%, CORE≥60%, ROTATION≥65%
  5. Surgical STAR PTS:  STAR PTS≥65% (other STAR stats≥55%), CORE≥55%, ROTATION≥65%
     (STAR PTS is 53.8% at P≥55%, 50.9% at P≥65% — actually non-monotone; keep for info)

Usage:
    python scripts/ablation_gating.py [--save]
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

CSV_PATH = Path(__file__).parent.parent / 'data' / 'processed' / 'playoff_backtest.csv'

STATS  = ['pts', 'reb', 'ast', 'pra', 'reb_ast']
TIERS  = ['STAR', 'CORE', 'ROTATION']


# ── Gating configs ─────────────────────────────────────────────────────────────
# Each config is a dict: {(tier, stat): min_p_over}
# A missing key means no picks from that tier×stat.
# 'default' key used where no stat-specific override exists.

def _uniform_tier(star: float, core: float, rot: float) -> dict:
    """Uniform threshold per tier across all stats."""
    out = {}
    for stat in STATS:
        out[('STAR',     stat)] = star
        out[('CORE',     stat)] = core
        out[('ROTATION', stat)] = rot
    return out


CONFIGS = {
    '0_baseline': {
        'label': '0. Baseline (92899eb) — uniform P≥55%',
        'gates': _uniform_tier(0.55, 0.55, 0.55),
    },
    '1_modest_rot': {
        'label': '1. Modest ROTATION gate — ROT≥60%',
        'gates': _uniform_tier(0.55, 0.55, 0.60),
    },
    '2_aggr_rot': {
        'label': '2. Aggressive ROTATION gate — ROT≥65%',
        'gates': _uniform_tier(0.55, 0.55, 0.65),
    },
    '3_rot_suppressed': {
        'label': '3. ROTATION suppressed — ROT≥70%',
        'gates': _uniform_tier(0.55, 0.55, 0.70),
    },
    '4_dual': {
        'label': '4. Dual-gate — CORE≥60%, ROT≥65%',
        'gates': _uniform_tier(0.55, 0.60, 0.65),
    },
    '5_surgical': {
        'label': '5. Surgical — STAR PTS≥65%, CORE≥58%, ROT≥65%',
        'gates': {
            **{('STAR', s): 0.55 for s in STATS},
            ('STAR',     'pts'): 0.65,   # STAR PTS 53.8% at 55%, needs filtering
            **{('CORE', s): 0.58 for s in STATS},
            **{('ROTATION', s): 0.65 for s in STATS},
        },
    },
}


# ── Filtering ─────────────────────────────────────────────────────────────────

def apply_gates(df: pd.DataFrame, gates: dict) -> pd.DataFrame:
    """Filter rows according to tier×stat threshold gates."""
    masks = []
    for (tier, stat), thresh in gates.items():
        mask = (df['tier'] == tier) & (df['stat'] == stat) & (df['p_over'] >= thresh)
        masks.append(mask)
    if not masks:
        return df.iloc[0:0]
    combined = masks[0]
    for m in masks[1:]:
        combined = combined | m
    return df[combined].copy()


# ── Metrics ───────────────────────────────────────────────────────────────────

def _hr(x):   return f"{x:.1%}" if x is not None and not np.isnan(x) else "  N/A"
def _n(x):    return f"n={x:,}"
def _sign(x): return f"{x:+.1%}" if x is not None and not np.isnan(x) else "    N/A"


def threshold_metrics(df: pd.DataFrame) -> dict:
    out = {}
    for t in [0.55, 0.60, 0.65, 0.70]:
        picks = df[df.p_over >= t]
        out[t] = (picks.hit.mean() if len(picks) > 5 else None, len(picks))
    return out


def pick_table(df: pd.DataFrame) -> pd.DataFrame:
    """N and HR for every tier×stat combination."""
    rows = []
    for stat in STATS:
        for tier in TIERS:
            sub = df[(df.stat == stat) & (df.tier == tier)]
            rows.append({
                'stat': stat, 'tier': tier,
                'n': len(sub),
                'hr': sub.hit.mean() if len(sub) > 5 else None,
            })
    return pd.DataFrame(rows)


def summary_metrics(df: pd.DataFrame) -> dict:
    tm = threshold_metrics(df)
    out = {
        't55_hr': tm[0.55][0], 't55_n': tm[0.55][1],
        't60_hr': tm[0.60][0], 't60_n': tm[0.60][1],
        't65_hr': tm[0.65][0], 't65_n': tm[0.65][1],
        't70_hr': tm[0.70][0], 't70_n': tm[0.70][1],
    }
    for tier in TIERS:
        t = df[df.tier == tier]
        out[f'{tier}_hr'] = t.hit.mean() if len(t) > 5 else None
        out[f'{tier}_n']  = len(t)
    for stat in STATS:
        s = df[df.stat == stat]
        out[f'{stat}_hr'] = s.hit.mean() if len(s) > 5 else None
        out[f'{stat}_n']  = len(s)
    return out


def print_config_block(label: str, df: pd.DataFrame, base_df: pd.DataFrame | None = None) -> None:
    sm = summary_metrics(df)
    print(f"\n{'─'*68}")
    print(f"  {label}")
    print(f"  Total picks: {len(df):,}")
    print(f"{'─'*68}")

    print(f"\n  Overall threshold performance:")
    for t, key in [(0.55,'t55'),(0.60,'t60'),(0.65,'t65'),(0.70,'t70')]:
        hr, n = sm[f'{key}_hr'], sm[f'{key}_n']
        delta = ''
        if base_df is not None:
            base_sm = summary_metrics(base_df)
            bhr = base_sm[f'{key}_hr']
            if hr is not None and bhr is not None:
                delta = f"  (Δ{hr-bhr:+.1%} vs baseline)"
        print(f"    P≥{t:.0%}: {_hr(hr):>6}  ({_n(n)}){delta}")

    print(f"\n  Hit rate by tier (all stats included):")
    for tier in TIERS:
        hr, n = sm[f'{tier}_hr'], sm[f'{tier}_n']
        delta = ''
        if base_df is not None:
            base_sm = summary_metrics(base_df)
            bhr = base_sm[f'{tier}_hr']
            if hr is not None and bhr is not None:
                delta = f"  (Δ{hr-bhr:+.1%})"
        print(f"    {tier:<10}: {_hr(hr):>6}  ({_n(n)}){delta}")

    print(f"\n  Hit rate by stat (all tiers included):")
    for stat in STATS:
        hr, n = sm[f'{stat}_hr'], sm[f'{stat}_n']
        delta = ''
        if base_df is not None:
            base_sm = summary_metrics(base_df)
            bhr = base_sm[f'{stat}_hr']
            if hr is not None and bhr is not None:
                delta = f"  (Δ{hr-bhr:+.1%})"
        print(f"    {stat.upper():<8}: {_hr(hr):>6}  ({_n(n)}){delta}")

    print(f"\n  Tier × Stat matrix (P≥55% from gated pool):")
    header = f"    {'':10}" + "".join(f"  {s.upper():>8}" for s in STATS)
    print(header)
    for tier in TIERS:
        row_str = f"    {tier:<10}"
        for stat in STATS:
            sub = df[(df.tier==tier) & (df.stat==stat)]
            hr  = sub.hit.mean() if len(sub) > 5 else None
            cell = f"{hr:.3f}({len(sub):3d})" if hr is not None else f"  ---({len(sub):3d})"
            row_str += f"  {cell:>12}"
        print(row_str)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()

    print(f"\n{'='*68}")
    print(f"  POLICY-GATING ABLATION — Playoff Tier Thresholds")
    print(f"  Source: {CSV_PATH.name}  ({CSV_PATH.stat().st_size // 1024:,} KB)")
    print(f"{'='*68}")

    df_full = pd.read_csv(CSV_PATH)
    print(f"\n  Full backtest: {len(df_full):,} rows loaded")
    print(f"  Seasons: {sorted(df_full.season.unique())}")

    results = {}
    for key, cfg in CONFIGS.items():
        gated = apply_gates(df_full, cfg['gates'])
        results[key] = gated
        print(f"  {cfg['label'][:60]}: {len(gated):,} picks")

    base_df = results['0_baseline']

    for key, cfg in CONFIGS.items():
        print_config_block(cfg['label'], results[key],
                           base_df=None if key == '0_baseline' else base_df)

    # ── Compact delta table ────────────────────────────────────────────────────
    print(f"\n\n{'='*68}")
    print(f"  DELTA TABLE vs Baseline (all thresholds)")
    print(f"{'='*68}")

    base_sm = summary_metrics(base_df)
    hdrs = ["Config", "Total N", "P55 HR", "P55 Δ", "P65 HR", "P65 Δ",
            "P70 HR", "P70 Δ", "STAR HR", "CORE HR", "ROT HR"]
    rows = []
    for key, cfg in CONFIGS.items():
        sm = summary_metrics(results[key])
        rows.append([
            cfg['label'].split('—')[0].strip(),
            f"{sm['t55_n']:,}",
            _hr(sm['t55_hr']),
            _sign((sm['t55_hr'] or 0) - (base_sm['t55_hr'] or 0)) if key != '0_baseline' else '—',
            _hr(sm['t65_hr']),
            _sign((sm['t65_hr'] or 0) - (base_sm['t65_hr'] or 0)) if key != '0_baseline' else '—',
            _hr(sm['t70_hr']),
            _sign((sm['t70_hr'] or 0) - (base_sm['t70_hr'] or 0)) if key != '0_baseline' else '—',
            _hr(sm['STAR_hr']),
            _hr(sm['CORE_hr']),
            _hr(sm['ROTATION_hr']),
        ])

    cw = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(hdrs)]
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in cw)
    print("\n" + fmt.format(*hdrs))
    print("  " + "  ".join("─" * w for w in cw))
    for row in rows:
        print(fmt.format(*row))

    # ── Volume–quality tradeoff summary ───────────────────────────────────────
    print(f"\n\n{'='*68}")
    print(f"  VOLUME–QUALITY TRADEOFF SUMMARY")
    print(f"{'='*68}")
    b55n = base_sm['t55_n']
    b55hr = base_sm['t55_hr']
    for key, cfg in CONFIGS.items():
        sm = summary_metrics(results[key])
        n    = sm['t55_n']
        hr   = sm['t55_hr']
        n_delta = n - b55n
        hr_delta = (hr or 0) - (b55hr or 0)
        # Efficiency: HR gained per 100 picks sacrificed
        efficiency = (hr_delta / (-n_delta / 100)) if n_delta < 0 else None
        eff_str = f"{efficiency:+.3f}pp per 100 picks sacrificed" if efficiency else "—"
        print(f"\n  {cfg['label'].split('—')[0].strip()}")
        print(f"    Picks: {n:,} (Δ{n_delta:+,})  |  P≥55% HR: {_hr(hr)} (Δ{hr_delta:+.1%})")
        print(f"    Efficiency: {eff_str}")

    # ── Recommendation logic ───────────────────────────────────────────────────
    print(f"\n\n{'='*68}")
    print(f"  RECOMMENDATION")
    print(f"{'='*68}")

    # Score: HR×log(N) — maximize quality × log-volume
    best_key, best_score = None, -999
    for key, cfg in CONFIGS.items():
        sm = summary_metrics(results[key])
        hr, n = sm['t55_hr'], sm['t55_n']
        if hr and n > 100:
            score = hr * np.log(n)
            if score > best_score:
                best_score, best_key = score, key
    print(f"\n  Best quality×log-volume score: {CONFIGS[best_key]['label']}")

    # Also find best raw HR at P≥55%
    best_hr_key = max(CONFIGS.keys(),
                      key=lambda k: summary_metrics(results[k])['t55_hr'] or 0)
    print(f"  Best raw P≥55% HR:             {CONFIGS[best_hr_key]['label']}")

    if args.save:
        all_rows = []
        for key, cfg in CONFIGS.items():
            sm = summary_metrics(results[key])
            sm['config'] = cfg['label']
            sm['total_picks'] = len(results[key])
            all_rows.append(sm)
        out = Path(__file__).parent.parent / 'data' / 'processed' / 'ablation_gating.csv'
        pd.DataFrame(all_rows).to_csv(out, index=False)
        print(f"\n  Saved: {out}")

    print(f"\n{'='*68}\n")
    return results


if __name__ == '__main__':
    main()
