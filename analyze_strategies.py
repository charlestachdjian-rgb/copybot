"""
Strategy analyzer for BTC 5-min Up/Down market data.

Reads market_data.json (produced by collect_data.py) and simulates multiple
trading strategies against each window, comparing risk metrics.

Strategies:
  A  -- Always trade YES token  (current bot behavior)
  B  -- Trade token with mid >= 0.50  (bias-following)
  B+ -- Option B, but band filter [0.50, 0.60] on selected token

All strategies use our existing safety rules:
  - Warmup: 0-30s (no trading)
  - Winddown: 270-300s (forced sell if holding)
  - Buy cutoff: no new buys after 120s
  - Forced flatten: sell at bid after 60s held
  - Stop-loss: sell at bid if mid drops >15% from entry
  - One round-trip per window
  - Toxic flow: skip if spread >15% or mid drift >25%

Usage:  python analyze_strategies.py
        python analyze_strategies.py --file path/to/market_data.json
"""

import json
import os
import sys
import statistics

# ── Constants (mirrored from main_amm.py) ──
WINDOW_SEC = 300
WARMUP_SEC = 30
WINDDOWN_SEC = 30
FORCED_FLATTEN_SEC = 60
STOP_LOSS_MID_DROP_PCT = 15.0
TOXIC_SPREAD_PCT = 15.0
TOXIC_MID_DRIFT_PCT = 25.0
BUY_CUTOFF_SEC = 120
ORDER_SIZE = 5  # tokens per trade


