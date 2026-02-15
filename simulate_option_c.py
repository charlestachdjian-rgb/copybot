"""
Simulate Option C (Confirmed Quick Scalp) P&L over collected market data.

Replicates the exact logic from main_amm.py:
  - Option B token selection (pick side with mid >= 0.50)
  - Observation period: skip until t >= 65s
  - Momentum gate: selected token mid >= 0.54  (0.50 + 0.04)
  - Band filter: selected token mid in [0.40, 0.60]
  - Book imbalance confirmation: imbalance > 0 for selected token
  - One round-trip per window
  - Hold max 30s, then forced flatten at best_bid (taker)
  - Stop-loss: 25% mid drop from entry -> taker sell
  - Buy cutoff at t=90s (no new entries after)

Also simulates Option A (always YES) and Option B (bias-follow) with
current/old parameters for comparison.
"""

import json, os, statistics
from dataclasses import dataclass, field

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_data.json")
data = json.load(open(DATA_FILE, encoding="utf-8"))
windows = data["windows"]
print(f"Loaded {len(windows)} windows\n")


# ── Helpers ──

def find_snap(snaps, target_sec, tolerance=8):
    """Find closest snapshot to target time within tolerance."""
    best, best_diff = None, 999
    for s in snaps:
        d = abs(s["sec_in"] - target_sec)
        if d < best_diff and d <= tolerance:
            best, best_diff = s, d
    return best


def snaps_in_range(snaps, t_start, t_end):
    """Return snapshots with sec_in in [t_start, t_end], sorted by time."""
    return sorted(
        [s for s in snaps if t_start <= s["sec_in"] <= t_end],
        key=lambda s: s["sec_in"],
    )


@dataclass
class Trade:
    window: str
    side: str           # "YES" or "NO"
    entry_time: float   # sec_in when entered
    entry_price: float  # bid price at entry
    entry_mid: float
    exit_time: float    # sec_in when exited
    exit_price: float   # price we sold at
    exit_reason: str    # "flatten", "stop_loss", "winddown", "natural"
    qty: float = 5.0
    pnl: float = 0.0
    imbalance: float = 0.0
    direction: str = ""  # resolved outcome


@dataclass
class StrategyResult:
    name: str
    trades: list = field(default_factory=list)
    skips: list = field(default_factory=list)  # reasons for skipping windows


def get_token_data(snap, side):
    """Get bid/ask/mid/imbalance for a given side from snapshot."""
    if side == "YES":
        d = snap["yes"]
    else:
        d = snap["no"]
    return {
        "bid": d["best_bid"],
        "ask": d["best_ask"],
        "mid": d["mid"],
        "spread_pct": d.get("spread_pct", 0),
        "imbalance": d.get("book_imbalance", 0),
    }


def resolve_direction(w):
    """Get resolved direction for a window."""
    outcome = w.get("outcome", {})
    if not outcome or not outcome.get("resolved"):
        return None
    op = outcome.get("outcome_prices", [])
    if not isinstance(op, list) or len(op) < 2:
        return None
    try:
        return "UP" if float(op[0]) > 0.5 else "DOWN"
    except:
        return None


# ══════════════════════════════════════════════════════════════════
# STRATEGY SIMULATORS
# ══════════════════════════════════════════════════════════════════

