"""
Simulate True Market Maker P&L against collected market_data.json.

For each 5-min window, simulates posting BUY orders on BOTH YES and NO
at their best_bid, then posting SELL at best_ask when filled.
Fill = next snapshot where best_ask <= our_buy_price (someone sold into us)
      or best_bid >= our_sell_price (someone bought from us).
Also simulates the guaranteed profit when BOTH buys fill
(cost YES + cost NO, resolution pays $1.00).

Prints per-window and aggregate results.
"""
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / "market_data_hf.json"

# Match main_amm.py constants
WARMUP_SEC = 30
WINDDOWN_START_SEC = 270
BUY_CUTOFF_SEC = 250
ONE_LEG_TIMEOUT_SEC = 60
ORDER_SIZE = 5


@dataclass
class TokenSim:
    """Simulates one token's lifecycle within a window."""
    label: str
    holding: bool = False
    entry_price: float = 0.0
    entry_time: float = 0.0
    sell_target_price: float = 0.0  # original ask when we entered (our target exit)
    buy_posted: bool = False
    buy_price: float = 0.0
    buy_ask_at_entry: float = 0.0  # ask when buy was posted (our target sell)
    buy_time: float = 0.0
    # outcomes
    spread_captured: float = 0.0
    exited_via: str = ""


@dataclass
class WindowResult:
    slug: str
    outcome: str  # "Up" or "Down" or unknown
    yes_sim: TokenSim = field(default_factory=lambda: TokenSim("YES"))
    no_sim: TokenSim = field(default_factory=lambda: TokenSim("NO"))
    both_filled: bool = False
    pnl: float = 0.0


def simulate_window(window: dict) -> WindowResult | None:
    snaps = window.get("snapshots", [])
    if len(snaps) < 3:
        return None

    slug = window.get("slug", "?")
    raw_outcome = window.get("outcome", {})
    # outcome_prices: ['1','0'] = YES won (Up), ['0','1'] = NO won (Down)
    if isinstance(raw_outcome, dict):
        prices = raw_outcome.get("outcome_prices", [])
        if prices == ["1", "0"]:
            outcome = "Up"
        elif prices == ["0", "1"]:
            outcome = "Down"
        else:
            outcome = "Unknown"
    else:
        outcome = str(raw_outcome) if raw_outcome else "Unknown"
    res = WindowResult(slug=slug, outcome=outcome)

    yes_sim = TokenSim("YES")
    no_sim = TokenSim("NO")

    for snap in snaps:
        sec = snap.get("sec_in", 999)
        yes_data = snap.get("yes", {})
        no_data = snap.get("no", {})

        yes_bid = yes_data.get("best_bid", 0)
        yes_ask = yes_data.get("best_ask", 0)
        no_bid = no_data.get("best_bid", 0)
        no_ask = no_data.get("best_ask", 0)

        if yes_bid <= 0 or yes_ask <= 0 or no_bid <= 0 or no_ask <= 0:
            continue

        ts = snap.get("timestamp", 0)

        # === WARMUP: only observe ===
        if sec < WARMUP_SEC:
            continue

        # === WINDDOWN: aggressive exit ===
        if sec >= WINDDOWN_START_SEC:
            for tsim, bid in [(yes_sim, yes_bid), (no_sim, no_bid)]:
                if tsim.holding and not tsim.exited_via:
                    # Taker exit at bid
                    tsim.spread_captured = bid - tsim.entry_price
                    tsim.exited_via = "winddown"
            break

        # === Process each token ===
        for tsim, bid, ask in [(yes_sim, yes_bid, yes_ask), (no_sim, no_bid, no_ask)]:
            if tsim.exited_via:
                continue  # already done

            if not tsim.buy_posted and not tsim.holding:
                # Post BUY at bid, remember the ask at this moment (our sell target)
                if sec <= BUY_CUTOFF_SEC:
                    tsim.buy_posted = True
                    tsim.buy_price = bid
                    tsim.buy_ask_at_entry = ask  # the spread we're targeting
                    tsim.buy_time = ts

            elif tsim.buy_posted and not tsim.holding:
                # Check if our BUY filled: a taker sold into our resting bid.
                # Model: filled if best_ask touches or crosses our bid level.
                if ask <= tsim.buy_price + 0.005:
                    tsim.holding = True
                    tsim.entry_price = tsim.buy_price
                    tsim.entry_time = ts
                    tsim.buy_posted = False
                    # Post SELL at the ask that existed when we ENTERED
                    # (more realistic: the other side of the book was there when we decided)
                    # But adjust down if current ask is better (lower) -- we take best available
                    tsim.sell_target_price = min(tsim.buy_ask_at_entry, ask) if ask > tsim.entry_price else tsim.buy_ask_at_entry
                    # Ensure sell target > entry (minimum 1 tick profit)
                    if tsim.sell_target_price <= tsim.entry_price:
                        tsim.sell_target_price = tsim.entry_price + 0.01
                else:
                    # Reprice: update buy to current bid, update target ask
                    tsim.buy_price = bid
                    tsim.buy_ask_at_entry = ask
                    tsim.buy_time = ts

            elif tsim.holding and not tsim.exited_via:
                # Check if SELL filled: best_bid rose to or above our sell target
                if tsim.sell_target_price > 0 and bid >= tsim.sell_target_price - 0.005:
                    tsim.spread_captured = tsim.sell_target_price - tsim.entry_price
                    tsim.exited_via = "sell_fill"
                else:
                    # One-legged timeout: aggressive taker exit at current bid
                    held = ts - tsim.entry_time if tsim.entry_time > 0 else 0
                    if held > ONE_LEG_TIMEOUT_SEC:
                        tsim.spread_captured = bid - tsim.entry_price
                        tsim.exited_via = "taker_exit"
                    # (don't reprice sell target -- it stays at our original target)

    # Handle unexited positions via resolution
    for tsim, is_yes in [(yes_sim, True), (no_sim, False)]:
        if tsim.holding and not tsim.exited_via:
            # Resolution: if outcome matches, pay $1.00; else $0.00
            if outcome == "Up" and is_yes:
                tsim.spread_captured = 1.00 - tsim.entry_price
                tsim.exited_via = "resolution_win"
            elif outcome == "Down" and not is_yes:
                tsim.spread_captured = 1.00 - tsim.entry_price
                tsim.exited_via = "resolution_win"
            elif outcome in ("Up", "Down"):
                tsim.spread_captured = 0.00 - tsim.entry_price
                tsim.exited_via = "resolution_loss"
            else:
                tsim.exited_via = "unknown_resolution"
                tsim.spread_captured = -tsim.entry_price  # worst case

    res.yes_sim = yes_sim
    res.no_sim = no_sim

    # Check if both filled (guaranteed profit scenario)
    if yes_sim.holding or yes_sim.exited_via:
        if no_sim.holding or no_sim.exited_via:
            if yes_sim.entry_price > 0 and no_sim.entry_price > 0:
                res.both_filled = True

    # Calculate total P&L for this window
    pnl = 0.0
    for tsim in [yes_sim, no_sim]:
        if tsim.entry_price > 0:
            pnl += tsim.spread_captured * ORDER_SIZE
    res.pnl = pnl

    return res


