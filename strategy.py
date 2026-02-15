"""
Strategy: mid-price and optimal spread (spread_high_vol_pct), bid/ask around mid.
"""
from __future__ import annotations


def get_mid_price(best_bid: float, best_ask: float) -> float:
    """Mid from best bid/ask; fallback if one side missing."""
    if best_bid > 0 and best_ask > 0:
        return (best_bid + best_ask) / 2.0
    return best_bid if best_bid > 0 else max(best_ask, 0.01)


def get_spread_half_pct(mid: float, spread_high_vol_pct: float) -> float:
    """Half-spread in price units (0.75% => half = 0.375% of mid)."""
    return mid * (spread_high_vol_pct / 100.0) / 2.0


def get_bid_ask(
    best_bid: float,
    best_ask: float,
    spread_high_vol_pct: float,
) -> tuple[float, float]:
    """
    Optimal bid and ask around mid using spread_high_vol_pct.
    Returns (bid_price, ask_price).
    """
    mid = get_mid_price(best_bid, best_ask)
    half = get_spread_half_pct(mid, spread_high_vol_pct)
    bid_price = round(mid - half, 4)
    ask_price = round(mid + half, 4)
    # Clamp to (0, 1) for binary markets
    bid_price = max(0.01, min(0.99, bid_price))
    ask_price = max(0.01, min(0.99, ask_price))
    if bid_price >= ask_price:
        ask_price = min(0.99, bid_price + 0.01)
    return bid_price, ask_price