def simulate_option_c(windows):
    """
    Option C: Confirmed Quick Scalp
    - Warmup: 65s
    - Entry window: 65-90s
    - Momentum gate: mid >= 0.54
    - Imbalance confirm: imb > 0
    - Band: [0.40, 0.60]
    - Hold max: 30s (forced flatten)
    - Stop-loss: 25% mid drop
    - One round-trip per window
    """
    res = StrategyResult("Option C: Confirmed Quick Scalp")
    WARMUP = 65
    CUTOFF = 90
    FLATTEN_SEC = 30
    STOP_LOSS_PCT = 25.0
    MOMENTUM = 0.04
    BAND_LOW, BAND_HIGH = 0.40, 0.60
    QTY = 5

    for w in windows:
        snaps = w.get("snapshots", [])
        direction = resolve_direction(w)
        slug = w["slug"]

        # Skip windows without enough data
        entry_candidates = snaps_in_range(snaps, WARMUP, CUTOFF)
        if not entry_candidates:
            res.skips.append((slug, "no snapshots in entry window 65-90s"))
            continue

        # Option B token selection: at first available snapshot, pick side with mid >= 0.50
        first_snap = entry_candidates[0]
        yes_mid = first_snap["yes"]["mid"]
        no_mid = first_snap["no"]["mid"]

        if yes_mid >= 0.50:
            selected_side = "YES"
        elif no_mid > 0:
            selected_side = "NO"
        else:
            selected_side = "YES"  # fallback

        # Try each snapshot in entry window until we get a valid entry
        entered = False
        for snap in entry_candidates:
            td = get_token_data(snap, selected_side)
            mid = td["mid"]
            bid = td["bid"]
            ask = td["ask"]
            imb = td["imbalance"]

            # Band filter
            if mid < BAND_LOW or mid > BAND_HIGH:
                continue

            # Momentum gate: mid must be >= 0.50 + MOMENTUM
            if mid < 0.50 + MOMENTUM:
                continue

            # Imbalance confirmation
            if imb <= 0:
                continue

            # All gates passed — ENTER
            entry_price = bid  # buy at bid (POST_ONLY maker order)
            if entry_price <= 0:
                continue

            entry_time = snap["sec_in"]
            entry_mid = mid

            # Now simulate the hold period
            hold_snaps = snaps_in_range(snaps, entry_time + 1, entry_time + FLATTEN_SEC + 10)

            exit_price = None
            exit_time = None
            exit_reason = None

            for hs in hold_snaps:
                htd = get_token_data(hs, selected_side)
                h_mid = htd["mid"]
                h_bid = htd["bid"]
                h_time = hs["sec_in"]
                hold_elapsed = h_time - entry_time

                # Stop-loss check
                if entry_mid > 0 and h_mid > 0:
                    drop_pct = ((entry_mid - h_mid) / entry_mid) * 100
                    if drop_pct > STOP_LOSS_PCT:
                        exit_price = h_bid if h_bid > 0 else h_mid - 0.005
                        exit_time = h_time
                        exit_reason = "stop_loss"
                        break

                # Forced flatten at 30s
                if hold_elapsed >= FLATTEN_SEC:
                    exit_price = h_bid if h_bid > 0 else h_mid - 0.005
                    exit_time = h_time
                    exit_reason = "flatten_30s"
                    break

            # If no explicit exit during hold snaps, find the closest snap to entry+30
            if exit_price is None:
                flat_snap = find_snap(snaps, entry_time + FLATTEN_SEC, tolerance=15)
                if flat_snap:
                    ftd = get_token_data(flat_snap, selected_side)
                    exit_price = ftd["bid"] if ftd["bid"] > 0 else ftd["mid"] - 0.005
                    exit_time = flat_snap["sec_in"]
                    exit_reason = "flatten_approx"
                else:
                    # Use last available snap
                    last_hold = [s for s in snaps if s["sec_in"] > entry_time]
                    if last_hold:
                        ltds = get_token_data(last_hold[-1], selected_side)
                        exit_price = ltds["bid"] if ltds["bid"] > 0 else ltds["mid"] - 0.005
                        exit_time = last_hold[-1]["sec_in"]
                        exit_reason = "last_available"
                    else:
                        continue

            if exit_price is None or exit_price <= 0:
                continue

            pnl = (exit_price - entry_price) * QTY
            trade = Trade(
                window=slug, side=selected_side,
                entry_time=entry_time, entry_price=entry_price, entry_mid=entry_mid,
                exit_time=exit_time, exit_price=exit_price, exit_reason=exit_reason,
                qty=QTY, pnl=pnl, imbalance=imb, direction=direction or "?",
            )
            res.trades.append(trade)
            entered = True
            break  # one round-trip per window

        if not entered:
            # Figure out why we skipped
            reasons = []
            for snap in entry_candidates:
                td = get_token_data(snap, selected_side)
                if td["mid"] < BAND_LOW or td["mid"] > BAND_HIGH:
                    reasons.append(f"mid={td['mid']:.3f} outside band")
                elif td["mid"] < 0.50 + MOMENTUM:
                    reasons.append(f"mid={td['mid']:.3f} < {0.50+MOMENTUM:.2f} (momentum)")
                elif td["imbalance"] <= 0:
                    reasons.append(f"imb={td['imbalance']:+.3f} (no confirm)")
                elif td["bid"] <= 0:
                    reasons.append("no bid")
            unique_reasons = list(dict.fromkeys(reasons))[:3]
            res.skips.append((slug, "; ".join(unique_reasons) if unique_reasons else "unknown"))

    return res


