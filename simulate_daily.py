"""
Simulate True Market Maker P&L on collected daily market data (market_data_daily.json).

Single continuous session: no windows, no resolution (market may not have resolved yet).
Same logic as simulate_mm: post BUY at bid, fill when ask crosses; post SELL at ask,
fill when bid crosses; one-legged timeout -> taker exit. Positions still open at
end of data are closed at last bid (taker).

Usage:  python simulate_daily.py
        (reads market_data_daily.json, prints P&L and diagnostics)
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / "market_data_daily.json"

ONE_LEG_TIMEOUT_SEC = 60
ORDER_SIZE = 5
FILL_TOLERANCE = 0.005


@dataclass
class TokenSim:
    label: str
    holding: bool = False
    entry_price: float = 0.0
    entry_time: float = 0.0
    sell_target_price: float = 0.0
    buy_posted: bool = False
    buy_price: float = 0.0
    buy_ask_at_entry: float = 0.0
    buy_time: float = 0.0
    spread_captured: float = 0.0
    exited_via: str = ""


def simulate_session(data: dict) -> tuple[TokenSim, TokenSim, list[dict]]:
    """
    Run MM simulation over one session of snapshots.
    Returns (yes_sim, no_sim, trades_log).
    """
    snaps = data.get("snapshots", [])
    if len(snaps) < 3:
        return TokenSim("YES"), TokenSim("NO"), []

    yes_sim = TokenSim("YES")
    no_sim = TokenSim("NO")
    trades_log = []

    for snap in snaps:
        sec_elapsed = snap.get("sec_elapsed", 0)
        ts = snap.get("timestamp", 0)
        yes_data = snap.get("yes", {})
        no_data = snap.get("no", {})

        yes_bid = yes_data.get("best_bid", 0)
        yes_ask = yes_data.get("best_ask", 0)
        no_bid = no_data.get("best_bid", 0)
        no_ask = no_data.get("best_ask", 0)

        if yes_bid <= 0 or yes_ask <= 0 or no_bid <= 0 or no_ask <= 0:
            continue

        for tsim, bid, ask in [(yes_sim, yes_bid, yes_ask), (no_sim, no_bid, no_ask)]:
            if tsim.exited_via:
                continue

            if not tsim.buy_posted and not tsim.holding:
                tsim.buy_posted = True
                tsim.buy_price = bid
                tsim.buy_ask_at_entry = ask
                tsim.buy_time = ts

            elif tsim.buy_posted and not tsim.holding:
                if ask <= tsim.buy_price + FILL_TOLERANCE:
                    tsim.holding = True
                    tsim.entry_price = tsim.buy_price
                    tsim.entry_time = ts
                    tsim.buy_posted = False
                    tsim.sell_target_price = (
                        min(tsim.buy_ask_at_entry, ask) if ask > tsim.entry_price else tsim.buy_ask_at_entry
                    )
                    if tsim.sell_target_price <= tsim.entry_price:
                        tsim.sell_target_price = tsim.entry_price + 0.01
                else:
                    tsim.buy_price = bid
                    tsim.buy_ask_at_entry = ask
                    tsim.buy_time = ts

            elif tsim.holding and not tsim.exited_via:
                if tsim.sell_target_price > 0 and bid >= tsim.sell_target_price - FILL_TOLERANCE:
                    tsim.spread_captured = tsim.sell_target_price - tsim.entry_price
                    tsim.exited_via = "sell_fill"
                    trades_log.append(
                        {"token": tsim.label, "exit": "sell_fill", "spread": tsim.spread_captured, "sec": sec_elapsed}
                    )
                else:
                    held = ts - tsim.entry_time if tsim.entry_time > 0 else 0
                    if held > ONE_LEG_TIMEOUT_SEC:
                        tsim.spread_captured = bid - tsim.entry_price
                        tsim.exited_via = "taker_exit"
                        trades_log.append(
                            {"token": tsim.label, "exit": "taker_exit", "spread": tsim.spread_captured, "sec": sec_elapsed}
                        )

    # End of data: close any still-open position at last snapshot bid
    last = snaps[-1] if snaps else {}
    last_yes_bid = last.get("yes", {}).get("best_bid", 0)
    last_no_bid = last.get("no", {}).get("best_bid", 0)
    for tsim, last_bid in [(yes_sim, last_yes_bid), (no_sim, last_no_bid)]:
        if tsim.holding and not tsim.exited_via and last_bid > 0:
            tsim.spread_captured = last_bid - tsim.entry_price
            tsim.exited_via = "end_of_data"
            trades_log.append(
                {"token": tsim.label, "exit": "end_of_data", "spread": tsim.spread_captured}
            )

    return yes_sim, no_sim, trades_log


def main():
    if not DATA_PATH.exists():
        print(f"No data file at {DATA_PATH}")
        print("Run collect_data_daily.py first (1 hour collection).")
        return

    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    slug = data.get("slug", "?")
    question = (data.get("question") or "")[:60]
    snapshots = data.get("snapshots", [])
    duration_sec = data.get("duration_sec", 0)
    resolution_iso = data.get("resolution_iso", "")

    print("=" * 70)
    print("  DAILY MARKET SIMULATION — True Market Maker")
    print("=" * 70)
    print(f"  Slug: {slug}")
    print(f"  Question: {question}")
    print(f"  Resolution: {resolution_iso}")
    print(f"  Snapshots: {len(snapshots)}  |  Duration: {duration_sec}s")
    print()

    if len(snapshots) < 10:
        print("  Too few snapshots to simulate.")
        return

    yes_sim, no_sim, trades_log = simulate_session(data)

    # P&L
    pnl_yes = yes_sim.spread_captured * ORDER_SIZE
    pnl_no = no_sim.spread_captured * ORDER_SIZE
    total_pnl = pnl_yes + pnl_no

    print("  RESULTS")
    print("  -" * 35)
    print(f"  YES: exit={yes_sim.exited_via or 'none'}  spread/token={yes_sim.spread_captured:+.4f}  P&L={pnl_yes:+.3f} USDC")
    print(f"  NO:  exit={no_sim.exited_via or 'none'}  spread/token={no_sim.spread_captured:+.4f}  P&L={pnl_no:+.3f} USDC")
    print(f"  Total P&L: {total_pnl:+.3f} USDC")
    print()

    # Exit breakdown
    sell_fills = [t for t in trades_log if t.get("exit") == "sell_fill"]
    taker_exits = [t for t in trades_log if t.get("exit") == "taker_exit"]
    end_data = [t for t in trades_log if t.get("exit") == "end_of_data"]
    print(f"  Exit breakdown: sell_fill={len(sell_fills)}  taker_exit={len(taker_exits)}  end_of_data={len(end_data)}")
    if sell_fills:
        avg_win = sum(t["spread"] for t in sell_fills) / len(sell_fills)
        print(f"  Avg spread (sell_fill): {avg_win:+.4f} per token  ({avg_win * ORDER_SIZE:+.3f} USDC)")
    if taker_exits:
        avg_loss = sum(t["spread"] for t in taker_exits) / len(taker_exits)
        print(f"  Avg spread (taker_exit): {avg_loss:+.4f} per token  ({avg_loss * ORDER_SIZE:+.3f} USDC)")
    print()

    # Spread diagnostics (same as 5m)
    both_bids = []
    spread_yes = []
    spread_no = []
    for snap in snapshots:
        yd = snap.get("yes", {})
        nd = snap.get("no", {})
        yb = yd.get("best_bid", 0)
        ya = yd.get("best_ask", 0)
        nb = nd.get("best_bid", 0)
        na = nd.get("best_ask", 0)
        if yb > 0 and ya > 0 and nb > 0 and na > 0:
            both_bids.append(yb + nb)
            spread_yes.append(ya - yb)
            spread_no.append(na - nb)

    if both_bids:
        avg_cost = sum(both_bids) / len(both_bids)
        pct_under = sum(1 for c in both_bids if c < 1.0) / len(both_bids) * 100
        avg_sp_yes = sum(spread_yes) / len(spread_yes)
        avg_sp_no = sum(spread_no) / len(spread_no)
        print("  SPREAD DIAGNOSTICS")
        print("  -" * 35)
        print(f"  YES_bid + NO_bid: avg={avg_cost:.4f}  % under 1.00={pct_under:.1f}%")
        print(f"  Avg YES spread (ask-bid): {avg_sp_yes:.4f}  ({avg_sp_yes * ORDER_SIZE:.3f} USDC per 5 tokens)")
        print(f"  Avg NO  spread (ask-bid): {avg_sp_no:.4f}  ({avg_sp_no * ORDER_SIZE:.3f} USDC per 5 tokens)")
    print("=" * 70)


if __name__ == "__main__":
    main()
