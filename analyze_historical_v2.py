"""
Historical analysis v2 -- addresses the timing concern.

Instead of relying on early-mid (t=17s), this analysis:
1. Uses the outcome (UP/DOWN) as ground truth
2. Tests Option B's theoretical edge: in what % of windows does the
   early-mid correctly predict the outcome direction?
3. Computes the WORST-CASE for Option B: even if the t=30s mid has
   flipped vs the t=17s mid, how bad could it be?
4. Shows the distribution of early mids to assess flip risk.

Key question: "If the mid at t=17s is 0.53, how often does the market
actually resolve UP?" If the answer is >50%, then the early-mid IS
predictive even if the t=30s mid might differ slightly.
"""

import json
import os
import statistics

data = json.load(open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "historical_data.json"),
    encoding="utf-8",
))
windows = data.get("windows", [])
print(f"Loaded {len(windows)} windows\n")

# ── Analysis 1: Early-mid as outcome predictor ──
print("=" * 70)
print("  ANALYSIS 1: Does early-mid predict outcome?")
print("  (i.e., when YES mid > 0.50 at t~20s, does it resolve UP?)")
print("=" * 70)

bins = {}  # mid_bin -> {"up": count, "down": count}
for w in windows:
    direction = w.get("direction")
    yes_mid = w.get("yes_early_mid") or w.get("yes_pre_mid")
    if not direction or not yes_mid:
        continue

    # Bin by early mid (2% increments)
    b = round(yes_mid * 50) / 50  # round to nearest 0.02
    b_label = f"{b:.2f}"
    if b_label not in bins:
        bins[b_label] = {"up": 0, "down": 0, "mid": b}
    if direction == "UP":
        bins[b_label]["up"] += 1
    else:
        bins[b_label]["down"] += 1

print(f"\n  {'Early Mid':>10} | {'UP':>4} | {'DOWN':>4} | {'Total':>5} | {'UP%':>6} | {'Predicts':>10}")
print(f"  {'-'*10}-+-{'-'*4}-+-{'-'*4}-+-{'-'*5}-+-{'-'*6}-+-{'-'*10}")
for b_label in sorted(bins.keys()):
    b = bins[b_label]
    total = b["up"] + b["down"]
    up_pct = b["up"] / total * 100
    mid = b["mid"]
    if mid >= 0.50:
        correct = b["up"]  # B picks YES, correct if UP
    else:
        correct = b["down"]  # B picks NO, correct if DOWN
    pred = f"{correct}/{total} ({correct/total*100:.0f}%)"
    marker = " <-- our band" if 0.40 <= mid <= 0.60 else ""
    print(f"  {b_label:>10} | {b['up']:>4} | {b['down']:>4} | {total:>5} | {up_pct:>5.1f}% | {pred:>10}{marker}")

# ── Analysis 2: Flip risk assessment ──
print(f"\n{'='*70}")
print("  ANALYSIS 2: How often could t=30s mid differ from t=17s mid?")
print("  (using distance from 0.50 as proxy for flip risk)")
print("=" * 70)

close_to_50 = 0  # within 0.03 of 0.50 (high flip risk)
moderate = 0      # 0.03-0.08 from 0.50
far_from_50 = 0   # >0.08 from 0.50
distances = []

for w in windows:
    yes_mid = w.get("yes_early_mid") or w.get("yes_pre_mid")
    if not yes_mid:
        continue
    dist = abs(yes_mid - 0.50)
    distances.append(dist)
    if dist < 0.03:
        close_to_50 += 1
    elif dist < 0.08:
        moderate += 1
    else:
        far_from_50 += 1

total = close_to_50 + moderate + far_from_50
print(f"\n  |mid - 0.50| < 0.03 (HIGH flip risk):  {close_to_50:>4} ({close_to_50/total*100:.1f}%)")
print(f"  |mid - 0.50| 0.03-0.08 (LOW flip risk): {moderate:>4} ({moderate/total*100:.1f}%)")
print(f"  |mid - 0.50| > 0.08 (NO flip risk):     {far_from_50:>4} ({far_from_50/total*100:.1f}%)")
print(f"  Mean distance from 0.50: {statistics.mean(distances):.4f}")
print(f"  Median distance from 0.50: {statistics.median(distances):.4f}")

# ── Analysis 3: Option A vs B worst-case sensitivity ──
print(f"\n{'='*70}")
print("  ANALYSIS 3: Option A vs B -- even with conservative assumptions")
print("  Assume 50% of 'high flip risk' windows flip the wrong way for B")
print("=" * 70)

# Count where A and B agree vs disagree
a_wins = 0; a_losses = 0
b_wins = 0; b_losses = 0
b_same_as_a = 0; b_different = 0

