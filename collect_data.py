"""
Standalone 2-hour data collector for BTC 5-min Up/Down markets.

Collects orderbook snapshots for BOTH YES and NO tokens every ~5 seconds
across 24 consecutive 5-minute windows. Saves everything to market_data.json
incrementally (crash-safe).

Usage:  python collect_data.py
        (runs for ~2 hours, then exits)

Data collected per snapshot:
  - YES token: best bid/ask/mid/spread, depth, best-level sizes, book imbalance
  - NO token:  best bid/ask/mid/spread, depth, best-level sizes, book imbalance
  - Raw bid/ask arrays for both tokens (for deep analysis)
  - Recent trades if API supports it

After each window closes, the resolved outcome is fetched and attached.
"""

import asyncio
import aiohttp
import json
import os
import sys
import time
from datetime import datetime, timezone

# ── Config ──
WINDOW_SEC = 300          # 5 minutes
POLL_INTERVAL = 3         # seconds between snapshots (high-frequency for MM simulation)
NUM_WINDOWS = 12          # 1 hour = 12 x 5-minute windows
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_data_hf.json")
OUTCOME_POLL_DELAY = 15   # seconds to wait after window ends before checking outcome
OUTCOME_POLL_RETRIES = 4  # how many times to retry fetching outcome

# ── Helpers ──

def current_boundary() -> int:
    return (int(time.time()) // 300) * 300


def slug_for(boundary: int) -> str:
    return f"btc-updown-5m-{boundary}"


def sec_into_window() -> int:
    now = int(time.time())
    return now - current_boundary()


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ── API Calls ──

async def fetch_event(session: aiohttp.ClientSession, slug: str) -> dict | None:
    """Fetch event metadata from Gamma API."""
    url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


def extract_tokens(event: dict) -> tuple[str | None, str | None, str]:
    """Extract YES token (raw[0]), NO token (raw[1]), and question from event."""
    markets = event.get("markets") or []
    if not markets:
        return None, None, ""
    m = markets[0]
    question = m.get("question") or event.get("title") or ""
    raw = m.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None, None, question
    if not isinstance(raw, list) or len(raw) < 2:
        return None, None, question
    yes_tok = str(raw[0]) if len(str(raw[0])) >= 20 else None
    no_tok = str(raw[1]) if len(str(raw[1])) >= 20 else None
    return yes_tok, no_tok, question


def extract_outcome(event: dict) -> dict:
    """Extract resolution info from event."""
    markets = event.get("markets") or []
    if not markets:
        return {"resolved": False}
    m = markets[0]
    resolved = bool(m.get("closed") or m.get("resolved"))
    outcome = m.get("outcome")
    outcome_prices = m.get("outcomePrices")
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except Exception:
            pass
    return {
        "resolved": resolved,
        "outcome": outcome,
        "outcome_prices": outcome_prices,
    }


async def fetch_full_book(session: aiohttp.ClientSession, token_id: str) -> dict:
    """
    Fetch full orderbook for a token. Returns rich dict with:
    best_bid, best_ask, mid, spread, spread_pct,
    total_bid_depth, total_ask_depth, bid_levels, ask_levels,
    best_bid_size, best_ask_size, book_imbalance,
    raw_bids, raw_asks (price/size arrays)
    """
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    empty = {
        "best_bid": 0.0, "best_ask": 0.0, "mid": 0.0,
        "spread": 0.0, "spread_pct": 0.0,
        "total_bid_depth": 0.0, "total_ask_depth": 0.0,
        "bid_levels": 0, "ask_levels": 0,
        "best_bid_size": 0.0, "best_ask_size": 0.0,
        "book_imbalance": 0.0,
        "raw_bids": [], "raw_asks": [],
        "error": None,
    }
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
            if r.status != 200:
                empty["error"] = f"HTTP {r.status}"
                return empty
            data = await r.json()
    except Exception as e:
        empty["error"] = str(e)
        return empty

    bids_raw = data.get("bids") or []
    asks_raw = data.get("asks") or []

    # Parse into (price, size) tuples
    bids = []
    for b in bids_raw:
        p = safe_float(b.get("price") if isinstance(b, dict) else None)
        s = safe_float(b.get("size") if isinstance(b, dict) else None)
        if p > 0:
            bids.append({"price": p, "size": s})
    asks = []
    for a in asks_raw:
        p = safe_float(a.get("price") if isinstance(a, dict) else None)
        s = safe_float(a.get("size") if isinstance(a, dict) else None)
        if p > 0:
            asks.append({"price": p, "size": s})

    # Sort: bids descending by price, asks ascending
    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])

    best_bid = bids[0]["price"] if bids else 0.0
    best_ask = asks[0]["price"] if asks else 0.0
    best_bid_size = bids[0]["size"] if bids else 0.0
    best_ask_size = asks[0]["size"] if asks else 0.0
    mid = (best_bid + best_ask) / 2 if (best_bid > 0 and best_ask > 0) else 0.0
    spread = (best_ask - best_bid) if (best_bid > 0 and best_ask > 0) else 0.0
    spread_pct = (spread / mid * 100) if mid > 0 else 0.0
    total_bid_depth = sum(b["size"] for b in bids)
    total_ask_depth = sum(a["size"] for a in asks)
    total_depth = total_bid_depth + total_ask_depth
    book_imbalance = (total_bid_depth - total_ask_depth) / total_depth if total_depth > 0 else 0.0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": round(spread, 6),
        "spread_pct": round(spread_pct, 2),
        "total_bid_depth": round(total_bid_depth, 2),
        "total_ask_depth": round(total_ask_depth, 2),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "best_bid_size": round(best_bid_size, 2),
        "best_ask_size": round(best_ask_size, 2),
        "book_imbalance": round(book_imbalance, 4),
        "raw_bids": bids[:10],   # top 10 levels (keep file size manageable)
        "raw_asks": asks[:10],
        "error": None,
    }