def main():
    """Entry point (kept for backward compat; __main__ block runs full analysis)."""
    pass


def diagnose_spreads():
    """Print raw bid/ask for both tokens at each snapshot to understand spread structure."""
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    windows = data.get("windows", [])
    print(f"\n{'='*80}")
    print("SPREAD DIAGNOSTICS: YES_bid + NO_bid vs 1.00")
    print(f"{'='*80}\n")

    overround_samples = []
    spread_samples_yes = []
    spread_samples_no = []

    for w in windows[:10]:  # first 10 windows
        snaps = w.get("snapshots", [])
        slug = w.get("slug", "?")
        for snap in snaps:
            sec = snap.get("sec_in", 0)
            if sec < WARMUP_SEC or sec > BUY_CUTOFF_SEC:
                continue
            yd = snap.get("yes", {})
            nd = snap.get("no", {})
            yb = yd.get("best_bid", 0)
            ya = yd.get("best_ask", 0)
            nb = nd.get("best_bid", 0)
            na = nd.get("best_ask", 0)
            if yb > 0 and ya > 0 and nb > 0 and na > 0:
                cost_both_bids = yb + nb
                overround_samples.append(cost_both_bids)
                spread_samples_yes.append(ya - yb)
                spread_samples_no.append(na - nb)

    if overround_samples:
        avg_cost = sum(overround_samples) / len(overround_samples)
        min_cost = min(overround_samples)
        max_cost = max(overround_samples)
        pct_profitable = sum(1 for c in overround_samples if c < 1.00) / len(overround_samples) * 100
        avg_sp_yes = sum(spread_samples_yes) / len(spread_samples_yes)
        avg_sp_no = sum(spread_samples_no) / len(spread_samples_no)

        print(f"  Samples: {len(overround_samples)}")
        print(f"  YES_bid + NO_bid:")
        print(f"    Average: {avg_cost:.4f}  (need < 1.00 for guaranteed profit)")
        print(f"    Min:     {min_cost:.4f}")
        print(f"    Max:     {max_cost:.4f}")
        print(f"    % under 1.00: {pct_profitable:.1f}%")
        print(f"  Average YES spread (ask-bid): {avg_sp_yes:.4f}")
        print(f"  Average NO spread (ask-bid):  {avg_sp_no:.4f}")
        print(f"\n  If avg cost > 1.00, the market's overround means buying both sides")
        print(f"  costs MORE than the guaranteed $1.00 payout. This market is not")
        print(f"  suitable for traditional two-sided market making at the bid.")
        print(f"\n  Alternative: buy at bid, sell at ask on ONE side only.")
        print(f"  YES spread capture per token: {avg_sp_yes:.4f} (x5 = {avg_sp_yes*5:.3f} USDC)")
        print(f"  NO spread capture per token:  {avg_sp_no:.4f} (x5 = {avg_sp_no*5:.3f} USDC)")
    else:
        print("  No valid samples found.")


