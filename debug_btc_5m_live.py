"""
Debug: Can we see real-time bid/ask for the BTC Up or Down 5m market?
Slug format: btc-updown-5m-{unix_timestamp} (timestamp = start of 5min window in UTC).

Run: python debug_btc_5m_live.py           -> use current 5min window (live)
Run: python debug_btc_5m_live.py --slug btc-updown-5m-1771089000  -> specific window

February 14, 12:10-12:15 PM ET = btc-updown-5m-1771089000 (YES token = Up outcome)
"""
import argparse
import asyncio
import json
import time
from datetime import datetime, timezone

import aiohttp

CLOB_BOOK_URL = "https://clob.polymarket.com/book"
POLL_INTERVAL = 2.0

# Default: 12:10-12:15 PM ET (Feb 14, 2026) — use --live to resolve current window instead
DEFAULT_SLUG = "btc-updown-5m-1771089000"


def get_current_5m_slug() -> str:
    """Current UTC time rounded down to 5-min boundary -> btc-updown-5m-{ts}."""
    now = int(time.time())
    # Round down to last 5 minutes (300 seconds)
    boundary = (now // 300) * 300
    return f"btc-updown-5m-{boundary}"


async def get_yes_token_id(
    session: aiohttp.ClientSession, slug: str, silent: bool = False
) -> str | None:
    """Fetch event by slug from Gamma, return YES (Up) token_id from first market."""
    url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                if not silent:
                    print(f"Gamma error: status {resp.status}")
                return None
            event = await resp.json()
    except Exception as e:
        if not silent:
            print(f"Gamma request failed: {e}")
        return None

    markets = event.get("markets") or []
    if not markets:
        if not silent:
            print("Event has no markets")
        return None

    market = markets[0]
    raw = market.get("clobTokenIds")
    if not raw:
        if not silent:
            print("Market has no clobTokenIds")
        return None

    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list) or len(raw) < 2:
        if not silent:
            print("clobTokenIds invalid or < 2 tokens")
        return None

    yes_token_id = str(raw[0])
    if not silent:
        title = event.get("title", "?")
        print(f"Event: {title[:70]}")
        print(f"Slug:  {slug}")
        print(f"Up (YES) token_id: {yes_token_id}")
    return yes_token_id


async def fetch_book(session: aiohttp.ClientSession, token_id: str) -> dict | None:
    """GET CLOB order book; return full JSON or None."""
    url = f"{CLOB_BOOK_URL}?token_id={token_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Poll CLOB book for BTC Up/Down 5m")
    parser.add_argument(
        "--slug",
        default=None,
        help=f"Event slug (default: {DEFAULT_SLUG}). Use --live for current window.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use current 5min window (slug = btc-updown-5m-<rounded unix time>)",
    )
    args = parser.parse_args()

    if args.live:
        slug = get_current_5m_slug()
        print(f"LIVE mode: using current 5m slug = {slug}\n")
    else:
        slug = args.slug or DEFAULT_SLUG

    use_live = args.live

    async def run():
        async with aiohttp.ClientSession() as session:
            print("=== Resolving BTC Up/Down 5m market from Gamma ===\n")
            current_slug = slug
            token_id = await get_yes_token_id(session, current_slug)
            if not token_id:
                return
            print("\n=== Polling CLOB book every 2s (Ctrl+C to stop) ===\n")
            if use_live:
                print("(Live mode: will switch to new 5m token when the window changes.)\n")

            count = 0
            while True:
                # In live mode: if we crossed into a new 5min window, switch token
                if use_live:
                    now_slug = get_current_5m_slug()
                    if now_slug != current_slug:
                        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                        print(f"[{ts}] New 5m window -> switching to {now_slug}")
                        new_token = await get_yes_token_id(session, now_slug, silent=True)
                        if new_token:
                            current_slug = now_slug
                            token_id = new_token
                            print(f"[{ts}] Switched. Polling {current_slug}")
                        else:
                            print(f"[{ts}] Failed to get token for {now_slug}, keeping previous.")

                count += 1
                data = await fetch_book(session, token_id)
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                if not data:
                    print(f"[{ts}] poll #{count}: CLOB request failed or empty")
                else:
                    bids = data.get("bids") or []
                    asks = data.get("asks") or []
                    if not bids and not asks:
                        print(f"[{ts}] poll #{count}: book empty")
                    else:
                        print(f"[{ts}] poll #{count}:  {len(bids)} bids, {len(asks)} asks")

                await asyncio.sleep(POLL_INTERVAL)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
