"""
Collect 1 hour of orderbook data for a single daily market (e.g. Bitcoin Up or Down on February 16).

Same snapshot shape as the 5-min collector: YES/NO best_bid, best_ask, mid, spread,
depth, book_imbalance, raw top levels. Use the output for simulations (simulate_mm.py style).

Usage:  python collect_data_daily.py
        (runs 1 hour, saves to market_data_daily.json)

Config: set market_slug in config.json or leave default.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

# ── Config ──
DEFAULT_SLUG = "bitcoin-up-or-down-on-february-16"
DURATION_SEC = 3600       # 1 hour
POLL_INTERVAL = 3         # seconds between snapshots
OUTPUT_FILE = Path(__file__).resolve().parent / "market_data_daily.json"
SAVE_EVERY_N = 30         # incremental save every N snapshots (crash-safe)


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


async def fetch_event(session: aiohttp.ClientSession, slug: str) -> dict | None:
    url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


def extract_tokens_and_end(event: dict) -> tuple[str | None, str | None, str, str]:
    """Return (yes_token_id, no_token_id, question, end_date_iso)."""
    markets = event.get("markets") or []
    if not markets:
        return None, None, "", ""
    m = markets[0]
    question = m.get("question") or event.get("title") or ""
    end_iso = str(m.get("endDate") or m.get("endDateIso") or "")
    raw = m.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None, None, question, end_iso
    if not isinstance(raw, list) or len(raw) < 2:
        return None, None, question, end_iso
    yes_tok = str(raw[0]) if len(str(raw[0])) >= 20 else None
    no_tok = str(raw[1]) if len(str(raw[1])) >= 20 else None
    return yes_tok, no_tok, question, end_iso


async def fetch_full_book(session: aiohttp.ClientSession, token_id: str) -> dict:
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
        "raw_bids": bids[:10],
        "raw_asks": asks[:10],
        "error": None,
    }


def save_data(data: dict) -> None:
    tmp = str(OUTPUT_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    try:
        if os.path.exists(OUTPUT_FILE):
            os.remove(OUTPUT_FILE)
        os.rename(tmp, str(OUTPUT_FILE))
    except PermissionError:
        import shutil
        try:
            shutil.move(tmp, str(OUTPUT_FILE))
        except Exception:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, default=str)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


async def main() -> None:
    # Load slug from config or use default
    config_path = Path(__file__).resolve().parent / "config.json"
    slug = DEFAULT_SLUG
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
                if cfg.get("market_slug"):
                    slug = (cfg["market_slug"] or "").strip() or slug
        except Exception:
            pass

    print("=" * 60)
    print("  DAILY MARKET DATA COLLECTOR")
    print(f"  Slug: {slug}")
    print(f"  Duration: {DURATION_SEC}s ({DURATION_SEC // 60} min)")
    print(f"  Poll interval: {POLL_INTERVAL}s")
    print(f"  Output: {OUTPUT_FILE.name}")
    print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    async with aiohttp.ClientSession() as session:
        event = await fetch_event(session, slug)
        if event is None:
            print(f"  [{ts()}] ERROR: Could not fetch event for {slug}")
            sys.exit(1)

        yes_tok, no_tok, question, end_iso = extract_tokens_and_end(event)
        if not yes_tok or not no_tok:
            print(f"  [{ts()}] ERROR: Missing token IDs")
            sys.exit(1)

        print(f"  Question: {question[:70]}")
        print(f"  Resolution: {end_iso or 'n/a'}")
        print(f"  YES token: {yes_tok[:28]}...")
        print(f"  NO  token: {no_tok[:28]}...")
        print()

        resolution_ts: float | None = None
        if end_iso:
            try:
                resolution_ts = datetime.fromisoformat(
                    end_iso.replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, TypeError):
                pass

        data = {
            "slug": slug,
            "question": question,
            "yes_token_id": yes_tok,
            "no_token_id": no_tok,
            "resolution_iso": end_iso,
            "collection_started": time.time(),
            "collection_started_utc": datetime.now(timezone.utc).isoformat(),
            "duration_sec": DURATION_SEC,
            "poll_interval": POLL_INTERVAL,
            "snapshots": [],
        }

        start = time.time()
        snap_count = 0

        while True:
            elapsed = time.time() - start
            if elapsed >= DURATION_SEC:
                break

            now = int(time.time())
            yes_book, no_book = await asyncio.gather(
                fetch_full_book(session, yes_tok),
                fetch_full_book(session, no_tok),
            )

            sec_until_res = None
            if resolution_ts is not None:
                sec_until_res = int(resolution_ts - now)

            snap = {
                "timestamp": now,
                "timestamp_utc": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "sec_elapsed": int(elapsed),
                "sec_until_resolution": sec_until_res,
                "yes": yes_book,
                "no": no_book,
            }
            data["snapshots"].append(snap)
            snap_count += 1

            y_mid = yes_book["mid"]
            n_mid = no_book["mid"]
            y_sprd = yes_book["spread_pct"]
            y_imb = yes_book["book_imbalance"]
            y_bdep = yes_book["total_bid_depth"]
            y_adep = yes_book["total_ask_depth"]
            n_bdep = no_book["total_bid_depth"]
            n_adep = no_book["total_ask_depth"]
            print(
                f"  [{ts()}] t={int(elapsed):4d}s | "
                f"YES mid={y_mid:.4f} sprd={y_sprd:4.1f}% imb={y_imb:+.2f} dep={y_bdep:.0f}/{y_adep:.0f} | "
                f"NO mid={n_mid:.4f} dep={n_bdep:.0f}/{n_adep:.0f}"
            )

            if snap_count % SAVE_EVERY_N == 0:
                data["collection_updated"] = time.time()
                data["collection_updated_utc"] = datetime.now(timezone.utc).isoformat()
                data["snapshot_count"] = len(data["snapshots"])
                save_data(data)
                print(f"  [{ts()}] Saved {len(data['snapshots'])} snapshots")

            sleep_for = POLL_INTERVAL - (time.time() - now)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

        data["collection_ended"] = time.time()
        data["collection_ended_utc"] = datetime.now(timezone.utc).isoformat()
        data["snapshot_count"] = len(data["snapshots"])
        save_data(data)

    print()
    print("=" * 60)
    print("  COLLECTION COMPLETE")
    print(f"  {len(data['snapshots'])} snapshots saved to {OUTPUT_FILE.name}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n  [{ts()}] Interrupted. Data saved so far is in {OUTPUT_FILE.name}")
        sys.exit(0)
