"""
Scan Polymarket for markets suitable for True Market Maker (spread capture).

Criteria:
  - Binary (YES/NO) with CLOB order book
  - Spread in sweet spot: 2–10% (capturable, not crossed/illiquid)
  - Mid in 0.25–0.75 (two-sided flow; avoid 0.95/0.05)
  - Sufficient depth for our order size (~5–10)
  - Prefer longer-duration markets (resolution in days) = lower volatility proxy

Output: ranked list of markets with token IDs and metrics.
"""
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK = "https://clob.polymarket.com/book"
REQUEST_TIMEOUT = 4
MAX_MARKETS_TO_SCAN = 80
MIN_DEPTH_SUM = 50
SPREAD_PCT_MIN = 1.0
SPREAD_PCT_MAX = 25.0
MID_MIN = 0.20
MID_MAX = 0.80


def safe_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_book(data: dict) -> tuple[float, float, float, float]:
    """From CLOB book JSON return (best_bid, best_ask, best_bid_size, best_ask_size)."""
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    best_bid, best_ask = 0.0, 0.0
    best_bid_size, best_ask_size = 0.0, 0.0
    if bids:
        prices = [(safe_float(b.get("price")), safe_float(b.get("size"))) for b in bids if isinstance(b, dict)]
        prices = [(p, s) for p, s in prices if p > 0]
        if prices:
            best_bid, best_bid_size = max(prices, key=lambda x: x[0])
    if asks:
        prices = [(safe_float(a.get("price")), safe_float(a.get("size"))) for a in asks if isinstance(a, dict)]
        prices = [(p, s) for p, s in prices if p > 0]
        if prices:
            best_ask, best_ask_size = min(prices, key=lambda x: x[0])
    return best_bid, best_ask, best_bid_size, best_ask_size


@dataclass
class MarketScore:
    slug: str
    question: str
    yes_token_id: str
    no_token_id: str
    market_id: str
    tick_size: float
    neg_risk: bool
    # Book metrics (YES token)
    yes_bid: float
    yes_ask: float
    yes_mid: float
    yes_spread_pct: float
    yes_depth: float
    # Book metrics (NO token)
    no_bid: float
    no_ask: float
    no_mid: float
    no_spread_pct: float
    no_depth: float
    # Combined
    both_bids_sum: float
    end_date_iso: str = ""
    volume24hr: float = 0.0
    # Score components
    spread_score: float = 0.0
    mid_score: float = 0.0
    depth_score: float = 0.0
    duration_score: float = 0.0
    total_score: float = 0.0
    skip_reason: str = ""


async def fetch_book(session: aiohttp.ClientSession, token_id: str) -> tuple[float, float, float, float]:
    url = f"{CLOB_BOOK}?token_id={token_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as r:
            if r.status != 200:
                return 0.0, 0.0, 0.0, 0.0
            data = await r.json()
            return parse_book(data)
    except Exception:
        return 0.0, 0.0, 0.0, 0.0


def extract_tokens(m: dict) -> tuple[str | None, str | None]:
    raw = m.get("clobTokenIds")
    if raw is None:
        return None, None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None, None
    if not isinstance(raw, list) or len(raw) < 2:
        return None, None
    yes_t = str(raw[0]) if len(str(raw[0])) >= 20 else None
    no_t = str(raw[1]) if len(str(raw[1])) >= 20 else None
    return yes_t, no_t


