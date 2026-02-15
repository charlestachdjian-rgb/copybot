"""
Place ONE real limit order on Polymarket to verify live API + signing.

Follows https://docs.polymarket.com/quickstart/first-order (derive API creds, get_market for tick_size/neg_risk, create_and_post_order).
Uses py-clob-client. Default: buy NO on "12°C or higher" for London Feb 15 2026 temp.
Order is a small BUY at 0.01 so it rests on the book.

Requirements:
  - pip install py-clob-client python-dotenv
  - .env or environment:
      POLY_PRIVATE_KEY   = your wallet private key (hex, with or without 0x)
    Optional:
      POLY_TEST_SLUG     = Gamma event slug (default: highest-temperature-in-london-on-february-15-2026)
      POLY_TEST_OUTCOME = Option label, e.g. "12°C or higher" (must match event's groupItemTitle)
      POLY_TEST_SIDE    = YES or NO (default: NO)
      POLY_TEST_PRICE   = Limit price (default 0.01 = place-only, won't fill). Use 'auto' for best ask if you want the order to potentially fill.
      POLY_CHAIN_ID      = 137 (default)
      POLY_SIGNATURE_TYPE = 0 (EOA) | 1 (Magic/email) | 2 (browser wallet on Polymarket.com)
      POLY_FUNDER        = proxy wallet address (for type 1 or 2: see your Polymarket profile)
  - EOA/MetaMask: set USDC and Conditional Token allowances once (see Polymarket docs).
  - Magic/email wallet: no extra step.

Run:
  python place_one_real_order.py
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import aiohttp

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Load .env from project root
_root = Path(__file__).resolve().parent
if load_dotenv is not None:
    load_dotenv(_root / ".env")

HOST = "https://clob.polymarket.com"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
CHAIN_ID = int(os.getenv("POLY_CHAIN_ID", "137"))
GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events/slug/{slug}"


# For this test: London Feb 15 2026 temperature (override with env)
TEST_SLUG = os.getenv("POLY_TEST_SLUG", "highest-temperature-in-london-on-february-15-2026")
# Which outcome to buy: option label must match Gamma groupItemTitle exactly (e.g. "12°C or higher")
TEST_OUTCOME = os.getenv("POLY_TEST_OUTCOME", "12°C or higher")
TEST_SIDE = os.getenv("POLY_TEST_SIDE", "NO").upper()


async def fetch_token_for_event_outcome(
    slug: str, outcome_label: str, side: str
) -> tuple[str | None, str, float, bool]:
    """
    Fetch event by Gamma slug; find market whose groupItemTitle matches outcome_label.
    Return (token_id, market_question, tick_size, neg_risk). side "YES" -> clobTokenIds[0], "NO" -> clobTokenIds[1].
    """
    url = GAMMA_EVENT_URL.format(slug=slug)
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None, "", 0.01, True
            event = await resp.json()
    markets = event.get("markets") or []
    outcome_clean = outcome_label.strip()
    token_idx = 0 if side == "YES" else 1
    for market in markets:
        title = (market.get("groupItemTitle") or market.get("question") or "").strip()
        if not title:
            continue
        if title == outcome_clean or outcome_clean.lower() in title.lower():
            raw = market.get("clobTokenIds")
            if not raw:
                continue
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(raw, list) or len(raw) < 2:
                continue
            token_id = str(raw[token_idx])
            if len(token_id) < 20:
                continue
            question = market.get("question") or title
            tick_size = float(market.get("orderPriceMinTickSize", 0.01))
            neg_risk = bool(market.get("negRisk", True))
            return token_id, question, tick_size, neg_risk
    return None, "", 0.01, True


async def fetch_best_ask(token_id: str) -> float | None:
    """Fetch CLOB order book; return best ask (min of asks) for a BUY, or None."""
    url = f"{CLOB_BOOK_URL}?token_id={token_id}"
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    asks = data.get("asks") or []
    if not asks:
        return None
    prices = []
    for level in asks:
        try:
            p = level.get("price")
            prices.append(float(p) if p is not None else None)
        except (TypeError, ValueError):
            continue
    prices = [p for p in prices if p is not None and p > 0]
    return min(prices) if prices else None


def main() -> None:
    pk = (os.getenv("POLY_PRIVATE_KEY") or os.getenv("PRIVATE_KEY") or "").strip()
    if not pk:
        print("ERROR: Set POLY_PRIVATE_KEY or PRIVATE_KEY in .env (wallet private key, hex).")
        return
    if pk.startswith("0x"):
        pk = pk[2:]
    sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
    funder = (os.getenv("POLY_FUNDER") or "").strip() or None

    # Sanity check: proxy wallets (Magic=1, browser=2) need funder set
    if sig_type in (1, 2) and not funder:
        print("ERROR: POLY_FUNDER must be set for Magic (1) or browser wallet (2). Use your proxy address from Polymarket profile.")
        return
    if funder and len(funder) > 18:
        print(f"Using signature_type={sig_type}, funder={funder[:10]}...{funder[-6:]}")
    else:
        print(f"Using signature_type={sig_type}, funder={funder or '(signer address)'}")

    print(f"Fetching token: event={TEST_SLUG}, outcome='{TEST_OUTCOME}', side={TEST_SIDE}...")
    token_id, market_name, tick_size, neg_risk = asyncio.run(
        fetch_token_for_event_outcome(TEST_SLUG, TEST_OUTCOME, TEST_SIDE)
    )
    if not token_id:
        print(
            f"ERROR: No market found for outcome '{TEST_OUTCOME}' (side={TEST_SIDE}) in event '{TEST_SLUG}'."
        )
        return
    print(f"  Token: {token_id[:24]}...  Market: {market_name[:60]}")

    # Price: default 0.01 for safe "place only" test (order rests on book, won't fill). Set POLY_TEST_PRICE to use another price, or POLY_TEST_PRICE=auto for best ask.
    price_str = os.getenv("POLY_TEST_PRICE", "0.01").strip().lower()
    if price_str == "auto":
        print("Fetching current best ask...")
        price = asyncio.run(fetch_best_ask(token_id))
        if price is None:
            print("ERROR: No ask in book.")
            return
        print(f"  Best ask: {price}")
    else:
        try:
            price = float(price_str)
        except ValueError:
            print("ERROR: POLY_TEST_PRICE must be a number or 'auto'.")
            return
        print(f"  Limit price: {price} (place-only; order will rest on book)")

    size = 5.0
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import BUY

    client = ClobClient(
        HOST,
        key=pk,
        chain_id=CHAIN_ID,
        signature_type=sig_type,
        funder=funder,
    )
    print("Deriving API creds (L1 auth)...")
    client.set_api_creds(client.create_or_derive_api_creds())

    # Use tick_size and neg_risk from Gamma (CLOB get_market expects condition_id, not token_id, so we use Gamma data)
    options = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)

    order_args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
    print(f"Creating and posting order: BUY {size} @ {price} ({TEST_SIDE} on '{TEST_OUTCOME}')...")
    resp = client.create_and_post_order(order_args, options=options)
    print("Response:", resp)
    if isinstance(resp, dict) and resp.get("orderID") or getattr(resp, "order_id", None):
        print("  -> Order placed. You can cancel it on Polymarket if you want.")
    else:
        print("  -> Check response for errors (e.g. allowances, balance, or API message).")


if __name__ == "__main__":
    main()