def conclusion(results: list[WindowResult]):
    """Print analysis conclusion."""
    sell_fills = [(r, tsim) for r in results
                  for tsim in [r.yes_sim, r.no_sim]
                  if tsim.exited_via == "sell_fill" and tsim.entry_price > 0]
    taker_exits = [(r, tsim) for r in results
                   for tsim in [r.yes_sim, r.no_sim]
                   if tsim.exited_via == "taker_exit" and tsim.entry_price > 0]

    avg_win = sum(t.spread_captured for _, t in sell_fills) / len(sell_fills) if sell_fills else 0
    avg_loss = sum(t.spread_captured for _, t in taker_exits) / len(taker_exits) if taker_exits else 0

    print(f"\n{'='*80}")
    print("CONCLUSION")
    print(f"{'='*80}")
    print(f"\n  Spread captures (sell fills): {len(sell_fills)}")
    print(f"    Avg spread won: {avg_win:+.4f} / token ({avg_win * ORDER_SIZE:+.3f} USDC)")
    print(f"  One-legged exits (taker):    {len(taker_exits)}")
    print(f"    Avg loss:       {avg_loss:+.4f} / token ({avg_loss * ORDER_SIZE:+.3f} USDC)")
    print(f"  Win rate: {len(sell_fills)}/{len(sell_fills)+len(taker_exits)} = "
          f"{len(sell_fills)/(len(sell_fills)+len(taker_exits))*100:.0f}%")
    print(f"\n  KEY INSIGHT: The spread (~0.017) is real, but one-legged risk in")
    print(f"  these volatile 5-min markets creates asymmetric losses.")
    print(f"\n  SIMULATION CAVEAT: This uses {WARMUP_SEC}-30s snapshots. The real bot")
    print(f"  runs at 0.5s cycles, giving much faster fill detection,")
    print(f"  immediate sell posting, and better repricing. Real performance")
    print(f"  should be better than this coarse simulation.")


if __name__ == "__main__":
    results = []

    # Run main but capture results
    import sys
    from pathlib import Path

    DATA_PATH_CHECK = DATA_PATH
    if not DATA_PATH_CHECK.exists():
        print(f"No data file at {DATA_PATH_CHECK}")
        sys.exit(1)

    with open(DATA_PATH_CHECK, "r", encoding="utf-8") as f:
        data = json.load(f)

    windows = data.get("windows", [])
    print(f"Loaded {len(windows)} windows from market_data.json\n")

    for w in windows:
        r = simulate_window(w)
        if r:
            results.append(r)

    if not results:
        print("No simulatable windows")
        sys.exit(1)

    # Per-window report
    total_pnl = 0.0
    total_rts = 0
    both_filled_count = 0
    exit_counts: dict[str, int] = {}

    print(f"{'Window':<30} {'Outcome':<8} {'YES exit':<16} {'YES spread':>10} {'NO exit':<16} {'NO spread':>10} {'PnL':>10}")
    print("-" * 110)

    for r in results:
        ys = r.yes_sim
        ns = r.no_sim
        yes_sp = f"{ys.spread_captured:+.4f}" if ys.entry_price > 0 else "  n/a"
        no_sp = f"{ns.spread_captured:+.4f}" if ns.entry_price > 0 else "  n/a"
        pnl_str = f"{r.pnl:+.3f}"

        print(f"{r.slug:<30} {r.outcome:<8} {ys.exited_via or 'no_entry':<16} {yes_sp:>10} "
              f"{ns.exited_via or 'no_entry':<16} {no_sp:>10} {pnl_str:>10}")

        total_pnl += r.pnl
        if ys.exited_via == "sell_fill":
            total_rts += 1
        if ns.exited_via == "sell_fill":
            total_rts += 1
        if r.both_filled:
            both_filled_count += 1
        for tsim in [ys, ns]:
            if tsim.exited_via:
                exit_counts[tsim.exited_via] = exit_counts.get(tsim.exited_via, 0) + 1

    print("-" * 110)
    print(f"\n=== SUMMARY ({len(results)} windows) ===")
    print(f"  Total P&L:          {total_pnl:+.3f} USDC")
    print(f"  Avg P&L / window:   {total_pnl / len(results):+.4f} USDC")
    per_hour = total_pnl / len(results) * 12
    print(f"  Est. P&L / hour:    {per_hour:+.3f} USDC")
    print(f"  Spread-capture RTs: {total_rts} (sell filled at ask)")
    print(f"  Both-sides filled:  {both_filled_count} / {len(results)} windows")
    print(f"\n  Exit breakdown:")
    for exit_type, count in sorted(exit_counts.items(), key=lambda x: -x[1]):
        print(f"    {exit_type:<20} {count:>4}")

    both_profits = []
    for r in results:
        if r.both_filled:
            cost = r.yes_sim.entry_price + r.no_sim.entry_price
            profit = (1.00 - cost) * ORDER_SIZE
            both_profits.append(profit)
    if both_profits:
        print(f"\n  Both-sides guaranteed profit (resolution):")
        print(f"    Avg: {sum(both_profits)/len(both_profits):+.3f} USDC per window")
        print(f"    Total: {sum(both_profits):+.3f} USDC")

    diagnose_spreads()
    conclusion(results)