def score_market(s: MarketScore) -> None:
    # Spread: sweet spot 2–8% gets highest score; 1–2% or 8–12% ok; >15% bad
    if SPREAD_PCT_MIN <= s.yes_spread_pct <= 8 and SPREAD_PCT_MIN <= s.no_spread_pct <= 8:
        s.spread_score = 10.0
    elif SPREAD_PCT_MIN <= s.yes_spread_pct <= SPREAD_PCT_MAX and SPREAD_PCT_MIN <= s.no_spread_pct <= SPREAD_PCT_MAX:
        s.spread_score = 5.0
    else:
        s.spread_score = 0.0

    # Mid in 0.25–0.75 = two-sided (best for MM)
    if MID_MIN <= s.yes_mid <= MID_MAX and MID_MIN <= s.no_mid <= MID_MAX:
        s.mid_score = 10.0
    elif 0.15 <= s.yes_mid <= 0.85 and 0.15 <= s.no_mid <= 0.85:
        s.mid_score = 5.0
    else:
        s.mid_score = 0.0

    # Depth: more is better (cap at 500)
    d = min(s.yes_depth + s.no_depth, 500)
    s.depth_score = min(10.0, d / 50.0)

    # Duration: end date in the future by days = less volatile proxy
    try:
        if s.end_date_iso:
            end = datetime.fromisoformat(s.end_date_iso.replace("Z", "+00:00"))
            days = (end - datetime.now(timezone.utc)).total_seconds() / 86400
            if days >= 7:
                s.duration_score = 10.0
            elif days >= 1:
                s.duration_score = 7.0
            elif days >= 0.1:
                s.duration_score = 3.0
            else:
                s.duration_score = 0.0
    except Exception:
        s.duration_score = 5.0

    s.total_score = s.spread_score + s.mid_score + s.depth_score + s.duration_score


