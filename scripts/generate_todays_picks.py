"""
Daily SAFE Flex picks generator.

Loads trained models and features.parquet, filters to the given date +
player IDs, and outputs two cards with zero shared legs, max 2 legs per
game, and one prop per player per card.

Usage:
    python scripts/generate_todays_picks.py --date 2025-03-15 --players 2544 1629029 203999

Stat thresholds (minimum P(over) to qualify):
    REB / AST      : P >= 60%  (well-calibrated in backtest)
    PRA / REB_AST  : P >= 65%
    PTS / FG3M     : P >= 70%  (noisier — only high-confidence picks)
"""

import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import pickle
from scipy import stats as scipy_stats

from config.settings import PROCESSED_DIR


# ── Constants ────────────────────────────────────────────────────────────────

COMPOSITE_STATS = {
    'pra':     ['pts', 'reb', 'ast'],
    'reb_ast': ['reb', 'ast'],
}

# Minimum P(over) to even consider a leg — REB/AST are calibrated, rest need
# higher confidence to compensate for weaker signal
STAT_MIN_PROB = {
    'reb':     0.60,
    'ast':     0.60,
    'pra':     0.65,
    'reb_ast': 0.65,
    'pts':     0.70,
    'fg3m':    0.70,
}

# Priority order: calibrated markets first, noisy markets last
STATS = ['reb', 'ast', 'pra', 'reb_ast', 'pts', 'fg3m']

MIN_MINUTES = 20


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_model(stat: str) -> dict:
    path = PROCESSED_DIR / "models" / f"{stat}_model.pkl"
    if not path.exists():
        raise FileNotFoundError(f"Model not found: {path}. Run player_props training first.")
    with open(path, 'rb') as f:
        return pickle.load(f)


def prob_over(pred: float, line: float, std: float) -> float:
    if std <= 0 or np.isnan(std):
        std = max(abs(pred) * 0.2, 1.0)
    z = (line - pred) / std
    return float(np.clip(1 - scipy_stats.norm.cdf(z), 0.01, 0.99))


def predict_stat(df: pd.DataFrame, stat: str) -> pd.Series:
    """Return a Series of raw model predictions aligned to df.index."""
    components = COMPOSITE_STATS.get(stat)
    if components:
        models = {c: load_model(c) for c in components}
        valid = pd.Series(True, index=df.index)
        for m in models.values():
            cols = [c for c in m['feature_cols'] if c in df.columns]
            valid &= df[cols].notna().all(axis=1)
        preds = pd.Series(0.0, index=df.index)
        for m in models.values():
            cols = [c for c in m['feature_cols'] if c in df.columns]
            sub = df.loc[valid, cols]
            preds.loc[valid] += m['model'].predict(sub)
        preds[~valid] = np.nan
    else:
        m = load_model(stat)
        cols = [c for c in m['feature_cols'] if c in df.columns]
        valid = df[cols].notna().all(axis=1)
        preds = pd.Series(np.nan, index=df.index)
        preds.loc[valid] = m['model'].predict(df.loc[valid, cols])
    return preds


# ── Candidate generation ──────────────────────────────────────────────────────

def build_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """Score every (player, stat) pair and return rows above threshold."""
    rows = []
    for stat in STATS:
        l10_col = f'{stat}_L10'
        std_col = f'{stat}_std_L10'
        if l10_col not in df.columns:
            continue

        min_p = STAT_MIN_PROB[stat]
        preds = predict_stat(df, stat)

        for idx, row in df.iterrows():
            pred = preds.get(idx, np.nan)
            if pd.isna(pred):
                continue
            line = row.get(l10_col)
            if pd.isna(line):
                continue
            std_val = row.get(std_col, np.nan)
            p = prob_over(pred, line, float('nan') if pd.isna(std_val) else std_val)
            if p < min_p:
                continue
            rows.append({
                'player_id': row['player_id'],
                'player':    row.get('full_name', str(row['player_id'])),
                'game':      row.get('matchup', 'UNK'),
                'stat':      stat.upper(),
                'line':      round(float(line), 1),
                'pred':      round(float(pred), 1),
                'prob':      p,
            })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values('prob', ascending=False).reset_index(drop=True)


# ── Card builder ──────────────────────────────────────────────────────────────

