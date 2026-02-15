"""
Execution: POST_ONLY orders via Polymarket CLOB API.
Paper: simulate_place_order (virtual fills to trades.csv).
Live: place_order uses py-clob-client (EIP-712, .env POLY_*).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if TYPE_CHECKING:
    from inventory import InventoryState

if load_dotenv is not None:
    load_dotenv(Path(__file__).resolve().parent / ".env")

CLOB_BASE = "https://clob.polymarket.com"
_clob_client: Any = None
# Same folder as dashboard so both bot and Streamlit see the same file
TRADES_CSV = str(Path(__file__).resolve().parent / "trades.csv")
# 0 = trigger fill logic immediately when price is fetched (faster polling for high-latency VPN)
SIMULATED_LATENCY_SEC = 0.0
# Consider filled if best bid/ask comes within this many cents of our order (relaxed for paper testing)
FILL_TOLERANCE = 0.005

logger = logging.getLogger(__name__)


async def simulate_place_order(
    token_id: str,
    side: str,
    price: float,
    size: float,
    best_bid: float,
    best_ask: float,
    post_only: bool,
    inventory: "InventoryState",
    force_fill_debug: bool = False,
) -> dict[str, Any]:
    """
    Paper trading: no real API. Check orderbook immediately (no artificial delay).
    If POST_ONLY and order would cross (taker), log Rejected and return.
    Filled if market touches limit OR best bid/ask within FILL_TOLERANCE (0.005), or if force_fill_debug.
    """
    if SIMULATED_LATENCY_SEC > 0:
        await asyncio.sleep(SIMULATED_LATENCY_SEC)

    side_upper = side.upper()
    if post_only and not force_fill_debug:
        if side_upper == "BUY" and best_ask > 0 and price >= best_ask:
            logger.warning("Rejected (would be Taker): BUY @ %.4f >= best_ask %.4f", price, best_ask)
            return {"ok": False, "rejected": True, "filled": False}
        if side_upper == "SELL" and best_bid > 0 and price <= best_bid:
            logger.warning("Rejected (would be Taker): SELL @ %.4f <= best_bid %.4f", price, best_bid)
            return {"ok": False, "rejected": True, "filled": False}

    # Simulated fill: exact cross, or within FILL_TOLERANCE (0.5 cents), or debug force
    filled = force_fill_debug
    if not filled:
        if side_upper == "BUY" and best_ask > 0 and best_ask <= price + FILL_TOLERANCE:
            filled = True
        if side_upper == "SELL" and best_bid > 0 and best_bid >= price - FILL_TOLERANCE:
            filled = True

    if not filled:
        return {"ok": True, "rejected": False, "filled": False}

    success, realized_pnl_delta = inventory.simulate_fill(token_id, side_upper, size, price)
    if not success:
        return {"ok": False, "rejected": False, "filled": False}

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    append_trade_csv(ts, side_upper, price, size, token_id, realized_pnl_delta)
    logger.info("Virtual Trade FILLED %s %.2f @ %.4f (realized_pnl=%.2f)", side_upper, size, price, realized_pnl_delta)
    print(f"  >>> Virtual Trade FILLED: {side_upper} {size:.0f} @ {price:.4f}  |  P/L this trade: {realized_pnl_delta:+.2f} USDC")
    return {"ok": True, "rejected": False, "filled": True}


def _get_clob_client():
    """Lazy-init ClobClient from .env (POLY_PRIVATE_KEY, POLY_FUNDER, POLY_SIGNATURE_TYPE, POLY_CHAIN_ID)."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    from py_clob_client.client import ClobClient

    pk = (os.getenv("POLY_PRIVATE_KEY") or os.getenv("PRIVATE_KEY") or "").strip()
    if pk.startswith("0x"):
        pk = pk[2:]
    if not pk:
        raise RuntimeError("POLY_PRIVATE_KEY or PRIVATE_KEY required in .env for live orders")
    host = CLOB_BASE
    chain_id = int(os.getenv("POLY_CHAIN_ID", "137"))
    sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
    funder = (os.getenv("POLY_FUNDER") or "").strip() or None
    _clob_client = ClobClient(host, key=pk, chain_id=chain_id, signature_type=sig_type, funder=funder)
    _clob_client.set_api_creds(_clob_client.create_or_derive_api_creds())
    return _clob_client