async def fetch_recent_trades(session: aiohttp.ClientSession, token_id: str) -> list:
    """Try to fetch recent trades from CLOB. Returns list of trades or empty on failure."""
    # Try the known CLOB trades endpoint
    url = f"https://clob.polymarket.com/trades?asset_id={token_id}&limit=20"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                return []
            data = await r.json()
            if isinstance(data, list):
                return data[:20]
            if isinstance(data, dict):
                return (data.get("trades") or data.get("data") or [])[:20]
            return []
    except Exception:
        return []


# ── Persistence ──

def save_data(data: dict):
    """Save data to JSON file. Handles Windows file-locking gracefully."""
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    # Try atomic rename; fall back to direct overwrite on Windows lock errors
    try:
        if os.path.exists(OUTPUT_FILE):
            os.remove(OUTPUT_FILE)
        os.rename(tmp, OUTPUT_FILE)
    except PermissionError:
        # Windows file lock — write directly instead
        import shutil
        try:
            shutil.move(tmp, OUTPUT_FILE)
        except Exception:
            # Last resort: write directly to output file
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, default=str)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


# ── Main Loop ──

async def collect_window(session: aiohttp.ClientSession, window_num: int, boundary: int) -> dict:
    """Collect all snapshots for one 5-minute window."""
    slug = slug_for(boundary)
    print(f"\n{'='*60}")
    print(f"  WINDOW {window_num}/{NUM_WINDOWS}  |  {slug}")
    print(f"  {datetime.fromtimestamp(boundary, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}")

    # Discover tokens
    event = await fetch_event(session, slug)
    if event is None:
        print(f"  [{ts()}] ERROR: Could not fetch event for {slug}")
        return {"slug": slug, "boundary": boundary, "error": "event_fetch_failed", "snapshots": []}

    yes_tok, no_tok, question = extract_tokens(event)
    if not yes_tok or not no_tok:
        print(f"  [{ts()}] ERROR: Missing token IDs for {slug}")
        return {"slug": slug, "boundary": boundary, "error": "no_tokens", "snapshots": []}

    print(f"  Question: {question[:70]}")
    print(f"  YES token: {yes_tok[:30]}...")
    print(f"  NO  token: {no_tok[:30]}...")

    snapshots = []
    snap_count = 0

    while True:
        now = int(time.time())
        sec_in = now - boundary
        if sec_in >= WINDOW_SEC:
            break
        if sec_in < 0:
            # Window hasn't started yet, wait
            await asyncio.sleep(0.5)
            continue

        # Fetch both orderbooks in parallel
        yes_book, no_book = await asyncio.gather(
            fetch_full_book(session, yes_tok),
            fetch_full_book(session, no_tok),
        )

        # Try to fetch recent trades (less frequently -- every 30s to avoid rate limits)
        yes_trades = []
        no_trades = []
        if snap_count % 6 == 0:  # every ~30s
            yes_trades, no_trades = await asyncio.gather(
                fetch_recent_trades(session, yes_tok),
                fetch_recent_trades(session, no_tok),
            )

        snap = {
            "timestamp": now,
            "timestamp_utc": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "sec_in": sec_in,
            "yes": yes_book,
            "no": no_book,
        }
        if yes_trades:
            snap["yes_trades"] = yes_trades
        if no_trades:
            snap["no_trades"] = no_trades

        snapshots.append(snap)
        snap_count += 1

        # Compact log line
        y_mid = yes_book["mid"]
        n_mid = no_book["mid"]
        y_sprd = yes_book["spread_pct"]
        y_imb = yes_book["book_imbalance"]
        y_bdep = yes_book["total_bid_depth"]
        y_adep = yes_book["total_ask_depth"]
        n_bdep = no_book["total_bid_depth"]
        n_adep = no_book["total_ask_depth"]

        zone = "WARM" if sec_in < 30 else ("WIND" if sec_in >= 270 else "ACTV")
        print(
            f"  [{ts()}] t={sec_in:3d}s {zone} | "
            f"YES mid={y_mid:.4f} sprd={y_sprd:4.1f}% imb={y_imb:+.2f} dep={y_bdep:.0f}/{y_adep:.0f} | "
            f"NO  mid={n_mid:.4f} dep={n_bdep:.0f}/{n_adep:.0f}"
        )

        # Sleep until next poll
        elapsed = int(time.time()) - now
        sleep_for = POLL_INTERVAL - elapsed
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

    print(f"  [{ts()}] Window complete: {len(snapshots)} snapshots collected")

    # Build window record
    window_data = {
        "slug": slug,
        "boundary": boundary,
        "question": question,
        "yes_token_id": yes_tok,
        "no_token_id": no_tok,
        "snapshot_count": len(snapshots),
        "snapshots": snapshots,
        "outcome": None,  # filled in later
    }

    return window_data