def load_data(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Simulation Engine ──

def simulate_strategy(snapshots: list[dict], strategy: str) -> dict:
    """
    Simulate one strategy on a window's snapshots.
    
    strategy:
      "A"  -- always use YES token data
      "B"  -- use token with mid >= 0.50
      "B+" -- use token with mid >= 0.50, but only buy if selected mid in [0.50, 0.60]
    
    Returns detailed result dict.
    """
    state = {
        "mode": "BUY",
        "entry_price": None,
        "entry_mid": None,
        "entry_sec": None,
        "round_trip_done": False,
        "last_mid": None,
        "token_side": None,  # "YES" or "NO"
    }
    decisions = []
    exit_price = None
    exit_reason = None
    adverse_moves = 0  # count of snapshots where mid moved against us after entry
    favorable_moves = 0
    max_adverse_pct = 0.0
    max_favorable_pct = 0.0
    hold_secs = 0
    volatility_samples = []  # |mid change| between consecutive snapshots

    prev_mid_for_vol = None

    for snap in snapshots:
        sec_in = snap["sec_in"]
        yes = snap.get("yes", {})
        no = snap.get("no", {})

        # Select which token to trade based on strategy
        if strategy == "A":
            tok = yes
            side = "YES"
        elif strategy in ("B", "B+"):
            y_mid = yes.get("mid", 0.0)
            n_mid = no.get("mid", 0.0)
            if y_mid >= 0.50:
                tok = yes
                side = "YES"
            else:
                tok = no
                side = "NO"
        else:
            tok = yes
            side = "YES"

        bid = tok.get("best_bid", 0.0)
        ask = tok.get("best_ask", 0.0)
        mid = tok.get("mid", 0.0)
        spread_pct = tok.get("spread_pct", 0.0)
        imbalance = tok.get("book_imbalance", 0.0)
        best_bid_size = tok.get("best_bid_size", 0.0)
        best_ask_size = tok.get("best_ask_size", 0.0)

        # Volatility tracking
        if prev_mid_for_vol is not None and prev_mid_for_vol > 0 and mid > 0:
            vol = abs(mid - prev_mid_for_vol) / prev_mid_for_vol * 100
            volatility_samples.append(vol)
        prev_mid_for_vol = mid

        decision = {
            "sec_in": sec_in,
            "mid": mid,
            "bid": bid,
            "ask": ask,
            "spread_pct": spread_pct,
            "side": side,
            "action": None,
            "reason": None,
            "imbalance": imbalance,
        }

        # -- Warmup --
        if sec_in < WARMUP_SEC:
            decision["action"] = "SKIP"
            decision["reason"] = "warmup"
            decisions.append(decision)
            continue

        # -- Winddown --
        safe_zone_end = WINDOW_SEC - WINDDOWN_SEC
        if sec_in >= safe_zone_end:
            if state["mode"] == "SELL" and bid > 0:
                decision["action"] = "WINDDOWN_SELL"
                decision["reason"] = f"winddown sell @ bid={bid:.4f}"
                exit_price = bid
                exit_reason = "winddown"
                hold_secs = sec_in - (state["entry_sec"] or sec_in)
                state["mode"] = "BUY"
                state["round_trip_done"] = True
            else:
                decision["action"] = "SKIP"
                decision["reason"] = "winddown, flat"
            decisions.append(decision)
            continue

        # -- Toxic flow --
        if state["last_mid"] is not None and state["last_mid"] > 0 and mid > 0:
            drift = abs(mid - state["last_mid"]) / state["last_mid"] * 100
            if spread_pct > TOXIC_SPREAD_PCT or drift > TOXIC_MID_DRIFT_PCT:
                decision["action"] = "SKIP"
                decision["reason"] = f"toxic (sprd={spread_pct:.1f}%, drift={drift:.1f}%)"
                state["last_mid"] = mid
                decisions.append(decision)
                continue
        state["last_mid"] = mid

        # ── SELL MODE ──
        if state["mode"] == "SELL":
            held_sec = sec_in - (state["entry_sec"] or sec_in)

            # Track adverse selection
            if state["entry_mid"] and state["entry_mid"] > 0 and mid > 0:
                move_pct = ((mid - state["entry_mid"]) / state["entry_mid"]) * 100
                if move_pct < 0:
                    adverse_moves += 1
                    if abs(move_pct) > max_adverse_pct:
                        max_adverse_pct = abs(move_pct)
                else:
                    favorable_moves += 1
                    if move_pct > max_favorable_pct:
                        max_favorable_pct = move_pct

            # Stop-loss
            if state["entry_price"] and state["entry_price"] > 0 and mid > 0:
                drop_pct = ((state["entry_price"] - mid) / state["entry_price"]) * 100
                if drop_pct > STOP_LOSS_MID_DROP_PCT:
                    decision["action"] = "STOP_LOSS"
                    decision["reason"] = f"mid dropped {drop_pct:.1f}% from entry"
                    exit_price = bid
                    exit_reason = f"stop-loss ({drop_pct:.1f}%)"
                    hold_secs = held_sec
                    state["mode"] = "BUY"
                    state["round_trip_done"] = True
                    decisions.append(decision)
                    continue

            # Let-resolve (held too long)
            if held_sec > 240:
                decision["action"] = "LET_RESOLVE"
                decision["reason"] = f"held {held_sec:.0f}s > 240s"
                exit_price = None
                exit_reason = "let_resolve"
                hold_secs = held_sec
                state["mode"] = "DONE"
                decisions.append(decision)
                continue

            # Forced flatten
            if held_sec > FORCED_FLATTEN_SEC:
                decision["action"] = "FORCED_FLATTEN"
                decision["reason"] = f"held {held_sec:.0f}s > {FORCED_FLATTEN_SEC}s, sell @ bid={bid:.4f}"
                exit_price = bid
                exit_reason = f"time-flatten ({held_sec:.0f}s)"
                hold_secs = held_sec
                state["mode"] = "BUY"
                state["round_trip_done"] = True
                decisions.append(decision)
                continue

            # Normal sell pending
            decision["action"] = "SELL_PENDING"
            decision["reason"] = f"holding, SELL @ ask={ask:.4f} (held {held_sec:.0f}s)"
            decisions.append(decision)

        elif state["mode"] == "BUY":
            # Round-trip check
            if state["round_trip_done"]:
                decision["action"] = "SKIP"
                decision["reason"] = "round-trip done"
                decisions.append(decision)
                continue

            # Buy cutoff
            if sec_in > BUY_CUTOFF_SEC:
                decision["action"] = "SKIP"
                decision["reason"] = f"buy cutoff ({sec_in}s > {BUY_CUTOFF_SEC}s)"
                decisions.append(decision)
                continue

            # Band filter
            if strategy == "B+":
                # For B+, the selected token's mid should be in [0.50, 0.60]
                if mid < 0.50 or mid > 0.60:
                    decision["action"] = "SKIP"
                    decision["reason"] = f"B+ band: mid={mid:.4f} outside [0.50, 0.60]"
                    decisions.append(decision)
                    continue
            else:
                # Option A and B use the original 0.40-0.60 band on the selected token's mid
                if mid < 0.40 or mid > 0.60:
                    decision["action"] = "SKIP"
                    decision["reason"] = f"band: mid={mid:.4f} outside [0.40, 0.60]"
                    decisions.append(decision)
                    continue

            # Check if bid is valid
            if bid <= 0:
                decision["action"] = "SKIP"
                decision["reason"] = "no bid"
                decisions.append(decision)
                continue

            # BUY
            decision["action"] = "BUY"
            decision["reason"] = f"BUY {side} @ bid={bid:.4f} (mid={mid:.4f})"
            state["entry_price"] = bid
            state["entry_mid"] = mid
            state["entry_sec"] = sec_in
            state["mode"] = "SELL"
            state["token_side"] = side
            decisions.append(decision)

        elif state["mode"] == "DONE":
            decision["action"] = "SKIP"
            decision["reason"] = "let-resolve, done"
            decisions.append(decision)

    # ── Compute P&L ──
    pnl = None
    if state["entry_price"] is not None and exit_price is not None:
        pnl = (exit_price - state["entry_price"]) * ORDER_SIZE

    # If we're still holding at end (let_resolve or no winddown sell), estimate from outcome
    still_holding = state["mode"] in ("SELL", "DONE")

    total_sel_moves = adverse_moves + favorable_moves
    adverse_rate = adverse_moves / total_sel_moves if total_sel_moves > 0 else 0.0

    avg_vol = statistics.mean(volatility_samples) if volatility_samples else 0.0
    max_vol = max(volatility_samples) if volatility_samples else 0.0
    high_vol_count = sum(1 for v in volatility_samples if v > 3.0)

    return {
        "strategy": strategy,
        "traded": state["entry_price"] is not None,
        "token_side": state.get("token_side"),
        "entry_price": state["entry_price"],
        "entry_mid": state.get("entry_mid"),
        "entry_sec": state.get("entry_sec"),
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "hold_secs": hold_secs,
        "pnl": pnl,
        "still_holding": still_holding,
        "round_trip_done": state["round_trip_done"],
        "adverse_moves": adverse_moves,
        "favorable_moves": favorable_moves,
        "adverse_rate": round(adverse_rate, 3),
        "max_adverse_pct": round(max_adverse_pct, 2),
        "max_favorable_pct": round(max_favorable_pct, 2),
        "avg_volatility_pct": round(avg_vol, 2),
        "max_volatility_pct": round(max_vol, 2),
        "high_vol_snapshots": high_vol_count,
        "decisions": decisions,
    }


def estimate_resolution_pnl(result: dict, outcome: dict | None) -> float | None:
    """
    If the bot was still holding at resolution (let_resolve or no exit),
    estimate P&L from the outcome.
    """
    if not result["still_holding"] or not result["entry_price"]:
        return result["pnl"]
    if not outcome or not outcome.get("resolved"):
        return None

    # outcome_prices is [yes_price, no_price], e.g. ["1", "0"] or ["0", "1"]
    op = outcome.get("outcome_prices")
    if not isinstance(op, list) or len(op) < 2:
        return None

    try:
        if result["token_side"] == "YES":
            resolve_price = float(op[0])
        else:
            resolve_price = float(op[1])
    except (TypeError, ValueError, IndexError):
        return None

    return (resolve_price - result["entry_price"]) * ORDER_SIZE


# ── Reporting ──

def analyze_all(data: dict):
    windows = data.get("windows", [])
    if not windows:
        print("No windows found in data file.")
        return

    strategies = ["A", "B", "B+"]
    all_results = {s: [] for s in strategies}

    print(f"\nAnalyzing {len(windows)} windows across strategies {strategies}...\n")

    for i, w in enumerate(windows):
        snaps = w.get("snapshots", [])
        outcome = w.get("outcome")
        slug = w.get("slug", "?")

        if not snaps:
            print(f"  Window {i+1} ({slug}): no snapshots, skipping")
            continue

        for strat in strategies:
            result = simulate_strategy(snaps, strat)
            # Try to resolve P&L for held positions
            if result["still_holding"]:
                result["resolution_pnl"] = estimate_resolution_pnl(result, outcome)
            else:
                result["resolution_pnl"] = result["pnl"]
            result["window_slug"] = slug
            result["window_idx"] = i
            result["outcome"] = outcome
            all_results[strat].append(result)

    # ── Print per-window comparison ──
    print("=" * 100)
    print(f"  {'WINDOW':<28} | {'STRAT':>5} | {'SIDE':>4} | {'ENTRY':>6} | {'EXIT':>6} | {'REASON':<20} | {'P&L':>8} | {'HOLD':>5} | {'ADV%':>5}")
    print("-" * 100)

    for i, w in enumerate(windows):
        slug = w.get("slug", "?")
        outcome = w.get("outcome", {})
        outcome_str = ""
        if outcome and outcome.get("resolved"):
            op = outcome.get("outcome_prices", [])
            if isinstance(op, list) and len(op) >= 2:
                try:
                    yes_val = float(op[0])
                    outcome_str = "UP" if yes_val > 0.5 else "DOWN"
                except (TypeError, ValueError):
                    pass

        for strat in strategies:
            results_for_window = [r for r in all_results[strat] if r["window_idx"] == i]
            if not results_for_window:
                continue
            r = results_for_window[0]

            entry_str = f"{r['entry_price']:.4f}" if r['entry_price'] else "  --  "
            exit_str = f"{r['exit_price']:.4f}" if r['exit_price'] else "  --  "
            pnl_val = r["resolution_pnl"] if r["resolution_pnl"] is not None else r["pnl"]
            pnl_str = f"{pnl_val:+.4f}" if pnl_val is not None else "   --  "
            reason_str = (r["exit_reason"] or "no trade")[:20]
            side_str = r["token_side"] or "--"
            hold_str = f"{r['hold_secs']:.0f}s" if r['hold_secs'] else " --"
            adv_str = f"{r['adverse_rate']*100:.0f}%" if r['traded'] else " --"

            label = f"{slug[-10:]} ({outcome_str:>4})" if strat == strategies[0] else ""
            print(
                f"  {label:<28} | {strat:>5} | {side_str:>4} | {entry_str:>6} | {exit_str:>6} | "
                f"{reason_str:<20} | {pnl_str:>8} | {hold_str:>5} | {adv_str:>5}"
            )
        print("-" * 100)

    # ── Aggregate Metrics ──
    print("\n" + "=" * 90)
    print("  AGGREGATE METRICS")
    print("=" * 90)

    header = f"  {'Metric':<40}"
    for s in strategies:
        header += f" | {'Opt '+s:>12}"
    print(header)
    print("-" * 90)

    def metric_row(label: str, values: list):
        row = f"  {label:<40}"
        for v in values:
            if isinstance(v, float):
                row += f" | {v:>12.4f}"
            elif isinstance(v, int):
                row += f" | {v:>12d}"
            elif isinstance(v, str):
                row += f" | {v:>12}"
            else:
                row += f" | {'--':>12}"
        print(row)

    for label, key, fmt in [
        ("Windows with a trade", "traded_count", "int"),
        ("Windows skipped (no trade)", "skipped_count", "int"),
        ("Total P&L (USDC)", "total_pnl", "float"),
        ("Average P&L per window (USDC)", "avg_pnl", "float"),
        ("Best window P&L", "best_pnl", "float"),
        ("Worst window P&L", "worst_pnl", "float"),
        ("Stop-loss triggers", "stop_loss_count", "int"),
        ("Forced-flatten triggers", "flatten_count", "int"),
        ("Winddown sells", "winddown_count", "int"),
        ("Let-resolve (held to end)", "let_resolve_count", "int"),
        ("Avg hold time (s)", "avg_hold", "float"),
        ("Max hold time (s)", "max_hold", "float"),
        ("Avg adverse selection rate", "avg_adverse_rate", "float"),
        ("Max adverse move (%)", "max_adverse", "float"),
        ("Avg volatility per snap (%)", "avg_vol", "float"),
        ("High-vol snapshots (>3%/snap)", "high_vol_total", "int"),
    ]:
        vals = []
        for s in strategies:
            results = all_results[s]
            traded = [r for r in results if r["traded"]]
            pnls = [r["resolution_pnl"] for r in results if r["resolution_pnl"] is not None]

            if key == "traded_count":
                vals.append(len(traded))
            elif key == "skipped_count":
                vals.append(len(results) - len(traded))
            elif key == "total_pnl":
                vals.append(sum(pnls) if pnls else 0.0)
            elif key == "avg_pnl":
                vals.append(statistics.mean(pnls) if pnls else 0.0)
            elif key == "best_pnl":
                vals.append(max(pnls) if pnls else 0.0)
            elif key == "worst_pnl":
                vals.append(min(pnls) if pnls else 0.0)
            elif key == "stop_loss_count":
                vals.append(sum(1 for r in traded if r["exit_reason"] and "stop-loss" in r["exit_reason"]))
            elif key == "flatten_count":
                vals.append(sum(1 for r in traded if r["exit_reason"] and "time-flatten" in r["exit_reason"]))
            elif key == "winddown_count":
                vals.append(sum(1 for r in traded if r["exit_reason"] == "winddown"))
            elif key == "let_resolve_count":
                vals.append(sum(1 for r in traded if r["exit_reason"] == "let_resolve"))
            elif key == "avg_hold":
                holds = [r["hold_secs"] for r in traded if r["hold_secs"]]
                vals.append(statistics.mean(holds) if holds else 0.0)
            elif key == "max_hold":
                holds = [r["hold_secs"] for r in traded if r["hold_secs"]]
                vals.append(max(holds) if holds else 0.0)
            elif key == "avg_adverse_rate":
                rates = [r["adverse_rate"] for r in traded]
                vals.append(statistics.mean(rates) if rates else 0.0)
            elif key == "max_adverse":
                vals.append(max((r["max_adverse_pct"] for r in traded), default=0.0))
            elif key == "avg_vol":
                vols = [r["avg_volatility_pct"] for r in results]
                vals.append(statistics.mean(vols) if vols else 0.0)
            elif key == "high_vol_total":
                vals.append(sum(r["high_vol_snapshots"] for r in results))
            else:
                vals.append(None)

        metric_row(label, vals)

    # ── Directional Exposure Analysis ──
    print("\n" + "=" * 90)
    print("  DIRECTIONAL EXPOSURE ANALYSIS")
    print("=" * 90)

    for s in strategies:
        results = all_results[s]
        traded = [r for r in results if r["traded"]]
        yes_trades = sum(1 for r in traded if r["token_side"] == "YES")
        no_trades = sum(1 for r in traded if r["token_side"] == "NO")

        # Count wins vs losses
        wins = sum(1 for r in traded if r["resolution_pnl"] is not None and r["resolution_pnl"] > 0)
        losses = sum(1 for r in traded if r["resolution_pnl"] is not None and r["resolution_pnl"] < 0)
        breakeven = sum(1 for r in traded if r["resolution_pnl"] is not None and r["resolution_pnl"] == 0)

        # Outcomes alignment
        aligned = 0
        misaligned = 0
        for r in traded:
            outcome = r.get("outcome", {})
            if not outcome or not outcome.get("resolved"):
                continue
            op = outcome.get("outcome_prices", [])
            if not isinstance(op, list) or len(op) < 2:
                continue
            try:
                yes_resolved = float(op[0])
            except (TypeError, ValueError):
                continue
            if r["token_side"] == "YES" and yes_resolved > 0.5:
                aligned += 1
            elif r["token_side"] == "NO" and yes_resolved < 0.5:
                aligned += 1
            else:
                misaligned += 1

        print(f"\n  Option {s}:")
        print(f"    Trades: {len(traded)} total ({yes_trades} YES, {no_trades} NO)")
        print(f"    Wins/Losses/BE: {wins}/{losses}/{breakeven}")
        print(f"    Aligned with outcome: {aligned}, Misaligned: {misaligned}")
        if traded:
            win_rate = wins / len(traded) * 100
            print(f"    Win rate: {win_rate:.1f}%")

    # ── Mid-Price Zone Analysis ──
    print("\n" + "=" * 90)
    print("  MID-PRICE ZONE RISK ANALYSIS (across all windows)")
    print("=" * 90)

    # Analyze which mid-price zones have highest volatility and adverse moves
    zones = {
        "0.00-0.20": {"vol": [], "count": 0},
        "0.20-0.40": {"vol": [], "count": 0},
        "0.40-0.50": {"vol": [], "count": 0},
        "0.50-0.60": {"vol": [], "count": 0},
        "0.60-0.80": {"vol": [], "count": 0},
        "0.80-1.00": {"vol": [], "count": 0},
    }

    for w in windows:
        snaps = w.get("snapshots", [])
        prev_mid = None
        for snap in snaps:
            yes = snap.get("yes", {})
            mid = yes.get("mid", 0.0)
            if mid <= 0:
                continue

            if prev_mid and prev_mid > 0:
                vol = abs(mid - prev_mid) / prev_mid * 100
                for zname, (zlo, zhi) in [
                    ("0.00-0.20", (0.0, 0.20)),
                    ("0.20-0.40", (0.20, 0.40)),
                    ("0.40-0.50", (0.40, 0.50)),
                    ("0.50-0.60", (0.50, 0.60)),
                    ("0.60-0.80", (0.60, 0.80)),
                    ("0.80-1.00", (0.80, 1.00)),
                ]:
                    if zlo <= mid < zhi:
                        zones[zname]["vol"].append(vol)
                        zones[zname]["count"] += 1
                        break
            prev_mid = mid

    print(f"\n  {'Zone':<12} | {'Snapshots':>10} | {'Avg Vol%':>10} | {'Max Vol%':>10} | {'Risk Level':<12}")
    print(f"  {'-'*12}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}-+-{'-'*12}")
    for zname in zones:
        z = zones[zname]
        count = z["count"]
        avg_v = statistics.mean(z["vol"]) if z["vol"] else 0.0
        max_v = max(z["vol"]) if z["vol"] else 0.0
        risk = "LOW" if avg_v < 3 else ("MEDIUM" if avg_v < 8 else "HIGH")
        print(f"  {zname:<12} | {count:>10} | {avg_v:>9.2f}% | {max_v:>9.2f}% | {risk:<12}")

    # ── Book Depth & Imbalance Analysis ──
    print("\n" + "=" * 90)
    print("  BOOK DEPTH & IMBALANCE ANALYSIS")
    print("=" * 90)

    imbalances_at_entry = {s: [] for s in strategies}
    depths_at_entry = {s: [] for s in strategies}
    for s in strategies:
        for r in all_results[s]:
            if not r["traded"]:
                continue
            # Find the entry decision
            for d in r["decisions"]:
                if d["action"] == "BUY":
                    imbalances_at_entry[s].append(d.get("imbalance", 0.0))
                    break

    for s in strategies:
        imbs = imbalances_at_entry[s]
        if imbs:
            avg_imb = statistics.mean(imbs)
            print(f"  Option {s}: avg book imbalance at entry = {avg_imb:+.4f} (>0 = more bids, <0 = more asks)")
        else:
            print(f"  Option {s}: no entries")

    # ── Spread Analysis ──
    print("\n" + "=" * 90)
    print("  SPREAD DYNAMICS")
    print("=" * 90)

    time_bins = {
        "0-30s (warmup)": [],
        "30-60s (early)": [],
        "60-120s (mid)": [],
        "120-270s (late)": [],
        "270-300s (winddown)": [],
    }
    for w in windows:
        for snap in w.get("snapshots", []):
            sec = snap["sec_in"]
            sprd = snap.get("yes", {}).get("spread_pct", 0.0)
            if sec < 30:
                time_bins["0-30s (warmup)"].append(sprd)
            elif sec < 60:
                time_bins["30-60s (early)"].append(sprd)
            elif sec < 120:
                time_bins["60-120s (mid)"].append(sprd)
            elif sec < 270:
                time_bins["120-270s (late)"].append(sprd)
            else:
                time_bins["270-300s (winddown)"].append(sprd)

    print(f"\n  {'Time Zone':<22} | {'Avg Spread%':>12} | {'Max Spread%':>12} | {'Samples':>8}")
    print(f"  {'-'*22}-+-{'-'*12}-+-{'-'*12}-+-{'-'*8}")
    for tname, spreads in time_bins.items():
        if spreads:
            print(f"  {tname:<22} | {statistics.mean(spreads):>11.2f}% | {max(spreads):>11.2f}% | {len(spreads):>8}")
        else:
            print(f"  {tname:<22} | {'--':>12} | {'--':>12} | {0:>8}")

    # ── Recommendation ──
    print("\n" + "=" * 90)
    print("  RECOMMENDATION")
    print("=" * 90)

    # Find best strategy by total P&L and risk
    best_pnl_strat = None
    best_pnl = float("-inf")
    safest_strat = None
    lowest_worst = float("-inf")

    for s in strategies:
        pnls = [r["resolution_pnl"] for r in all_results[s] if r["resolution_pnl"] is not None]
        total = sum(pnls) if pnls else 0.0
        worst = min(pnls) if pnls else 0.0

        if total > best_pnl:
            best_pnl = total
            best_pnl_strat = s
        if worst > lowest_worst:
            lowest_worst = worst
            safest_strat = s

    traded_counts = {s: sum(1 for r in all_results[s] if r["traded"]) for s in strategies}
    stop_loss_counts = {
        s: sum(1 for r in all_results[s] if r["traded"] and r["exit_reason"] and "stop-loss" in r["exit_reason"])
        for s in strategies
    }

    print(f"\n  Best total P&L:      Option {best_pnl_strat} ({best_pnl:+.4f} USDC)")
    print(f"  Smallest worst-case: Option {safest_strat} (worst window: {lowest_worst:+.4f} USDC)")
    print(f"  Trade frequency:     A={traded_counts['A']}, B={traded_counts['B']}, B+={traded_counts['B+']}")
    print(f"  Stop-loss triggers:  A={stop_loss_counts['A']}, B={stop_loss_counts['B']}, B+={stop_loss_counts['B+']}")

    print(f"""
  Analysis notes:
  - Option A (always YES) has full directional exposure to BTC going down.
  - Option B (bias-following) trades the token the market favors, reducing
    directional exposure. It should have fewer stop-loss triggers when the
    market resolves against YES.
  - Option B+ (bias + tight band) is the most conservative, trading only
    when the selected token's mid is in [0.50, 0.60]. It will skip more
    windows but should have the lowest worst-case loss.
  - Check the directional exposure section above to see if Option B
    successfully reduced the correlation between outcomes and losses.
""")

    print("=" * 90)
    print("  ANALYSIS COMPLETE")
    print("=" * 90)


# ── Entry Point ──

if __name__ == "__main__":
    path = OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_data.json")
    if len(sys.argv) > 2 and sys.argv[1] == "--file":
        path = sys.argv[2]

    if not os.path.exists(path):
        print(f"ERROR: {path} not found. Run collect_data.py first.")
        sys.exit(1)

    data = load_data(path)
    print(f"Loaded {len(data.get('windows', []))} windows from {path}")
    analyze_all(data)