def _get_token_balance_sync(token_id: str) -> float:
    """Sync: fetch conditional token balance for token_id. Returns 0.0 on error."""
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    client = _get_clob_client()
    params = BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL,
        token_id=token_id,
    )
    resp = client.get_balance_allowance(params)
    # resp is typically {"balance": "5.0", "allowance": "..."} or similar
    try:
        return float(resp.get("balance", 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _get_usdc_balance_sync() -> float:
    """Sync: fetch USDC (collateral) balance via CLOB client. Returns 0.0 on error."""
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    client = _get_clob_client()
    # COLLATERAL = USDC. Some library versions require token_id even for collateral.
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    except TypeError:
        # Fallback: pass a dummy token_id if the constructor requires it
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, token_id="")
    resp = client.get_balance_allowance(params)
    try:
        return float(resp.get("balance", 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


async def get_usdc_balance() -> float | None:
    """Async: get USDC (collateral) balance. Returns None on API error."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _get_usdc_balance_sync)
    except Exception as e:
        logger.warning("get_usdc_balance failed: %s", e)
        return None


async def get_token_balance(token_id: str) -> float | None:
    """Async: get conditional token balance for token_id. Returns None on API error."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: _get_token_balance_sync(token_id))
    except Exception as e:
        logger.warning("get_token_balance failed: %s", e)
        return None


def _get_open_orders_sync(token_id: str) -> list[dict[str, Any]]:
    """Sync: fetch open orders for a specific token (asset_id). Returns [] on error."""
    from py_clob_client.clob_types import OpenOrderParams

    client = _get_clob_client()
    params = OpenOrderParams(asset_id=token_id)
    return client.get_orders(params)


async def get_open_orders(token_id: str) -> list[dict[str, Any]] | None:
    """Async: get open orders for token_id. Returns None on error (caller should skip placing)."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: _get_open_orders_sync(token_id))
    except Exception as e:
        logger.warning("get_open_orders failed: %s", e)
        return None


def _cancel_orders_sync(order_ids: list[str]) -> dict[str, Any]:
    """Sync: cancel specific orders by ID list."""
    client = _get_clob_client()
    return client.cancel_orders(order_ids)


async def cancel_orders(order_ids: list[str]) -> bool:
    """Async: cancel specific orders by their IDs. Returns True on success, False on failure."""
    if not order_ids:
        return True
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, lambda: _cancel_orders_sync(order_ids))
        logger.info("cancel_orders(%s): %s", order_ids, result)
        return True
    except Exception as e:
        logger.warning("cancel_orders failed: %s", e)
        return False


def _cancel_all_open_orders_sync() -> dict[str, Any]:
    """Sync: cancel all open orders for the authenticated user."""
    client = _get_clob_client()
    return client.cancel_all()


async def cancel_all_open_orders() -> None:
    """Async: cancel all open orders. Logs result."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _cancel_all_open_orders_sync)
        logger.info("cancel_all_open_orders: %s", result)
    except Exception as e:
        logger.warning("cancel_all_open_orders failed: %s", e)


def _place_order_sync(
    token_id: str, side: str, price: float, size: float,
    tick_size: float, neg_risk: bool, post_only: bool = True,
) -> dict[str, Any]:
    """Sync: create and post one order via py-clob-client (run in thread).
    post_only=True ensures the order rests on the book (maker, 0% fee).
    post_only=False allows immediate matching (taker, ~0.5% fee).
    """
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import BUY, SELL

    client = _get_clob_client()
    side_const = BUY if side.upper() == "BUY" else SELL
    options = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)
    order_args = OrderArgs(token_id=token_id, price=price, size=size, side=side_const)
    # Pass post_only to the CLOB API so maker orders don't accidentally cross the spread
    try:
        resp = client.create_and_post_order(
            order_args, options=options, order_type=OrderType.GTC, post_only=post_only,
        )
    except TypeError:
        # Fallback for older py-clob-client versions that don't support post_only kwarg
        resp = client.create_and_post_order(order_args, options=options)
    return {"ok": getattr(resp, "success", resp.get("success", False)), "data": resp}


async def place_order(
    session: aiohttp.ClientSession,
    token_id: str,
    side: str,
    price: float,
    size: float,
    post_only: bool = True,
    tick_size: float = 0.01,
    neg_risk: bool = True,
    api_key: str | None = None,
    api_secret: str | None = None,
    api_passphrase: str | None = None,
) -> dict[str, Any]:
    """
    Place a single order on Polymarket CLOB via py-clob-client (EIP-712).
    Uses .env: POLY_PRIVATE_KEY, POLY_FUNDER, POLY_SIGNATURE_TYPE.
    """
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: _place_order_sync(token_id, side, price, size, tick_size, neg_risk, post_only),
        )
    except Exception as e:
        logger.exception("place_order failed: %s", e)
        return {"ok": False, "status": 0, "data": {"error": str(e)}}
    ok = result.get("ok", False)
    data = result.get("data", {})
    if ok:
        logger.info("Order placed: %s %.2f @ %.4f -> %s", side, size, price, data.get("orderID", ""))
    else:
        logger.warning("Order rejected: %s", data)
    return {"ok": ok, "status": 200 if ok else 400, "data": data}


def append_trade_csv(
    timestamp: str,
    side: str,
    price: float,
    size: float,
    token_id: str,
    realized_pnl: float = 0.0,
    filepath: str = TRADES_CSV,
) -> None:
    """Append one trade to trades.csv for dashboard."""
    import csv
    from pathlib import Path

    path = Path(filepath)
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["timestamp", "side", "price", "size", "token_id", "realized_pnl"])
        w.writerow([timestamp, side, price, size, token_id, realized_pnl])