def simulate_option_b(windows):
    """
    Option B (old): Bias-follow, enter t=30, hold 60s, stop-loss 15%.
    """
    res = StrategyResult("Option B: Bias-Follow (old)")
    WARMUP = 30
    CUTOFF = 120
    FLATTEN_SEC = 60
    STOP_LOSS_PCT = 15.0
    BAND_LOW, BAND_HIGH = 0.40, 0.60
    QTY = 5

    for w in windows:
        snaps = w.get("snapshots", [])
        direction = resolve_direction(w)
        slug = w["slug"]

        entry_candidates = snaps_in_range(snaps, WARMUP, CUTOFF)
        if not entry_candidates:
            res.skips.append((slug, "no snapshots in 30-120s"))
            continue

        first_snap = entry_candidates[0]
        yes_mid = first_snap["yes"]["mid"]
        selected_side = "YES" if yes_mid >= 0.50 else "NO"

        entered = False
        for snap in entry_candidates:
            td = get_token_data(snap, selected_side)
            mid = td["mid"]
            bid = td["bid"]
            if mid < BAND_LOW or mid > BAND_HIGH:
                continue
            if bid <= 0:
                continue

            entry_price = bid
            entry_time = snap["sec_in"]
            entry_mid = mid

            hold_snaps = snaps_in_range(snaps, entry_time + 1, entry_time + FLATTEN_SEC + 10)
            exit_price, exit_time, exit_reason = None, None, None

            for hs in hold_snaps:
                htd = get_token_data(hs, selected_side)
                h_mid, h_bid = htd["mid"], htd["bid"]
                hold_elapsed = hs["sec_in"] - entry_time

                if entry_mid > 0 and h_mid > 0:
                    drop_pct = ((entry_mid - h_mid) / entry_mid) * 100
                    if drop_pct > STOP_LOSS_PCT:
                        exit_price = h_bid if h_bid > 0 else h_mid - 0.005
                        exit_time = hs["sec_in"]
                        exit_reason = "stop_loss"
                        break

                if hold_elapsed >= FLATTEN_SEC:
                    exit_price = h_bid if h_bid > 0 else h_mid - 0.005
                    exit_time = hs["sec_in"]
                    exit_reason = "flatten_60s"
                    break

            if exit_price is None:
                flat_snap = find_snap(snaps, entry_time + FLATTEN_SEC, tolerance=15)
                if flat_snap:
                    ftd = get_token_data(flat_snap, selected_side)
                    exit_price = ftd["bid"] if ftd["bid"] > 0 else ftd["mid"] - 0.005
                    exit_time = flat_snap["sec_in"]
                    exit_reason = "flatten_approx"

            if exit_price is None or exit_price <= 0:
                continue

            pnl = (exit_price - entry_price) * QTY
            res.trades.append(Trade(
                window=slug, side=selected_side,
                entry_time=entry_time, entry_price=entry_price, entry_mid=entry_mid,
                exit_time=exit_time, exit_price=exit_price, exit_reason=exit_reason,
                qty=QTY, pnl=pnl, direction=direction or "?",
            ))
            entered = True
            break

        if not entered:
            res.skips.append((slug, "no valid entry"))

    return res