async def fetch_outcome_for_window(session: aiohttp.ClientSession, window_data: dict) -> dict:
    """Poll for resolved outcome after window ends."""
    slug = window_data["slug"]
    print(f"  [{ts()}] Fetching outcome for {slug}...")
    for attempt in range(OUTCOME_POLL_RETRIES):
        await asyncio.sleep(OUTCOME_POLL_DELAY)
        event = await fetch_event(session, slug)
        if event is None:
            continue
        outcome_info = extract_outcome(event)
        if outcome_info["resolved"]:
            window_data["outcome"] = outcome_info
            print(f"  [{ts()}] Outcome: {outcome_info}")
            return window_data
        print(f"  [{ts()}] Not yet resolved (attempt {attempt+1}/{OUTCOME_POLL_RETRIES})")

    # Still not resolved -- save what we have
    print(f"  [{ts()}] Could not get resolved outcome, will retry at end")
    window_data["outcome"] = {"resolved": False, "outcome": None, "outcome_prices": None}
    return window_data


async def backfill_outcomes(session: aiohttp.ClientSession, all_data: dict):
    """Re-check unresolved windows (they should be resolved by now)."""
    for w in all_data["windows"]:
        if w.get("outcome") and w["outcome"].get("resolved"):
            continue
        slug = w["slug"]
        print(f"  [{ts()}] Backfilling outcome for {slug}...")
        event = await fetch_event(session, slug)
        if event:
            w["outcome"] = extract_outcome(event)
            print(f"  [{ts()}] -> {w['outcome']}")


