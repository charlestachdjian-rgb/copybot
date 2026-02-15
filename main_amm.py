"""
Main AMM engine — True Market Maker (single fixed market).

Designed for one binary market at a time (e.g. "Bitcoin Up or Down on February 16?").
Strategy: post resting BUY orders on BOTH YES and NO tokens simultaneously.
When either buy fills, post a SELL at the ask. Profit = spread; direction-neutral.
Config: set market_slug (e.g. bitcoin-up-or-down-on-february-16). Winddown N seconds
before resolution, then exit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from execution import (
    cancel_all_open_orders,
    cancel_orders,
    get_open_orders,
    get_token_balance,
    get_usdc_balance,
    place_order,
    simulate_place_order,
)
from inventory import InventoryState, VIRTUAL_WALLET_START_USDC
from strategy import get_bid_ask, get_mid_price

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
LOOP_INTERVAL = 0.5
STRICT_TIMEOUT = 15.0
BURST_TIMEOUT_COUNT = 3
JITTER_WINDOW_CYCLES = 10
JITTER_WARNING_MS = 100
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """Load config.json; fallback to defaults."""
    defaults = {
        "market_slug": "",  # required: e.g. bitcoin-up-or-down-on-february-16
        "spread_high_vol_pct": 0.1,
        "loop_interval": 0.5,
        "POST_ONLY": True,
        "PAPER_TRADING": True,
        "TRIAL_MODE": False,
        "MAX_ORDERS_PER_SESSION": 999,
        "MAX_USDC_ESTIMATE_PER_SESSION": 0,
        "MAX_SESSION_LOSS_USDC": MAX_SESSION_LOSS_DEFAULT,
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
                defaults.update(data)
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def _parse_price(level: dict) -> float | None:
    """Extract price from CLOB order level (price may be string or number)."""
    try:
        raw = level.get("price")
        if raw is None:
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


async def fetch_order_book(
    session: aiohttp.ClientSession,
    token_id: str,
    debug_label: str | None = None,
) -> tuple[float, float]:
    """
    Fetch order book from CLOB API.
    Returns (best_bid, best_ask); (0.0, 0.0) on error.
    """
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    timeout = aiohttp.ClientTimeout(total=2)
    try:
        async with session.get(url, timeout=timeout) as resp:
            text = await resp.text()
            if debug_label is not None:
                print(f"DEBUG: CLOB Response for {debug_label}: {resp.status} - {text[:100]}")
            if resp.status != 200:
                logger.debug("CLOB book status %s", resp.status)
                return 0.0, 0.0
            if not text:
                return 0.0, 0.0
            data = json.loads(text)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        logger.debug("fetch_order_book error: %s", e)
        return 0.0, 0.0

    bids = data.get("bids") or []
    asks = data.get("asks") or []

    real_bid = 0.0
    if bids:
        prices = [p for p in (_parse_price(b) for b in bids) if p is not None and p > 0]
        real_bid = max(prices) if prices else 0.0
    real_ask = 0.0
    if asks:
        prices = [p for p in (_parse_price(a) for a in asks) if p is not None and p > 0]
        real_ask = min(prices) if prices else 0.0

    return real_bid, real_ask


# ─────────────────────────────────────────────────────────────
#  Gamma / market discovery helpers (unchanged)
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
#  Single-market constants (e.g. daily "Bitcoin Up or Down on …")
# ─────────────────────────────────────────────────────────────

ONE_LEG_TIMEOUT_SEC = 60   # if holding > 60s without sell fill -> aggressive taker exit
WINDDOWN_BEFORE_END_SEC = 300  # wind down this many seconds before resolution
MAX_SESSION_LOSS_DEFAULT = 2.0
MAX_CONSECUTIVE_API_FAILS = 3
TOXIC_SPREAD_PCT = 15.0
TOXIC_MID_DRIFT_PCT = 25.0


async def get_tokens_for_slug(
    session: aiohttp.ClientSession, slug: str
) -> tuple[str | None, str | None, str, str, float, bool, str]:
    """
    Fetch Gamma event by slug and return BOTH token IDs plus metadata.
    Return (yes_token_id, no_token_id, market_name, market_id, tick_size, neg_risk, end_date_iso).
    end_date_iso used for winddown before resolution. Returns (None, None, ...) on error.
    """
    url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None, None, "", "", 0.01, True, ""
            event = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None, None, "", "", 0.01, True, ""

    markets = event.get("markets") or []
    if not markets:
        return None, None, "", "", 0.01, True, ""

    market = markets[0]
    raw = market.get("clobTokenIds")
    if not raw:
        return None, None, "", "", 0.01, True, ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None, None, "", "", 0.01, True, ""
    if not isinstance(raw, list) or len(raw) < 2:
        return None, None, "", "", 0.01, True, ""

    yes_token = str(raw[0])
    no_token = str(raw[1])
    if len(yes_token) < 20 or len(no_token) < 20:
        return None, None, "", "", 0.01, True, ""

    name = event.get("title") or market.get("question") or slug
    market_id = str(market.get("id", ""))
    tick_size = float(market.get("orderPriceMinTickSize", 0.01))
    neg_risk = bool(market.get("negRisk", True))
    end_date_iso = str(market.get("endDate") or market.get("endDateIso") or "")

    print(f"  [Tokens] YES=...{yes_token[-8:]}  NO=...{no_token[-8:]}")
    return yes_token, no_token, name, market_id, tick_size, neg_risk, end_date_iso


def _jitter_stats(cycle_times: list[float]) -> tuple[float, float]:
    n = len(cycle_times)
    if n == 0:
        return 0.0, 0.0
    mean = sum(cycle_times) / n
    if n < 2:
        return mean, 0.0
    variance = sum((t - mean) ** 2 for t in cycle_times) / n
    return mean, math.sqrt(variance)


# ─────────────────────────────────────────────────────────────
#  Per-token state helper
# ─────────────────────────────────────────────────────────────

def _make_token_state() -> dict:
    """Fresh state for one token (YES or NO)."""
    return {
        "balance": 0.0,
        "prev_balance": 0.0,
        "entry_price": None,
        "buy_placed_at": None,
        "position_acquired_at": None,
        "last_sell_price": None,
        "round_trips": 0,
    }


# ─────────────────────────────────────────────────────────────
#  Core: market-making cycle for ONE token
# ─────────────────────────────────────────────────────────────

async def run_mm_cycle(
    session: aiohttp.ClientSession,
    config: dict,
    token_id: str,
    label: str,
    best_bid: float,
    best_ask: float,
    tick_size: float,
    neg_risk: bool,
    ts: dict,
    session_state: dict,
) -> None:
    """
    One market-making cycle for a single token (YES or NO).
    If flat -> post BUY at best_bid (POST_ONLY). If holding -> post SELL at best_ask (POST_ONLY).
    Reprice stale orders if mid drifted > 1 tick. One-legged timeout: 60s -> taker exit.
    """
    size = float(config.get("order_size", 5))
    mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0.0
    REPRICE_THRESHOLD = tick_size  # 1 tick

    # ── Trial guardrails ──
    trial_mode = config.get("TRIAL_MODE", False)
    if trial_mode:
        max_orders = int(config.get("MAX_ORDERS_PER_SESSION", 999))
        max_usdc = float(config.get("MAX_USDC_ESTIMATE_PER_SESSION", 0))
        orders_done = session_state.get("live_orders_placed", 0)
        usdc_done = session_state.get("estimated_usdc_placed", 0.0)
        if orders_done >= max_orders:
            return
        if max_usdc > 0 and usdc_done + size * 0.60 > max_usdc:
            return

    # ── Fetch balance ──
    balance_raw = await get_token_balance(token_id)
    if balance_raw is None:
        print(f"  [{label}] get_token_balance FAILED — skipping")
        fails = session_state.get("consecutive_api_failures", 0) + 1
        session_state["consecutive_api_failures"] = fails
        if fails >= MAX_CONSECUTIVE_API_FAILS:
            print(f"  [{label}] {fails} consecutive API failures — stopping")
            session_state["window_stopped"] = True
        return
    balance: float = balance_raw

    # ── Fetch open orders ──
    open_orders = await get_open_orders(token_id)
    if open_orders is None:
        print(f"  [{label}] get_open_orders FAILED — skipping")
        fails = session_state.get("consecutive_api_failures", 0) + 1
        session_state["consecutive_api_failures"] = fails
        if fails >= MAX_CONSECUTIVE_API_FAILS:
            session_state["window_stopped"] = True
        return

    session_state["consecutive_api_failures"] = 0

    entry_str = f"entry={ts['entry_price']:.4f}" if ts.get("entry_price") else "flat"
    print(f"  [{label}] bal={balance:.1f}  bid={best_bid:.4f}  ask={best_ask:.4f}  mid={mid:.4f}  {entry_str}")

    # ── P&L transition detection ──
    prev_bal = ts.get("prev_balance", 0.0)

    # Detect BUY fill: was flat, now holding
    if prev_bal == 0 and balance > 0:
        ep = ts.get("entry_price")
        if ep and ep > 0:
            cost = balance * ep
            session_state["est_cost"] = session_state.get("est_cost", 0.0) + cost
            print(f"  [{label}] BUY FILLED: {balance:.0f} tokens @ {ep:.4f} = cost {cost:.2f} USDC")

    # Detect SELL fill: was holding, now flat
    if prev_bal > 0 and balance == 0:
        lsp = ts.get("last_sell_price")
        if lsp is not None:
            revenue = prev_bal * lsp
            session_state["est_revenue"] = session_state.get("est_revenue", 0.0) + revenue
            ts["round_trips"] = ts.get("round_trips", 0) + 1
            spread_captured = lsp - (ts.get("entry_price") or lsp)
            print(f"  [{label}] SELL FILLED: {prev_bal:.0f} tokens @ {lsp:.4f} = +{revenue:.2f} USDC "
                  f"(spread {spread_captured:+.4f}/token, RT #{ts['round_trips']})")
        else:
            print(f"  [{label}] Position resolved (no active sell). Revenue = resolution payout.")
        ts["last_sell_price"] = None
    ts["prev_balance"] = balance

    # ── P&L kill switch ──
    est_pnl = session_state.get("est_revenue", 0.0) - session_state.get("est_cost", 0.0)
    max_loss = float(config.get("MAX_SESSION_LOSS_USDC", MAX_SESSION_LOSS_DEFAULT))
    if est_pnl < -max_loss:
        print(f"  [KILL SWITCH] P&L est: {est_pnl:+.2f} (limit: -{max_loss:.1f}). STOPPING.")
        session_state["pnl_killed"] = True
        return

    # ══════════════════════════════════════════════════════════
    if balance > 0:
        # ═══ HOLDING: manage sell ═══
        sell_qty = math.floor(balance)
        if sell_qty < 5:
            print(f"  [{label}] balance {balance:.2f} < min 5 — letting resolve")
            return

        # Cancel any residual BUY orders while holding
        open_buys = [o for o in open_orders if str(o.get("side", "")).upper() == "BUY"]
        if open_buys:
            buy_ids = [str(o.get("id", "")) for o in open_buys if o.get("id")]
            print(f"  [{label}] cancelling {len(buy_ids)} residual BUY(s)")
            await cancel_orders(buy_ids)

        # Track acquisition time
        if ts.get("position_acquired_at") is None:
            ts["position_acquired_at"] = ts.get("buy_placed_at") or time.time()

        held_sec = time.time() - ts["position_acquired_at"]

        # ── One-legged timeout: aggressive taker exit ──
        if held_sec > ONE_LEG_TIMEOUT_SEC:
            print(f"  [{label}] HELD {held_sec:.0f}s > {ONE_LEG_TIMEOUT_SEC}s — aggressive exit @ bid={best_bid:.4f}")
            # Cancel existing sells first
            open_sells = [o for o in open_orders if str(o.get("side", "")).upper() == "SELL"]
            if open_sells:
                sell_ids = [str(o.get("id", "")) for o in open_sells if o.get("id")]
                await cancel_orders(sell_ids)
            # Taker sell at best_bid
            if best_bid > 0:
                result = await place_order(
                    session, token_id, "SELL", best_bid, sell_qty,
                    post_only=False, tick_size=tick_size, neg_risk=neg_risk,
                )
                if result.get("ok"):
                    print(f"  [{label}] -> Taker SELL accepted")
                    ts["last_sell_price"] = best_bid
                    session_state["live_orders_placed"] = session_state.get("live_orders_placed", 0) + 1
                else:
                    print(f"  [{label}] -> SELL REJECTED: {result.get('data', {})}")
            return

        # ── Check existing SELL orders + reprice ──
        open_sells = [o for o in open_orders if str(o.get("side", "")).upper() == "SELL"]
        if open_sells:
            stale_ids = []
            for o in open_sells:
                try:
                    o_price = float(o.get("price", 0))
                except (TypeError, ValueError):
                    o_price = 0.0
                if abs(o_price - best_ask) > REPRICE_THRESHOLD:
                    stale_ids.append(str(o.get("id", "")))
                    print(f"  [{label}] stale SELL @ {o_price:.4f} vs target {best_ask:.4f}")
            if stale_ids:
                ok = await cancel_orders(stale_ids)
                if not ok:
                    print(f"  [{label}] cancel FAILED — skip to avoid stacking")
                    return
                print(f"  [{label}] cancelled {len(stale_ids)} stale SELL(s), re-placing")
                # Fall through to place new sell
            else:
                print(f"  [{label}] SELL at target price — waiting for fill (held {held_sec:.0f}s)")
                return

        # ── Place SELL at ask (maker) ──
        if best_ask <= 0:
            return
        pnl_est = ""
        if ts.get("entry_price"):
            spread_est = (best_ask - ts["entry_price"]) * sell_qty
            pnl_est = f" est_pnl={spread_est:+.3f}"
        print(f"  [{label}] SELL {sell_qty} @ {best_ask:.4f} (POST_ONLY){pnl_est}")
        result = await place_order(
            session, token_id, "SELL", best_ask, sell_qty,
            post_only=True, tick_size=tick_size, neg_risk=neg_risk,
        )
        if result.get("ok"):
            print(f"  [{label}] -> SELL order accepted")
            ts["last_sell_price"] = best_ask
            session_state["live_orders_placed"] = session_state.get("live_orders_placed", 0) + 1
        else:
            print(f"  [{label}] -> SELL REJECTED: {result.get('data', {})}")

    else:
        # ═══ FLAT: manage buy ═══

        # Reset position tracking
        ts["position_acquired_at"] = None
        ts["entry_price"] = None
        ts["buy_placed_at"] = None

        # ── Check existing BUY orders + reprice ──
        open_buys = [o for o in open_orders if str(o.get("side", "")).upper() == "BUY"]
        if open_buys:
            stale_ids = []
            for o in open_buys:
                try:
                    o_price = float(o.get("price", 0))
                except (TypeError, ValueError):
                    o_price = 0.0
                if abs(o_price - best_bid) > REPRICE_THRESHOLD:
                    stale_ids.append(str(o.get("id", "")))
                    print(f"  [{label}] stale BUY @ {o_price:.4f} vs target {best_bid:.4f}")
            if stale_ids:
                ok = await cancel_orders(stale_ids)
                if not ok:
                    print(f"  [{label}] cancel FAILED — skip to avoid stacking")
                    return
                print(f"  [{label}] cancelled {len(stale_ids)} stale BUY(s), re-placing")
                # Fall through to place new buy
            else:
                print(f"  [{label}] BUY at target price — waiting for fill")
                return

        # Cancel orphaned SELL orders while flat
        open_sells = [o for o in open_orders if str(o.get("side", "")).upper() == "SELL"]
        if open_sells:
            sell_ids = [str(o.get("id", "")) for o in open_sells if o.get("id")]
            print(f"  [{label}] cancelling {len(sell_ids)} orphaned SELL(s)")
            await cancel_orders(sell_ids)

        # ── Place BUY at bid (maker) ──
        if best_bid <= 0:
            return
        print(f"  [{label}] BUY {size:.0f} @ {best_bid:.4f} (POST_ONLY)")
        result = await place_order(
            session, token_id, "BUY", best_bid, size,
            post_only=True, tick_size=tick_size, neg_risk=neg_risk,
        )
        if result.get("ok"):
            print(f"  [{label}] -> BUY order accepted")
            ts["entry_price"] = best_bid
            ts["buy_placed_at"] = time.time()
            session_state["live_orders_placed"] = session_state.get("live_orders_placed", 0) + 1
            session_state["estimated_usdc_placed"] = (
                session_state.get("estimated_usdc_placed", 0.0) + size * best_bid
            )
        else:
            print(f"  [{label}] -> BUY REJECTED: {result.get('data', {})}")


# ─────────────────────────────────────────────────────────────
#  Preflight (adapted for two-token)
# ─────────────────────────────────────────────────────────────

async def refresh_state() -> None:
    await asyncio.sleep(0.05)


async def preflight(
    session: aiohttp.ClientSession,
    yes_token_id: str,
    no_token_id: str,
    config: dict,
    tick_size: float,
    neg_risk: bool,
) -> bool:
    """
    Pre-launch safety checklist for two-token market maker.
    Returns True if ALL checks pass, False to abort.
    """
    order_size = float(config.get("order_size", 5))
    print("\n" + "=" * 60)
    print("  PREFLIGHT SAFETY CHECKLIST (True Market Maker)")
    print("=" * 60)
    all_ok = True

    # CHECK 1: Config sanity
    print("\n[1/7] Config sanity...")
    config_ok = True
    if order_size < 5:
        print(f"  FAIL: order_size={order_size} < 5")
        config_ok = False
    li = float(config.get("loop_interval", 0))
    if li <= 0:
        print(f"  FAIL: loop_interval={li} <= 0")
        config_ok = False
    max_loss = float(config.get("MAX_SESSION_LOSS_USDC", 0))
    if max_loss <= 0:
        print(f"  FAIL: MAX_SESSION_LOSS_USDC={max_loss} <= 0")
        config_ok = False
    if config_ok:
        print(f"  order_size={order_size}  loop={li}s  max_loss={max_loss} USDC  PASS")
    else:
        all_ok = False

    # CHECK 2: Stale orders cleanup
    print("\n[2/7] Stale orders cleanup...")
    try:
        await cancel_all_open_orders()
        await asyncio.sleep(0.5)
        print(f"  Cancelled all. PASS")
    except Exception as e:
        print(f"  WARN: {e}. Proceeding.")

    # CHECK 3: Existing position check (both tokens)
    print("\n[3/7] Existing position check...")
    for label, tid in [("YES", yes_token_id), ("NO", no_token_id)]:
        bal = await get_token_balance(tid)
        if bal is None:
            print(f"  {label}: WARN — could not fetch balance")
        elif bal > 0:
            print(f"  {label}: WARNING — holding {bal:.2f} tokens from previous session")
        else:
            print(f"  {label}: flat")
    print(f"  PASS")

    # CHECK 4: USDC balance (need enough for BOTH buy orders)
    print("\n[4/7] USDC balance check...")
    usdc = await get_usdc_balance()
    if usdc is None:
        print("  FAIL: Could not fetch USDC balance.")
        all_ok = False
    else:
        # Two buy orders: order_size * ~0.60 each
        min_required = order_size * 0.60 * 2
        print(f"  USDC balance: {usdc:.2f}")
        print(f"  Min required: {min_required:.2f} (2 x {order_size} x 0.60)")
        if usdc < min_required:
            print(f"  FAIL: {usdc:.2f} < {min_required:.2f}")
            all_ok = False
        else:
            print(f"  PASS")

    # CHECK 5: Orderbook health (both tokens)
    print("\n[5/7] Orderbook health...")
    for label, tid in [("YES", yes_token_id), ("NO", no_token_id)]:
        ob_bid, ob_ask = await fetch_order_book(session, tid)
        if ob_bid <= 0 or ob_ask <= 0:
            print(f"  {label}: FAIL — missing side(s). bid={ob_bid} ask={ob_ask}")
            all_ok = False
        elif ob_bid >= ob_ask:
            print(f"  {label}: FAIL — crossed. bid={ob_bid:.4f} >= ask={ob_ask:.4f}")
            all_ok = False
        else:
            ob_mid = (ob_bid + ob_ask) / 2
            sp = (ob_ask - ob_bid) / ob_mid * 100 if ob_mid > 0 else 999
            print(f"  {label}: bid={ob_bid:.4f} ask={ob_ask:.4f} spread={sp:.1f}% PASS")

    # CHECK 6: Clock drift
    print("\n[6/7] Clock drift check...")
    try:
        t_before = time.time()
        async with session.get(
            "https://clob.polymarket.com/time",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            t_after = time.time()
            if resp.status == 200:
                server_data = await resp.json()
                server_ts = float(server_data) if isinstance(server_data, (int, float)) else float(server_data.get("time", 0))
                if server_ts > 1e12:
                    server_ts /= 1000.0
                local_ts = (t_before + t_after) / 2.0
                drift = abs(local_ts - server_ts)
                print(f"  Drift: {drift:.2f}s {'PASS' if drift <= 3 else 'FAIL'}")
                if drift > 3.0:
                    all_ok = False
            else:
                print(f"  WARN: /time status {resp.status}. Proceeding.")
    except Exception as e:
        print(f"  WARN: {e}. Proceeding.")

    # CHECK 7: Execution test (use YES token)
    print("\n[7/7] Execution test...")
    test_price = 0.01
    try:
        result = await place_order(
            session, yes_token_id, "BUY", test_price, order_size,
            post_only=True, tick_size=tick_size, neg_risk=neg_risk,
        )
        if not result.get("ok"):
            print(f"  FAIL: Test order rejected: {result.get('data', {})}")
            all_ok = False
        else:
            data = result.get("data", {})
            oid = data.get("orderID") or data.get("order_id") or data.get("id")
            if oid:
                print(f"  Placed OK (id: {str(oid)[:16]}...)")
                await asyncio.sleep(1.0)
                await cancel_orders([str(oid)])
                await asyncio.sleep(0.5)
                print(f"  Cancelled. PASS")
            else:
                print(f"  Placed OK but no order ID. PASS (with warning)")
    except Exception as e:
        print(f"  FAIL: {e}")
        all_ok = False

    print("\n" + "=" * 60)
    if all_ok:
        print("  PREFLIGHT COMPLETE — SYSTEM SAFE TO TRADE")
    else:
        print("  PREFLIGHT FAILED — ABORTING LAUNCH")
    print("=" * 60 + "\n")
    return all_ok


# ─────────────────────────────────────────────────────────────
#  Winddown for two tokens
# ─────────────────────────────────────────────────────────────

async def winddown_token(
    session: aiohttp.ClientSession,
    token_id: str,
    label: str,
    best_bid: float,
    tick_size: float,
    neg_risk: bool,
    ts: dict,
) -> None:
    """Aggressively exit one token: cancel orders, taker-sell if holding."""
    bal = await get_token_balance(token_id)
    if bal is not None and bal > 0 and best_bid > 0:
        sell_qty = math.floor(bal)
        if sell_qty >= 5:
            print(f"  [{label}] WindDown: holding {bal:.0f} — forced sell {sell_qty} @ bid={best_bid:.4f}")
            result = await place_order(
                session, token_id, "SELL", best_bid, sell_qty,
                post_only=False, tick_size=tick_size, neg_risk=neg_risk,
            )
            if result.get("ok"):
                print(f"  [{label}] WindDown: taker SELL accepted")
                ts["last_sell_price"] = best_bid
            else:
                print(f"  [{label}] WindDown: SELL REJECTED — tokens ride to resolution")
        else:
            print(f"  [{label}] WindDown: balance {bal:.2f} < 5 — letting resolve")
    else:
        print(f"  [{label}] WindDown: flat")


# ─────────────────────────────────────────────────────────────
#  Main loop
# ─────────────────────────────────────────────────────────────

async def main_loop() -> None:
    config = load_config()
    loop_interval = config.get("loop_interval", LOOP_INTERVAL)
    paper = config.get("PAPER_TRADING", True)
    market_slug = (config.get("market_slug") or "").strip()
    if not market_slug:
        print("FATAL: config market_slug is required (e.g. bitcoin-up-or-down-on-february-16). Exiting.")
        return

    timeout_count = 0
    inventory = InventoryState(
        starting_equity_usdc=VIRTUAL_WALLET_START_USDC,
        virtual_balance_usdc=VIRTUAL_WALLET_START_USDC,
    )
    cycle_times: list[float] = []
    cycle_count = 0
    market_name = ""
    tick_size, neg_risk = 0.01, True
    end_date_ts: float | None = None

    yes_token_id: str = ""
    no_token_id: str = ""
    yes_bid, yes_ask = 0.48, 0.52
    no_bid, no_ask = 0.48, 0.52

    async with aiohttp.ClientSession() as session:
        yt, nt, name, _mid, tick_size, neg_risk, end_date_iso = await get_tokens_for_slug(
            session, market_slug
        )
        if not yt or not nt:
            print(f"FATAL: Could not fetch tokens for slug '{market_slug}'. Exiting.")
            return
        yes_token_id = yt
        no_token_id = nt
        market_name = name
        if end_date_iso:
            try:
                end_date_ts = datetime.fromisoformat(
                    end_date_iso.replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, TypeError):
                end_date_ts = None
        print(f"SYSTEM: {market_name}")
        print(f"  slug: {market_slug}  |  resolution: {end_date_iso or 'n/a'}")
        logger.info("Market: %s", name)

        print(f"--- STARTING TRUE MARKET MAKER: {market_name} ---")
        if not paper:
            print("*** LIVE — TRUE MARKET MAKER (spread capture, direction-neutral) ***")
            print(f"*** Quoting BOTH YES and NO tokens simultaneously ***")
            print(f"*** Winddown {WINDDOWN_BEFORE_END_SEC}s before resolution ***")
            print(f"*** One-leg timeout: {ONE_LEG_TIMEOUT_SEC}s ***")
            max_loss = config.get("MAX_SESSION_LOSS_USDC", MAX_SESSION_LOSS_DEFAULT)
            print(f"*** Safety: P&L kill @ -{max_loss} USDC | Toxic flow | API fail @ {MAX_CONSECUTIVE_API_FAILS} ***")
            trial_mode = config.get("TRIAL_MODE", False)
            if trial_mode:
                max_ord = config.get("MAX_ORDERS_PER_SESSION", 999)
                max_usdc = config.get("MAX_USDC_ESTIMATE_PER_SESSION", 0)
                print(f"*** TRIAL MODE: max {max_ord} orders, max USDC estimate {max_usdc or 'none'} ***")

            safe = await preflight(session, yes_token_id, no_token_id, config, tick_size, neg_risk)
            if not safe:
                print("ABORTING: Preflight checks failed.")
                return

        # ── Session state (shared across both tokens) ──
        session_state: dict = {
            "live_orders_placed": 0,
            "estimated_usdc_placed": 0.0,
            "est_cost": 0.0,
            "est_revenue": 0.0,
            "pnl_killed": False,
            "consecutive_api_failures": 0,
            "window_stopped": False,
            "winddown_done": False,
            "last_yes_mid": None,
            "last_no_mid": None,
        }

        yes_state = _make_token_state()
        no_state = _make_token_state()

        while True:
            cycle_start = time.perf_counter()

            # ── Fetch order books for both tokens ──
            fb_yes = await fetch_order_book(session, yes_token_id)
            if fb_yes[0] > 0:
                yes_bid = fb_yes[0]
            if fb_yes[1] > 0:
                yes_ask = fb_yes[1]

            fb_no = await fetch_order_book(session, no_token_id)
            if fb_no[0] > 0:
                no_bid = fb_no[0]
            if fb_no[1] > 0:
                no_ask = fb_no[1]

            yes_mid = (yes_bid + yes_ask) / 2 if yes_bid > 0 and yes_ask > 0 else 0
            no_mid = (no_bid + no_ask) / 2 if no_bid > 0 and no_ask > 0 else 0
            logger.info("YES bid=%.4f ask=%.4f | NO bid=%.4f ask=%.4f", yes_bid, yes_ask, no_bid, no_ask)

            # ── Toxic flow detection (check YES book as proxy) ──
            if not paper and yes_bid > 0 and yes_ask > 0:
                toxic = False
                spread_raw = (yes_ask - yes_bid) / yes_mid * 100 if yes_mid > 0 else 999
                if spread_raw > TOXIC_SPREAD_PCT:
                    print(f"  [ToxicFlow] YES spread {spread_raw:.1f}% > {TOXIC_SPREAD_PCT}% — skipping")
                    toxic = True
                if not toxic:
                    last_ym = session_state.get("last_yes_mid")
                    if last_ym and last_ym > 0 and yes_mid > 0:
                        drift = abs(yes_mid - last_ym) / last_ym * 100
                        if drift > TOXIC_MID_DRIFT_PCT:
                            print(f"  [ToxicFlow] YES mid drift {drift:.1f}% > {TOXIC_MID_DRIFT_PCT}% — skipping")
                            toxic = True
                session_state["last_yes_mid"] = yes_mid
                session_state["last_no_mid"] = no_mid
                if toxic:
                    await cancel_all_open_orders()
                    cycle_count += 1
                    elapsed = time.perf_counter() - cycle_start
                    sleep_for = loop_interval - elapsed
                    if sleep_for > 0:
                        await asyncio.sleep(sleep_for)
                    continue

            # ── API failure backoff ──
            if not paper and session_state.get("window_stopped"):
                print(f"  [Stopped] Too many API failures. Skipping cycle.")
                cycle_count += 1
                elapsed = time.perf_counter() - cycle_start
                sleep_for = loop_interval - elapsed
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                continue

            # ── Winddown: N seconds before resolution ──
            winddown_due = (
                end_date_ts is not None
                and time.time() >= end_date_ts - WINDDOWN_BEFORE_END_SEC
            )
            if not paper and winddown_due:
                if not session_state.get("winddown_done"):
                    print(f"  [WindDown] Within {WINDDOWN_BEFORE_END_SEC}s of resolution — flattening and exiting")
                    await cancel_all_open_orders()
                    await winddown_token(session, yes_token_id, "YES", yes_bid, tick_size, neg_risk, yes_state)
                    await winddown_token(session, no_token_id, "NO", no_bid, tick_size, neg_risk, no_state)
                    session_state["winddown_done"] = True
                    print("  [WindDown] Done. Exiting (market will resolve).")
                    break
                elapsed = time.perf_counter() - cycle_start
                sleep_for = loop_interval - elapsed
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                continue

            # ── ACTIVE TRADING: run MM cycle for both tokens ──
            if not paper:
                try:
                    ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    spread_yes = (yes_ask - yes_bid) if yes_bid > 0 and yes_ask > 0 else 0
                    spread_no = (no_ask - no_bid) if no_bid > 0 and no_ask > 0 else 0
                    rt_yes = yes_state.get("round_trips", 0)
                    rt_no = no_state.get("round_trips", 0)
                    est_pnl = session_state.get("est_revenue", 0.0) - session_state.get("est_cost", 0.0)
                    print(f"\n[{ts_str}] YES spread={spread_yes:.3f} | NO spread={spread_no:.3f} | "
                          f"RTs: YES={rt_yes} NO={rt_no} | est_pnl={est_pnl:+.3f}")

                    await asyncio.wait_for(
                        run_mm_cycle(
                            session, config, yes_token_id, "YES",
                            yes_bid, yes_ask, tick_size, neg_risk,
                            yes_state, session_state,
                        ),
                        timeout=STRICT_TIMEOUT / 2,
                    )

                    await asyncio.wait_for(
                        run_mm_cycle(
                            session, config, no_token_id, "NO",
                            no_bid, no_ask, tick_size, neg_risk,
                            no_state, session_state,
                        ),
                        timeout=STRICT_TIMEOUT / 2,
                    )

                    timeout_count = 0

                    # P&L kill switch
                    if session_state.get("pnl_killed"):
                        print(f"\n*** P&L KILL SWITCH ***")
                        await cancel_all_open_orders()
                        break

                    # Trial caps
                    if config.get("TRIAL_MODE", False):
                        max_ord = int(config.get("MAX_ORDERS_PER_SESSION", 999))
                        if session_state.get("live_orders_placed", 0) >= max_ord:
                            print(f"\n*** TRIAL CAP: {session_state['live_orders_placed']} orders ***")
                            await cancel_all_open_orders()
                            break
                        max_usdc = float(config.get("MAX_USDC_ESTIMATE_PER_SESSION", 0))
                        if max_usdc > 0 and session_state.get("estimated_usdc_placed", 0) >= max_usdc:
                            print(f"\n*** TRIAL CAP: USDC limit ***")
                            await cancel_all_open_orders()
                            break

                except asyncio.TimeoutError:
                    timeout_count += 1
                    print(f"  [Timeout] cycle timed out ({timeout_count}/{BURST_TIMEOUT_COUNT})")
                    if timeout_count >= BURST_TIMEOUT_COUNT:
                        await refresh_state()
                        timeout_count = 0
                except Exception as e:
                    logger.exception("Cycle error: %s", e)
                    timeout_count = 0
            else:
                # Paper trading (simplified — uses old single-token sim)
                try:
                    force_fill_debug = cycle_count < 200
                    await asyncio.wait_for(
                        _paper_cycle(
                            session, config, inventory, yes_bid, yes_ask,
                            yes_token_id, tick_size, neg_risk, force_fill_debug,
                        ),
                        timeout=STRICT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass

            elapsed = time.perf_counter() - cycle_start
            cycle_times.append(elapsed)
            if len(cycle_times) > JITTER_WINDOW_CYCLES:
                cycle_times.pop(0)
            cycle_count += 1

            if len(cycle_times) >= JITTER_WINDOW_CYCLES:
                mean_sec, std_sec = _jitter_stats(cycle_times)
                std_ms = std_sec * 1000
                if std_ms > JITTER_WARNING_MS:
                    logger.warning("High Jitter (std=%.1f ms, avg=%.1f ms)", std_ms, mean_sec * 1000)

            sleep_for = loop_interval - elapsed
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)


async def _paper_cycle(
    session: aiohttp.ClientSession,
    config: dict,
    inventory: InventoryState,
    best_bid: float,
    best_ask: float,
    token_id: str,
    tick_size: float,
    neg_risk: bool,
    force_fill_debug: bool,
) -> None:
    """Paper trading cycle (simplified, single-token, for dashboard testing)."""
    post_only = config.get("POST_ONLY", True)
    size = float(config.get("order_size", 10))
    mid = get_mid_price(best_bid, best_ask)
    mark_prices = {token_id: mid}

    if best_bid > 0 and best_ask > 0:
        bid_price, ask_price = best_bid, best_ask
    else:
        spread_pct = config.get("spread_high_vol_pct", 0.75)
        bid_price, ask_price = get_bid_ask(best_bid, best_ask, spread_pct)

    if inventory.is_stop_loss_hit(mark_prices):
        return

    logger.info(
        "Paper: BUY %.2f @ %.4f | SELL %.2f @ %.4f (mid=%.4f)",
        size, bid_price, size, ask_price, mid,
    )
    await simulate_place_order(
        token_id, "BUY", bid_price, size, best_bid, best_ask, post_only, inventory, force_fill_debug
    )
    await simulate_place_order(
        token_id, "SELL", ask_price, size, best_bid, best_ask, post_only, inventory, force_fill_debug
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main_loop())