def simulate_option_a(windows):
    """
    Option A (baseline): Always YES, enter t=30, hold 60s, stop-loss 15%.
    """
    res = StrategyResult("Option A: Always YES (baseline)")
    WARMUP = 30
    CUTOFF = 120
    FLATTEN_SEC = 60
    STOP_LOSS_PCT = 15.0
    BAND_LOW, BAND_HIGH = 0.40, 0.60
    QTY = 5

    for w in windows:
        snaps = w.get("snapshots", [])
        direction = resolve_direction(w)
        slug = w["slug"]
        selected_side = "YES"

        entry_candidates = snaps_in_range(snaps, WARMUP, CUTOFF)
        if not entry_candidates:
            res.skips.append((slug, "no snapshots in 30-120s"))
            continue

        entered = False
        for snap in entry_candidates:
            td = get_token_data(snap, selected_side)
            mid, bid = td["mid"], td["bid"]
            if mid < BAND_LOW or mid > BAND_HIGH:
                continue
            if bid <= 0:
                continue

            entry_price = bid
            entry_time = snap["sec_in"]
            entry_mid = mid

            hold_snaps = snaps_in_range(snaps, entry_time + 1, entry_time + FLATTEN_SEC + 10)
            exit_price, exit_time, exit_reason = None, None, None

            for hs in hold_snaps:
                htd = get_token_data(hs, selected_side)
                h_mid, h_bid = htd["mid"], htd["bid"]
                hold_elapsed = hs["sec_in"] - entry_time

                if entry_mid > 0 and h_mid > 0:
                    drop_pct = ((entry_mid - h_mid) / entry_mid) * 100
                    if drop_pct > STOP_LOSS_PCT:
                        exit_price = h_bid if h_bid > 0 else h_mid - 0.005
                        exit_time = hs["sec_in"]
                        exit_reason = "stop_loss"
                        break

                if hold_elapsed >= FLATTEN_SEC:
                    exit_price = h_bid if h_bid > 0 else h_mid - 0.005
                    exit_time = hs["sec_in"]
                    exit_reason = "flatten_60s"
                    break

            if exit_price is None:
                flat_snap = find_snap(snaps, entry_time + FLATTEN_SEC, tolerance=15)
                if flat_snap:
                    ftd = get_token_data(flat_snap, selected_side)
                    exit_price = ftd["bid"] if ftd["bid"] > 0 else ftd["mid"] - 0.005
                    exit_time = flat_snap["sec_in"]
                    exit_reason = "flatten_approx"

            if exit_price is None or exit_price <= 0:
                continue

            pnl = (exit_price - entry_price) * QTY
            res.trades.append(Trade(
                window=slug, side=selected_side,
                entry_time=entry_time, entry_price=entry_price, entry_mid=entry_mid,
                exit_time=exit_time, exit_price=exit_price, exit_reason=exit_reason,
                qty=QTY, pnl=pnl, direction=direction or "?",
            ))
            entered = True
            break

        if not entered:
            res.skips.append((slug, "no valid entry"))

    return res


# ══════════════════════════════════════════════════════════════════
# REPORTING
# ══════════════════════════════════════════════════════════════════