async def main():
    print("=" * 70)
    print("  POLYMARKET SCAN — Markets suitable for True Market Maker")
    print("=" * 70)
    print(f"  Criteria: binary CLOB, spread {SPREAD_PCT_MIN}-{SPREAD_PCT_MAX}%, mid {MID_MIN}-{MID_MAX}")
    print(f"  Prefer: longer duration (days), depth >= {MIN_DEPTH_SUM}")
    print()

    async with aiohttp.ClientSession() as session:
        # Fetch active markets (volume or liquidity order)
        params = {
            "closed": "false",
            "limit": MAX_MARKETS_TO_SCAN,
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            async with session.get(GAMMA_MARKETS, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    print(f"  Gamma API error: {r.status}")
                    return
                markets = await r.json()
        except Exception as e:
            print(f"  Failed to fetch markets: {e}")
            return

        if not isinstance(markets, list):
            print("  No markets list returned")
            return

        # Filter: order book enabled, binary
        candidates = []
        for m in markets:
            if not m.get("enableOrderBook"):
                continue
            yes_t, no_t = extract_tokens(m)
            if not yes_t or not no_t:
                continue
            candidates.append((m, yes_t, no_t))
            if len(candidates) >= 60:
                break

        print(f"  Fetched {len(markets)} markets, {len(candidates)} binary CLOB candidates. Scanning books...\n")

        results: list[MarketScore] = []
        for i, (m, yes_tok, no_tok) in enumerate(candidates):
            yes_bid, yes_ask, yes_bsz, yes_asz = await fetch_book(session, yes_tok)
            no_bid, no_ask, no_bsz, no_asz = await fetch_book(session, no_tok)
            await asyncio.sleep(0.15)

            question = (m.get("question") or m.get("groupItemTitle") or "?")[:70]
            slug = m.get("slug") or m.get("conditionId") or "?"
            market_id = str(m.get("id", ""))
            tick_size = safe_float(m.get("orderPriceMinTickSize"), 0.01)
            neg_risk = bool(m.get("negRisk", True))
            end_date = m.get("endDate") or m.get("endDateIso") or ""
            vol = safe_float(m.get("volume24hr") or m.get("volumeNum"), 0)

            if yes_bid <= 0 or yes_ask <= 0 or no_bid <= 0 or no_ask <= 0:
                continue
            if yes_bid >= yes_ask or no_bid >= no_ask:
                continue

            yes_mid = (yes_bid + yes_ask) / 2
            no_mid = (no_bid + no_ask) / 2
            yes_spread_pct = (yes_ask - yes_bid) / yes_mid * 100 if yes_mid > 0 else 0
            no_spread_pct = (no_ask - no_bid) / no_mid * 100 if no_mid > 0 else 0
            yes_depth = yes_bsz + yes_asz
            no_depth = no_bsz + no_asz
            both_sum = yes_bid + no_bid

            s = MarketScore(
                slug=slug,
                question=question,
                yes_token_id=yes_tok,
                no_token_id=no_tok,
                market_id=market_id,
                tick_size=tick_size,
                neg_risk=neg_risk,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                yes_mid=yes_mid,
                yes_spread_pct=yes_spread_pct,
                yes_depth=yes_depth,
                no_bid=no_bid,
                no_ask=no_ask,
                no_mid=no_mid,
                no_spread_pct=no_spread_pct,
                no_depth=no_depth,
                both_bids_sum=both_sum,
                end_date_iso=end_date,
                volume24hr=vol,
            )

            if yes_spread_pct < SPREAD_PCT_MIN or no_spread_pct < SPREAD_PCT_MIN:
                s.skip_reason = "spread too tight"
            elif yes_spread_pct > SPREAD_PCT_MAX or no_spread_pct > SPREAD_PCT_MAX:
                s.skip_reason = "spread too wide"
            elif yes_depth + no_depth < MIN_DEPTH_SUM:
                s.skip_reason = "low depth"
            elif both_sum >= 1.0:
                s.skip_reason = "YES_bid+NO_bid >= 1 (no edge)"
            else:
                score_market(s)
                results.append(s)

            if (i + 1) % 20 == 0:
                print(f"  Scanned {i+1}/{len(candidates)} ...")

        # Sort by total score descending
        results.sort(key=lambda x: (-x.total_score, -x.volume24hr))

    # Output
    out_path = Path(__file__).resolve().parent / "market_scan_results.json"
    out_data = {
        "scan_time_utc": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "spread_pct_min": SPREAD_PCT_MIN,
            "spread_pct_max": SPREAD_PCT_MAX,
            "mid_range": [MID_MIN, MID_MAX],
            "min_depth_sum": MIN_DEPTH_SUM,
        },
        "markets": [
            {
                "slug": s.slug,
                "question": s.question,
                "yes_token_id": s.yes_token_id,
                "no_token_id": s.no_token_id,
                "market_id": s.market_id,
                "tick_size": s.tick_size,
                "neg_risk": s.neg_risk,
                "yes_mid": round(s.yes_mid, 4),
                "no_mid": round(s.no_mid, 4),
                "yes_spread_pct": round(s.yes_spread_pct, 2),
                "no_spread_pct": round(s.no_spread_pct, 2),
                "yes_depth": round(s.yes_depth, 1),
                "no_depth": round(s.no_depth, 1),
                "both_bids_sum": round(s.both_bids_sum, 4),
                "volume24hr": round(s.volume24hr, 1),
                "end_date_iso": s.end_date_iso,
                "total_score": round(s.total_score, 1),
                "spread_score": s.spread_score,
                "mid_score": s.mid_score,
                "depth_score": s.depth_score,
                "duration_score": s.duration_score,
            }
            for s in results
        ],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_data, f, indent=2, default=str)
    print(f"\n  Saved {len(results)} markets to {out_path.name}\n")

    print("=" * 70)
    print("  TOP MARKETS FOR SPREAD CAPTURE (by score)")
    print("=" * 70)
    for i, s in enumerate(results[:20], 1):
        end_short = s.end_date_iso[:10] if s.end_date_iso else "?"
        print(f"\n  #{i}  Score: {s.total_score:.1f}  (spr={s.spread_score} mid={s.mid_score} depth={s.depth_score} dur={s.duration_score})")
        print(f"      {s.question}")
        print(f"      YES mid={s.yes_mid:.3f} spread={s.yes_spread_pct:.1f}%  NO mid={s.no_mid:.3f} spread={s.no_spread_pct:.1f}%")
        print(f"      Depth: YES={s.yes_depth:.0f} NO={s.no_depth:.0f}  |  YES_bid+NO_bid={s.both_bids_sum:.4f}  |  End: {end_short}  Vol24h: {s.volume24hr:.0f}")
        print(f"      slug: {s.slug}")
        print(f"      yes_token_id: {s.yes_token_id[:24]}...")
    print("\n" + "=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