async def main():
    print("=" * 60)
    print("  BTC 5-MIN MARKET DATA COLLECTOR")
    print(f"  Collecting {NUM_WINDOWS} windows (~{NUM_WINDOWS * 5} minutes)")
    print(f"  Output: {OUTPUT_FILE}")
    print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    # Load existing data if resuming from crash
    all_data = {"windows": [], "collection_started": time.time(), "collection_started_utc": datetime.now(timezone.utc).isoformat()}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing_slugs = {w["slug"] for w in existing.get("windows", [])}
            all_data = existing
            print(f"  Resuming: {len(existing.get('windows', []))} windows already collected")
        except Exception:
            print("  Could not load existing data, starting fresh")
            existing_slugs = set()
    else:
        existing_slugs = set()

    async with aiohttp.ClientSession() as session:

        windows_collected = len(all_data["windows"])

        while windows_collected < NUM_WINDOWS:
            # Figure out which boundary we're in
            boundary = current_boundary()
            slug = slug_for(boundary)

            # If we already have this window, wait for next
            if slug in existing_slugs:
                next_boundary = boundary + 300
                wait = next_boundary - int(time.time()) + 1
                if wait > 0:
                    print(f"\n  [{ts()}] Already have {slug}, waiting {wait}s for next window...")
                    await asyncio.sleep(wait)
                continue

            # Wait for window to start if we're between windows
            sec_in = sec_into_window()
            if sec_in < 2:
                # Give Gamma API a moment to create the event
                print(f"\n  [{ts()}] Window just started, waiting 3s for event to propagate...")
                await asyncio.sleep(3)

            # Collect this window
            window_data = await collect_window(session, windows_collected + 1, boundary)

            # Fetch outcome (quick poll)
            if window_data.get("snapshots"):
                await fetch_outcome_for_window(session, window_data)

            all_data["windows"].append(window_data)
            existing_slugs.add(slug)
            windows_collected += 1

            # Save incrementally
            all_data["collection_updated"] = time.time()
            all_data["collection_updated_utc"] = datetime.now(timezone.utc).isoformat()
            all_data["windows_collected"] = windows_collected
            save_data(all_data)
            print(f"  [{ts()}] Saved to {OUTPUT_FILE} ({windows_collected}/{NUM_WINDOWS} windows)")

        # Final backfill of any unresolved outcomes
        print(f"\n  [{ts()}] Backfilling any unresolved outcomes...")
        await backfill_outcomes(session, all_data)

        all_data["collection_finished"] = time.time()
        all_data["collection_finished_utc"] = datetime.now(timezone.utc).isoformat()
        save_data(all_data)

    print(f"\n{'='*60}")
    print(f"  COLLECTION COMPLETE")
    print(f"  {windows_collected} windows saved to {OUTPUT_FILE}")
    total_snaps = sum(w.get("snapshot_count", 0) for w in all_data["windows"])
    print(f"  Total snapshots: {total_snaps}")
    resolved = sum(1 for w in all_data["windows"] if w.get("outcome", {}).get("resolved"))
    print(f"  Resolved outcomes: {resolved}/{windows_collected}")
    print(f"{'='*60}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n  [{ts()}] Interrupted by user. Data saved so far is in {OUTPUT_FILE}")
        sys.exit(0)

    # ── Auto-run analysis after collection ──
    print(f"\n{'='*60}")
    print(f"  LAUNCHING STRATEGY ANALYSIS AUTOMATICALLY...")
    print(f"{'='*60}\n")
    try:
        from analyze_strategies import load_data, analyze_all
        data = load_data(OUTPUT_FILE)
        print(f"Loaded {len(data.get('windows', []))} windows from {OUTPUT_FILE}")
        analyze_all(data)
    except Exception as e:
        print(f"\n  Auto-analysis failed: {e}")
        print(f"  You can run it manually:  python analyze_strategies.py")
        import traceback
        traceback.print_exc()