def build_card(candidates: pd.DataFrame, n_legs: int,
               exclude_keys: set) -> list[dict]:
    """
    Greedy highest-confidence selection with constraints:
      - Skip (player_id, stat) keys already in exclude_keys
      - One prop per player per card
      - Max 2 legs per game
    """
    used_players: set = set()
    game_counts: dict = {}
    card: list[dict] = []

    for _, row in candidates.iterrows():
        key = (row['player_id'], row['stat'])
        if key in exclude_keys:
            continue
        if row['player_id'] in used_players:
            continue
        if game_counts.get(row['game'], 0) >= 2:
            continue

        card.append(row.to_dict())
        used_players.add(row['player_id'])
        game_counts[row['game']] = game_counts.get(row['game'], 0) + 1

        if len(card) == n_legs:
            break

    return card


# ── Output ────────────────────────────────────────────────────────────────────

def print_card(card: list[dict], label: str) -> None:
    w = 74
    print(f"\n  {label}")
    print(f"  {'Player':<26} {'Stat':<9} {'Line':>5} {'Pred':>6} {'P(over)':>8}  Matchup")
    print(f"  {'-' * w}")
    for leg in card:
        bar = '#' * int(leg['prob'] * 20)
        print(f"  {leg['player']:<26} {leg['stat']:<9} {leg['line']:>5.1f} "
              f"{leg['pred']:>6.1f} {leg['prob']:>8.1%}  {leg['game']}")
    avg_prob = sum(leg['prob'] for leg in card) / len(card)
    print(f"  {'-' * w}")
    print(f"  {'Avg confidence':>42}  {avg_prob:>8.1%}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily SAFE Flex picks")
    parser.add_argument('--date',    required=True,          help='Game date YYYY-MM-DD')
    parser.add_argument('--players', nargs='+', type=int,    help='Player IDs (space-separated)')
    args = parser.parse_args()

    feature_path = PROCESSED_DIR / "features.parquet"
    if not feature_path.exists():
        print("Error: features.parquet not found. Run: python -m src.features.pipeline")
        sys.exit(1)

    df = pd.read_parquet(feature_path)
    df['game_date'] = pd.to_datetime(df['game_date'])
    target_date = pd.to_datetime(args.date)

    # Filter to requested date + players
    mask = df['game_date'] == target_date
    if args.players:
        mask &= df['player_id'].isin(args.players)
    df_today = df[mask].copy()

    if df_today.empty:
        print(f"\n  No rows found for date={args.date}"
              + (f", players={args.players}" if args.players else "") + ".")
        print("  Check that the feature pipeline has been run for this date.")
        sys.exit(1)

    # Minutes filter
    min_col = 'min_L10' if 'min_L10' in df_today.columns else 'min'
    n_before = len(df_today)
    df_today = df_today[df_today[min_col] >= MIN_MINUTES].copy()
    n_dropped = n_before - len(df_today)

    print(f"\n  Date: {args.date}")
    print(f"  Players loaded: {n_before}  |  After min_L10 >= {MIN_MINUTES}: {len(df_today)} ({n_dropped} dropped)")

    if df_today.empty:
        print("  All players were filtered out by the minutes threshold.")
        sys.exit(0)

    candidates = build_candidates(df_today)

    if candidates.empty:
        print("  No legs met minimum probability thresholds.")
        sys.exit(0)

    print(f"  Qualified legs: {len(candidates)}")

    # Build cards — card 2 may not share any leg with card 1
    card1 = build_card(candidates, n_legs=3, exclude_keys=set())
    used = {(leg['player_id'], leg['stat']) for leg in card1}
    card2 = build_card(candidates, n_legs=5, exclude_keys=used)

    print("\n" + "=" * 76)
    print("  SAFE FLEX DAILY PICKS")
    print("=" * 76)

    if card1:
        print_card(card1, "CARD 1  —  3-Leg SAFE Flex")
    else:
        print("\n  CARD 1: Not enough qualifying legs (need 3).")

    if card2:
        print_card(card2, "CARD 2  —  5-Leg SAFE Flex")
    else:
        print("\n  CARD 2: Not enough non-overlapping legs (need 5).")

    print("\n" + "=" * 76)
    print("  THRESHOLDS  REB/AST >= 60%  |  PRA/REB_AST >= 65%  |  PTS/FG3M >= 70%")
    print("  RULES       0 shared legs between cards  |  max 2 legs/game  |  1 prop/player")
    print("=" * 76 + "\n")


if __name__ == "__main__":
    main()