def report(result: StrategyResult):
    trades = result.trades
    n = len(trades)
    print(f"\n{'=' * 70}")
    print(f"  {result.name}")
    print(f"{'=' * 70}")

    if n == 0:
        print(f"  No trades executed. Skipped {len(result.skips)} windows.")
        for slug, reason in result.skips[:10]:
            ts_part = slug.split("-")[-1]
            print(f"    {ts_part}: {reason}")
        return

    pnls = [t.pnl for t in trades]
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    flat = [t for t in trades if t.pnl == 0]
    total_pnl = sum(pnls)
    avg_pnl = statistics.mean(pnls)
    worst = min(pnls)
    best = max(pnls)
    win_rate = len(wins) / n * 100

    stops = [t for t in trades if t.exit_reason == "stop_loss"]
    flattens = [t for t in trades if "flatten" in t.exit_reason]

    # Cumulative P&L
    cum_pnl = []
    running = 0.0
    for t in trades:
        running += t.pnl
        cum_pnl.append(running)
    max_drawdown = 0.0
    peak = 0.0
    for cp in cum_pnl:
        if cp > peak:
            peak = cp
        dd = peak - cp
        if dd > max_drawdown:
            max_drawdown = dd

    print(f"\n  SUMMARY:")
    print(f"    Trades executed:     {n:>3}  (skipped {len(result.skips)} windows)")
    print(f"    Wins / Losses / Flat: {len(wins)} / {len(losses)} / {len(flat)}")
    print(f"    Win rate:            {win_rate:>6.1f}%")
    print(f"    Stop-losses:         {len(stops):>3}")
    print(f"    Forced flattens:     {len(flattens):>3}")
    print(f"")
    print(f"    Total P&L:           {total_pnl:>+8.3f} USDC")
    print(f"    Avg P&L per trade:   {avg_pnl:>+8.3f} USDC")
    print(f"    Best trade:          {best:>+8.3f} USDC")
    print(f"    Worst trade:         {worst:>+8.3f} USDC")
    print(f"    Max drawdown:        {max_drawdown:>8.3f} USDC")

    if len(wins) > 0:
        avg_win = statistics.mean([t.pnl for t in wins])
        print(f"    Avg WIN:             {avg_win:>+8.3f} USDC")
    if len(losses) > 0:
        avg_loss = statistics.mean([t.pnl for t in losses])
        print(f"    Avg LOSS:            {avg_loss:>+8.3f} USDC")

    # Per-trade detail
    print(f"\n  TRADE-BY-TRADE:")
    print(f"    {'Window':<14} {'Side':>4} {'Dir':>4} {'Entry':>6} {'@':>6} {'Exit':>6} {'@':>6} {'Hold':>5} {'Exit':>12} {'Imb':>7} {'P&L':>8}")
    print(f"    {'-'*14} {'-'*4} {'-'*4} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*5} {'-'*12} {'-'*7} {'-'*8}")

    for t in trades:
        ts_part = t.window.split("-")[-1]
        hold_s = t.exit_time - t.entry_time
        imb_str = f"{t.imbalance:+.3f}" if t.imbalance else "  n/a"
        marker = "  W" if t.pnl > 0 else (" L" if t.pnl < 0 else "  -")
        print(f"    {ts_part:<14} {t.side:>4} {t.direction:>4} "
              f"t={t.entry_time:>3.0f}s {t.entry_price:>6.3f} "
              f"t={t.exit_time:>3.0f}s {t.exit_price:>6.3f} "
              f"{hold_s:>4.0f}s {t.exit_reason:>12} {imb_str} {t.pnl:>+7.3f}{marker}")

    # Cumulative P&L progression
    print(f"\n  CUMULATIVE P&L:")
    running = 0.0
    for i, t in enumerate(trades):
        running += t.pnl
        bar_len = int(abs(running) * 10)
        bar = ("#" * bar_len) if running >= 0 else ("." * bar_len)
        sign = "+" if running >= 0 else ""
        ts_part = t.window.split("-")[-1]
        print(f"    {i+1:>2}. {ts_part}: {sign}{running:.3f} USDC  {bar}")

    # Skipped windows
    if result.skips:
        print(f"\n  SKIPPED WINDOWS ({len(result.skips)}):")
        for slug, reason in result.skips:
            ts_part = slug.split("-")[-1]
            print(f"    {ts_part}: {reason}")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    res_a = simulate_option_a(windows)
    res_b = simulate_option_b(windows)
    res_c = simulate_option_c(windows)

    report(res_a)
    report(res_b)
    report(res_c)

    # Final comparison table
    print(f"\n\n{'=' * 70}")
    print(f"  HEAD-TO-HEAD COMPARISON")
    print(f"{'=' * 70}")
    print(f"  {'Metric':<25} {'Option A':>12} {'Option B':>12} {'Option C':>12}")
    print(f"  {'-'*25} {'-'*12} {'-'*12} {'-'*12}")

    for label, r in [("A", res_a), ("B", res_b), ("C", res_c)]:
        pass  # just for clarity

    results = [res_a, res_b, res_c]
    metrics = []
    for r in results:
        n = len(r.trades)
        pnls = [t.pnl for t in r.trades]
        total = sum(pnls) if pnls else 0
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / n * 100 if n > 0 else 0
        worst = min(pnls) if pnls else 0
        best = max(pnls) if pnls else 0
        stops = sum(1 for t in r.trades if t.exit_reason == "stop_loss")
        avg = statistics.mean(pnls) if pnls else 0
        metrics.append({
            "trades": n, "skips": len(r.skips), "total": total,
            "wr": wr, "worst": worst, "best": best, "stops": stops, "avg": avg,
        })

    rows = [
        ("Trades", "trades", "d"),
        ("Skipped windows", "skips", "d"),
        ("Win rate", "wr", ".1f"),
        ("Total P&L (USDC)", "total", "+.3f"),
        ("Avg P&L / trade", "avg", "+.3f"),
        ("Best trade", "best", "+.3f"),
        ("Worst trade", "worst", "+.3f"),
        ("Stop-losses", "stops", "d"),
    ]
    for label, key, fmt in rows:
        vals = [m[key] for m in metrics]
        if fmt == "d":
            strs = [f"{v:>12d}" for v in vals]
        elif ".1f" in fmt:
            strs = [f"{v:>11.1f}%" for v in vals]
        else:
            strs = [f"{v:>+12.3f}" for v in vals]
        print(f"  {label:<25} {''.join(strs)}")

    # Winner
    totals = [m["total"] for m in metrics]
    names = ["A", "B", "C"]
    best_idx = totals.index(max(totals))
    worst_idx = totals.index(min(totals))
    print(f"\n  WINNER: Option {names[best_idx]} ({totals[best_idx]:+.3f} USDC)")
    print(f"  LOSER:  Option {names[worst_idx]} ({totals[worst_idx]:+.3f} USDC)")
    print(f"  Option C improvement vs B: {totals[2] - totals[1]:+.3f} USDC")
    print(f"  Option C improvement vs A: {totals[2] - totals[0]:+.3f} USDC")
    print(f"{'=' * 70}")
