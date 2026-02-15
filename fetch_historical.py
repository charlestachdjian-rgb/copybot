"""
Bulk historical data fetcher for BTC 5-min Up/Down markets.

Fetches the outcome + early mid-price for hundreds of past windows using:
  - Gamma API: event slug -> token IDs + resolved outcome
  - CLOB API: prices-history -> early mid-price snapshot

With (early_mid, outcome) for hundreds of windows, we can statistically
validate Option A vs Option B without needing full orderbook snapshots.

Usage:  python fetch_historical.py
        python fetch_historical.py --hours 24    (default: 12 hours = 144 windows)
"""

import aiohttp
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historical_data.json")
DEFAULT_HOURS = 12  # 144 windows
CONCURRENCY = 5     # parallel API requests (be nice to the API)


def safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def fetch_window_data(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    boundary: int,
) -> dict | None:
    """Fetch outcome + early mid-price for a single window."""
    slug = f"btc-updown-5m-{boundary}"
    result = {
        "slug": slug,
        "boundary": boundary,
        "boundary_utc": datetime.fromtimestamp(boundary, tz=timezone.utc).isoformat(),
    }

    async with sem:
        # 1. Fetch event metadata from Gamma
        url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return None
                event = await r.json()
        except Exception:
            return None

        markets = event.get("markets") or []
        if not markets:
            return None
        m = markets[0]

        # Extract token IDs
        raw = m.get("clobTokenIds")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return None
        if not isinstance(raw, list) or len(raw) < 2:
            return None

        yes_tok = str(raw[0])
        no_tok = str(raw[1])
        result["yes_token"] = yes_tok
        result["no_token"] = no_tok
        result["question"] = m.get("question") or event.get("title") or slug

        # Extract outcome
        resolved = bool(m.get("closed") or m.get("resolved"))
        outcome_prices = m.get("outcomePrices")
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = json.loads(outcome_prices)
            except Exception:
                outcome_prices = None

        result["resolved"] = resolved
        result["outcome_raw"] = m.get("outcome")
        result["outcome_prices"] = outcome_prices

        if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
            yes_resolved = safe_float(outcome_prices[0])
            result["yes_resolved"] = yes_resolved
            result["direction"] = "UP" if yes_resolved > 0.5 else "DOWN"
        else:
            result["yes_resolved"] = None
            result["direction"] = None

        # 2. Fetch price history for YES and NO tokens
        # We want the early-window snapshot (first trade within the 5-min window)
        for tok, prefix in [(yes_tok, "yes"), (no_tok, "no")]:
            hurl = f"https://clob.polymarket.com/prices-history?market={tok}&interval=max"
            try:
                async with session.get(hurl, timeout=aiohttp.ClientTimeout(total=5)) as hr:
                    hdata = await hr.json()
                history = hdata.get("history", [])

                # Find all points within the 5-min window
                in_window = []
                for pt in history:
                    sec_in = pt["t"] - boundary
                    if 0 <= sec_in <= 300:
                        in_window.append({"sec_in": sec_in, "p": pt["p"]})

                result[f"{prefix}_points_in_window"] = len(in_window)
                if in_window:
                    result[f"{prefix}_early_mid"] = in_window[0]["p"]
                    result[f"{prefix}_early_sec"] = in_window[0]["sec_in"]
                    result[f"{prefix}_late_mid"] = in_window[-1]["p"]
                    result[f"{prefix}_late_sec"] = in_window[-1]["sec_in"]
                    result[f"{prefix}_all_mids"] = in_window
                else:
                    result[f"{prefix}_early_mid"] = None

                # Also get the latest price point before the window (pre-market price)
                pre_window = [pt for pt in history if pt["t"] < boundary]
                if pre_window:
                    last_pre = pre_window[-1]
                    result[f"{prefix}_pre_mid"] = last_pre["p"]
                else:
                    result[f"{prefix}_pre_mid"] = None

            except Exception:
                result[f"{prefix}_early_mid"] = None
                result[f"{prefix}_points_in_window"] = 0

    return result


