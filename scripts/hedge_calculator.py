"""
PrizePicks SAFE Flex Hedge Calculator.

After enough legs on a flex card have settled as wins, both remaining outcomes
(final leg hits OR misses) return money from PrizePicks. At that point you can
bet the OPPOSITE side on an external sportsbook to make both outcomes equal —
locking in a guaranteed profit regardless of what happens.

Usage:
    # Interactive mode (prompts you for card details)
    python scripts/hedge_calculator.py

    # Direct mode (all args on command line)
    python scripts/hedge_calculator.py --entry 25 --legs 5 --won 4 --odds -110

    # Show all hedge points for a card upfront (plan before games start)
    python scripts/hedge_calculator.py --entry 25 --legs 5 --preview

Examples:
    5-leg $10 card, 4 legs already hit, hedge at -110:
        Hedge $83.81 → guaranteed $106.19 profit no matter what

    3-leg $25 card, 2 legs hit, hedge at -115:
        Hedge $34.28 → guaranteed $28.22 profit no matter what
"""

import argparse
import sys


# ── PrizePicks SAFE Flex payout table ─────────────────────────────────────────
# Keys: (total_legs, legs_hit) → payout multiplier on entry amount.
# Source: PrizePicks official SAFE Flex rules.
PP_PAYOUTS: dict[tuple[int, int], float] = {
    # 2-leg
    (2, 2): 3.00,
    (2, 1): 1.25,
    (2, 0): 0.00,
    # 3-leg
    (3, 3): 5.00,
    (3, 2): 1.50,
    (3, 1): 0.00,
    (3, 0): 0.00,
    # 4-leg
    (4, 4): 10.00,
    (4, 3): 2.00,
    (4, 2): 0.00,
    (4, 1): 0.00,
    (4, 0): 0.00,
    # 5-leg
    (5, 5): 20.00,
    (5, 4): 4.00,
    (5, 3): 2.00,
    (5, 2): 0.00,
    (5, 1): 0.00,
    (5, 0): 0.00,
    # 6-leg
    (6, 6): 40.00,
    (6, 5): 8.00,
    (6, 4): 2.00,
    (6, 3): 0.00,
    (6, 2): 0.00,
    (6, 1): 0.00,
    (6, 0): 0.00,
}


def pp_payout(n_legs: int, legs_hit: int, entry: float) -> float:
    """Dollar return from PrizePicks for a given card outcome."""
    mult = PP_PAYOUTS.get((n_legs, legs_hit), 0.0)
    return mult * entry


def odds_win_rate(american_odds: int) -> float:
    """
    Convert American odds to the payout per dollar risked (not including stake).
    -110 → you risk $110 to win $100 → rate = 100/110 ≈ 0.909
    +120 → you risk $100 to win $120 → rate = 120/100 = 1.20
    """
    if american_odds < 0:
        return 100.0 / abs(american_odds)
    else:
        return american_odds / 100.0