for w in windows:
    direction = w.get("direction")
    yes_mid = w.get("yes_early_mid") or w.get("yes_pre_mid")
    if not direction or not yes_mid:
        continue

    # Option A: always YES
    a_correct = (direction == "UP")
    if a_correct:
        a_wins += 1
    else:
        a_losses += 1

    # Option B: pick based on early mid
    if yes_mid >= 0.50:
        b_pick = "YES"
    else:
        b_pick = "NO"

    if b_pick == "YES":
        b_same_as_a += 1
        b_correct = (direction == "UP")
    else:
        b_different += 1
        b_correct = (direction == "DOWN")

    if b_correct:
        b_wins += 1
    else:
        b_losses += 1

total = a_wins + a_losses
print(f"\n  A and B pick SAME token: {b_same_as_a}/{total} ({b_same_as_a/total*100:.1f}%)")
print(f"  A and B pick DIFFERENT:  {b_different}/{total} ({b_different/total*100:.1f}%)")
print(f"\n  Option A: {a_wins}W / {a_losses}L = {a_wins/total*100:.1f}% win rate")
print(f"  Option B: {b_wins}W / {b_losses}L = {b_wins/total*100:.1f}% win rate")

# Now the sensitivity: assume half of the "high flip risk" B-different picks go wrong
flip_penalty = min(close_to_50, b_different) // 2
b_worst_wins = b_wins - flip_penalty
b_worst_losses = b_losses + flip_penalty
print(f"\n  Worst-case B (half of high-risk flips go wrong):")
print(f"  Option B pessimistic: {b_worst_wins}W / {b_worst_losses}L = {b_worst_wins/total*100:.1f}% win rate")
print(f"  (still better than A: {a_wins/total*100:.1f}% ? {b_worst_wins/total*100:.1f}%)")

# ── Analysis 4: The REAL question -- P&L impact of wrong-direction vs same-direction ──
print(f"\n{'='*70}")
print("  ANALYSIS 4: P&L impact -- what matters is the LOSS avoidance")
print("=" * 70)

# When A loses (direction=DOWN, we hold YES):
# - YES resolves to 0. If we bought at mid~0.50, loss = -0.50 * 5 = -2.50 USDC
# When B picks NO in a DOWN window:
# - NO resolves to 1. If we bought at mid~0.50, profit = +0.50 * 5 = +2.50 USDC
# The swing per window where B differs from A: ~5.00 USDC (from -2.50 to +2.50)

a_pnl = 0.0
b_pnl = 0.0
diff_windows = []

for w in windows:
    direction = w.get("direction")
    yes_mid = w.get("yes_early_mid") or w.get("yes_pre_mid")
    no_mid = w.get("no_early_mid") or w.get("no_pre_mid")
    if not direction or not yes_mid:
        continue

    # Only count windows in our trading band [0.40-0.60]
    if yes_mid < 0.40 or yes_mid > 0.60:
        continue

    # A: always YES
    if direction == "UP":
        a_pnl += (1.0 - yes_mid) * 5
    else:
        a_pnl += (0.0 - yes_mid) * 5  # loss

    # B: pick favored
    if yes_mid >= 0.50:
        # Same as A
        if direction == "UP":
            b_pnl += (1.0 - yes_mid) * 5
        else:
            b_pnl += (0.0 - yes_mid) * 5
    else:
        actual_no_mid = no_mid if no_mid else (1.0 - yes_mid)
        if direction == "DOWN":
            b_pnl += (1.0 - actual_no_mid) * 5
        else:
            b_pnl += (0.0 - actual_no_mid) * 5
        diff_windows.append({
            "slug": w["slug"],
            "direction": direction,
            "yes_mid": yes_mid,
            "a_outcome": "LOSS" if direction == "DOWN" else "WIN",
            "b_outcome": "WIN" if direction == "DOWN" else "LOSS",
        })

print(f"\n  Band-filtered windows [0.40-0.60]:")
print(f"  Option A total P&L: {a_pnl:+.2f} USDC")
print(f"  Option B total P&L: {b_pnl:+.2f} USDC")
print(f"  Improvement: {b_pnl - a_pnl:+.2f} USDC")
print(f"\n  Windows where B picked differently from A: {len(diff_windows)}")
for dw in diff_windows[:10]:
    print(f"    {dw['slug'][-15:]}: dir={dw['direction']:>4} yes_mid={dw['yes_mid']:.4f}  A={dw['a_outcome']}  B={dw['b_outcome']}")
if len(diff_windows) > 10:
    print(f"    ... and {len(diff_windows)-10} more")

# Even if HALF of those B-different decisions flip (t=30 mid crossed 0.50):
half_flip_penalty = len(diff_windows) // 2
# Each flip costs ~5 USDC swing (from +2.50 to -2.50)
avg_swing = 5.0 * 0.50  # approximate for mid near 0.50
pessimistic_b_pnl = b_pnl - (half_flip_penalty * avg_swing)
print(f"\n  Pessimistic B (50% of different-picks flip at t=30):")
print(f"  Adjusted B P&L: {pessimistic_b_pnl:+.2f} USDC")
print(f"  Still better than A ({a_pnl:+.2f})? {'YES' if pessimistic_b_pnl > a_pnl else 'NO'}")

print(f"\n{'='*70}")
print("  CONCLUSION")
print("=" * 70)