async def main():
    hours = DEFAULT_HOURS
    if "--hours" in sys.argv:
        idx = sys.argv.index("--hours")
        if idx + 1 < len(sys.argv):
            hours = int(sys.argv[idx + 1])

    num_windows = hours * 12  # 12 windows per hour
    now = int(time.time())
    base = (now // 300) * 300

    print("=" * 70)
    print("  BULK HISTORICAL DATA FETCHER")
    print(f"  Fetching {num_windows} windows ({hours} hours of history)")
    print(f"  Concurrency: {CONCURRENCY}")
    print("=" * 70)

    sem = asyncio.Semaphore(CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        # Create tasks for all windows
        tasks = []
        for i in range(2, num_windows + 2):  # skip current + previous window
            boundary = base - (i * 300)
            tasks.append(fetch_window_data(session, sem, boundary))

        # Run with progress
        results = []
        done = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            done += 1
            if result:
                results.append(result)
            if done % 20 == 0 or done == len(tasks):
                print(f"  Progress: {done}/{len(tasks)} fetched, {len(results)} valid")

    # Sort by boundary
    results.sort(key=lambda x: x["boundary"])

    # Save
    data = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "hours": hours,
        "total_windows": len(results),
        "windows": results,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    print(f"\n  Saved {len(results)} windows to {OUTPUT_FILE}")

    # ── Quick inline analysis ──
    print(f"\n{'='*70}")
    print("  QUICK ANALYSIS: Option A vs Option B")
    print("=" * 70)

    # For each window, determine:
    # - Which token Option A picks (always YES)
    # - Which token Option B picks (token with early_mid >= 0.50)
    # - Whether the pick aligns with the resolved outcome

    a_aligned = 0
    a_misaligned = 0
    b_aligned = 0
    b_misaligned = 0
    b_picked_yes = 0
    b_picked_no = 0
    skipped = 0
    up_count = 0
    down_count = 0

    # Detailed tracking for mid-price bands
    band_stats = {}  # (band_label) -> {a_win, a_loss, b_win, b_loss}

    for w in results:
        direction = w.get("direction")
        yes_mid = w.get("yes_early_mid") or w.get("yes_pre_mid")
        no_mid = w.get("no_early_mid") or w.get("no_pre_mid")

        if direction is None or yes_mid is None:
            skipped += 1
            continue

        if direction == "UP":
            up_count += 1
        else:
            down_count += 1

        # Option A: always YES
        a_correct = (direction == "UP")
        if a_correct:
            a_aligned += 1
        else:
            a_misaligned += 1

        # Option B: pick token with mid >= 0.50
        if yes_mid >= 0.50:
            b_side = "YES"
            b_picked_yes += 1
            b_correct = (direction == "UP")
        else:
            b_side = "NO"
            b_picked_no += 1
            b_correct = (direction == "DOWN")

        if b_correct:
            b_aligned += 1
        else:
            b_misaligned += 1

        # Band stats
        band = None
        selected_mid = yes_mid if b_side == "YES" else (no_mid if no_mid else 1.0 - yes_mid)
        if selected_mid is not None:
            if selected_mid < 0.45:
                band = "<0.45"
            elif selected_mid < 0.50:
                band = "0.45-0.50"
            elif selected_mid < 0.55:
                band = "0.50-0.55"
            elif selected_mid < 0.60:
                band = "0.55-0.60"
            elif selected_mid < 0.65:
                band = "0.60-0.65"
            else:
                band = ">0.65"

            if band not in band_stats:
                band_stats[band] = {"a_win": 0, "a_loss": 0, "b_win": 0, "b_loss": 0, "count": 0}
            band_stats[band]["count"] += 1
            if a_correct:
                band_stats[band]["a_win"] += 1
            else:
                band_stats[band]["a_loss"] += 1
            if b_correct:
                band_stats[band]["b_win"] += 1
            else:
                band_stats[band]["b_loss"] += 1

    total = a_aligned + a_misaligned
    print(f"\n  Windows analyzed: {total} (skipped {skipped} with missing data)")
    print(f"  Market direction: {up_count} UP, {down_count} DOWN ({up_count/(up_count+down_count)*100:.1f}% UP)")

    print(f"\n  Option A (always YES):")
    print(f"    Aligned with outcome: {a_aligned}/{total} ({a_aligned/total*100:.1f}%)")
    print(f"    Misaligned:           {a_misaligned}/{total} ({a_misaligned/total*100:.1f}%)")

    print(f"\n  Option B (bias-following, mid >= 0.50):")
    print(f"    Picked YES: {b_picked_yes}, Picked NO: {b_picked_no}")
    print(f"    Aligned with outcome: {b_aligned}/{total} ({b_aligned/total*100:.1f}%)")
    print(f"    Misaligned:           {b_misaligned}/{total} ({b_misaligned/total*100:.1f}%)")

    improvement = (b_aligned - a_aligned) / total * 100
    print(f"\n  Option B improvement: {improvement:+.1f} percentage points")

    # Band analysis
    print(f"\n  --- Win rate by early mid-price band ---")
    print(f"  {'Band':<12} | {'Count':>6} | {'A win%':>8} | {'B win%':>8} | {'Better':>8}")
    print(f"  {'-'*12}-+-{'-'*6}-+-{'-'*8}-+-{'-'*8}-+-{'-'*8}")
    for band in ["<0.45", "0.45-0.50", "0.50-0.55", "0.55-0.60", "0.60-0.65", ">0.65"]:
        bs = band_stats.get(band)
        if not bs or bs["count"] == 0:
            continue
        a_rate = bs["a_win"] / bs["count"] * 100
        b_rate = bs["b_win"] / bs["count"] * 100
        better = "B" if b_rate > a_rate else ("A" if a_rate > b_rate else "TIE")
        print(f"  {band:<12} | {bs['count']:>6} | {a_rate:>7.1f}% | {b_rate:>7.1f}% | {better:>8}")

    # Simulated P&L (simplified)
    print(f"\n  --- Simplified P&L simulation ---")
    print(f"  (Assumes: enter at mid, exit at resolution, {5} tokens)")
    a_pnl = 0.0
    b_pnl = 0.0
    for w in results:
        direction = w.get("direction")
        yes_mid = w.get("yes_early_mid") or w.get("yes_pre_mid")
        no_mid = w.get("no_early_mid") or w.get("no_pre_mid")
        if direction is None or yes_mid is None:
            continue

        # Apply band filter: only trade if mid is in [0.40, 0.60]
        if yes_mid < 0.40 or yes_mid > 0.60:
            continue

        # Option A: buy YES at yes_mid
        if direction == "UP":
            a_pnl += (1.0 - yes_mid) * 5
        else:
            a_pnl += (0.0 - yes_mid) * 5

        # Option B: buy favored token
        if yes_mid >= 0.50:
            # Buy YES
            if direction == "UP":
                b_pnl += (1.0 - yes_mid) * 5
            else:
                b_pnl += (0.0 - yes_mid) * 5
        else:
            # Buy NO
            actual_no_mid = no_mid if no_mid else (1.0 - yes_mid)
            if direction == "DOWN":
                b_pnl += (1.0 - actual_no_mid) * 5
            else:
                b_pnl += (0.0 - actual_no_mid) * 5

    print(f"  Option A total P&L (band-filtered): {a_pnl:+.2f} USDC")
    print(f"  Option B total P&L (band-filtered): {b_pnl:+.2f} USDC")
    print(f"  Difference: {b_pnl - a_pnl:+.2f} USDC")

    print(f"\n{'='*70}")
    print("  DONE")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