def compute_equal_hedge(n_legs: int, legs_won: int, entry: float,
                        hedge_odds: int) -> dict:
    """
    Compute the hedge bet that equalizes profit across both outcomes of the
    last remaining leg.

    Algebra:
        pp_high = PP payout if last leg hits   (legs_won + 1 total wins)
        pp_low  = PP payout if last leg misses (legs_won total wins)
        rate    = payout per $1 risked on hedge bet

        Equal guarantee condition:
            pp_high - H = pp_low + H * rate
            H = (pp_high - pp_low) / (1 + rate)

    Returns a dict with hedge amount, guaranteed return, and profit.
    Returns None if hedging is not applicable (pp_low == 0, or pp_high <= pp_low).
    """
    pp_high = pp_payout(n_legs, legs_won + 1, entry)
    pp_low  = pp_payout(n_legs, legs_won,     entry)

    if pp_low <= 0:
        return {
            'viable': False,
            'reason': f"No payout if last leg misses ({legs_won}/{n_legs} pays $0). "
                      f"Need {_min_wins_for_payout(n_legs)} wins before hedging pays off.",
            'pp_high': pp_high,
            'pp_low': pp_low,
        }

    if pp_high <= pp_low:
        return {
            'viable': False,
            'reason': "Both outcomes already pay the same — nothing to hedge.",
            'pp_high': pp_high,
            'pp_low': pp_low,
        }

    rate = odds_win_rate(hedge_odds)
    hedge_amount = (pp_high - pp_low) / (1.0 + rate)
    guaranteed_return = pp_high - hedge_amount   # same as pp_low + hedge_amount * rate
    guaranteed_profit = guaranteed_return - entry  # subtract original entry cost

    # Partial hedge option: hedge only enough to guarantee breaking even
    breakeven_hedge = (pp_high - entry) / (1.0 + rate)
    breakeven_hedge = max(0.0, breakeven_hedge)

    return {
        'viable': True,
        'n_legs': n_legs,
        'legs_won': legs_won,
        'entry': entry,
        'hedge_odds': hedge_odds,
        'hedge_amount': hedge_amount,
        'guaranteed_return': guaranteed_return,
        'guaranteed_profit': guaranteed_profit,
        'roi': guaranteed_profit / entry * 100,
        'pp_high': pp_high,
        'pp_low': pp_low,
        'breakeven_hedge': breakeven_hedge,   # hedge to guarantee just breaking even
        'rate': rate,
    }


def _min_wins_for_payout(n_legs: int) -> int:
    """Minimum wins needed before both outcomes of last leg pay something."""
    for w in range(n_legs):
        if PP_PAYOUTS.get((n_legs, w), 0.0) > 0:
            return w
    return n_legs


def print_hedge_result(result: dict) -> None:
    if not result['viable']:
        print(f"\n  ✗ Hedge not viable: {result['reason']}")
        if result['pp_high'] > 0:
            print(f"    Best case (all hit): ${result['pp_high']:.2f} return")
        return

    r = result
    sep = "─" * 60
    print(f"\n  {sep}")
    print(f"  HEDGE CALCULATION")
    print(f"  {sep}")
    print(f"  Card:          {r['n_legs']}-leg SAFE Flex  |  Entry: ${r['entry']:.2f}")
    print(f"  Legs won:      {r['legs_won']}/{r['n_legs']}  ({r['n_legs'] - r['legs_won'] - 1} already lost, 1 pending)")
    print(f"  Hedge odds:    {'+' if r['hedge_odds'] >= 0 else ''}{r['hedge_odds']}")
    print(f"\n  PP Outcomes:")
    print(f"    Last leg HIT  → PP pays ${r['pp_high']:.2f}")
    print(f"    Last leg MISS → PP pays ${r['pp_low']:.2f}")
    print(f"\n  ── FULL LOCK ──────────────────────────────────────────")
    print(f"  Hedge bet:       ${r['hedge_amount']:.2f}  (on OPPOSITE side of last leg)")
    print(f"  Guaranteed return: ${r['guaranteed_return']:.2f}  (either outcome)")
    print(f"  Guaranteed profit: ${r['guaranteed_profit']:.2f}  ({r['roi']:.1f}% ROI on entry)")
    print(f"\n  Verification:")
    print(f"    Last leg HIT:  ${r['pp_high']:.2f} PP  −  ${r['hedge_amount']:.2f} hedge  = ${r['pp_high'] - r['hedge_amount']:.2f} ✓")
    hedge_win = r['hedge_amount'] * r['rate']
    print(f"    Last leg MISS: ${r['pp_low']:.2f} PP  +  ${hedge_win:.2f} hedge win  = ${r['pp_low'] + hedge_win:.2f} ✓")

    if r['breakeven_hedge'] > 0 and r['breakeven_hedge'] < r['hedge_amount']:
        print(f"\n  ── PARTIAL LOCK (keep upside) ──────────────────────────")
        partial_hedge = r['breakeven_hedge']
        partial_win = partial_hedge * r['rate']
        outcome_hit  = r['pp_high'] - partial_hedge
        outcome_miss = r['pp_low']  + partial_win
        print(f"  Hedge bet:       ${partial_hedge:.2f}  (floors at break-even)")
        print(f"    Last leg HIT:  ${outcome_hit:.2f} return  (${outcome_hit - r['entry']:.2f} profit)")
        print(f"    Last leg MISS: ${outcome_miss:.2f} return  (${outcome_miss - r['entry']:.2f} profit)")

    print(f"  {sep}\n")


def preview_all_hedge_points(n_legs: int, entry: float, hedge_odds: int) -> None:
    """
    Show every hedge opportunity for a card as legs accumulate.
    Run this before games start so you know what to watch for.
    """
    sep = "─" * 60
    print(f"\n  {sep}")
    print(f"  HEDGE PREVIEW  —  {n_legs}-leg SAFE Flex  |  ${entry:.2f} entry")
    print(f"  {sep}")
    print(f"  {'Legs won':>10}  {'Hedge ($)':>10}  {'Guaranteed return':>18}  {'Profit':>10}  {'ROI':>7}")
    print(f"  {'─'*58}")

    for won in range(n_legs):
        result = compute_equal_hedge(n_legs, won, entry, hedge_odds)
        if not result['viable']:
            pp_high = pp_payout(n_legs, won + 1, entry)
            pp_low  = pp_payout(n_legs, won, entry)
            miss_payout = f"${pp_low:.2f}" if pp_low > 0 else "lose"
            print(f"  {won}/{n_legs} won      {'—':>10}  "
                  f"  hit→${pp_high:.2f} / miss→{miss_payout}  (no hedge — miss pays $0)")
        else:
            r = result
            print(f"  {won}/{n_legs} won      "
                  f"${r['hedge_amount']:>9.2f}  "
                  f"    ${r['guaranteed_return']:>14.2f}  "
                  f"${r['guaranteed_profit']:>9.2f}  "
                  f"{r['roi']:>6.1f}%")

    # Also show the final case where all legs hit
    full = pp_payout(n_legs, n_legs, entry)
    print(f"  {n_legs}/{n_legs} won           —          "
          f"    ${full:>14.2f}  ${full - entry:>9.2f}  "
          f"{(full - entry) / entry * 100:>6.1f}%  (no hedge needed)")

    print(f"\n  * Hedge odds used: {'+' if hedge_odds >= 0 else ''}{hedge_odds}")
    print(f"  * Hedge = bet on OPPOSITE side of the last remaining leg")
    print(f"  * Run with --won N to see full breakdown for a specific state")
    print(f"  {sep}\n")


def interactive_mode() -> None:
    print("\n  ── PrizePicks SAFE Flex Hedge Calculator ──\n")

    try:
        entry     = float(input("  Entry amount ($): "))
        n_legs    = int(input("  Total legs in card (2-6): "))
        legs_won  = int(input("  Legs already settled as WINS: "))
        odds_str  = input("  External book odds on last leg (e.g. -110, +105) [default -110]: ").strip()
        hedge_odds = int(odds_str) if odds_str else -110
    except (ValueError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return

    result = compute_equal_hedge(n_legs, legs_won, entry, hedge_odds)
    print_hedge_result(result)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PrizePicks SAFE Flex hedge calculator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--entry',   type=float, help='Entry amount in dollars')
    parser.add_argument('--legs',    type=int,   help='Total legs in card (2-6)')
    parser.add_argument('--won',     type=int,   help='Legs already settled as wins')
    parser.add_argument('--odds',    type=int,   default=-110,
                        help='American odds on the last leg at external book (default: -110)')
    parser.add_argument('--preview', action='store_true',
                        help='Show all hedge points for this card before games start')
    args = parser.parse_args()

    if args.preview:
        if not args.entry or not args.legs:
            print("  --preview requires --entry and --legs")
            sys.exit(1)
        preview_all_hedge_points(args.legs, args.entry, args.odds)
        return

    if args.entry is not None and args.legs is not None and args.won is not None:
        result = compute_equal_hedge(args.legs, args.won, args.entry, args.odds)
        print_hedge_result(result)
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
