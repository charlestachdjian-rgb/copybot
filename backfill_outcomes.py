"""Backfill resolved outcomes for market_data_hf.json windows."""
import asyncio, aiohttp, json, os, sys, time
from datetime import datetime, timezone

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_data_hf.json")

async def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    wins = data.get("windows", [])
    print(f"Loaded {len(wins)} windows")

    async with aiohttp.ClientSession() as session:
        for w in wins:
            slug = w.get("slug", "?")
            outcome = w.get("outcome")
            if outcome and outcome.get("resolved"):
                print(f"  {slug}: already resolved")
                continue

            url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        print(f"  {slug}: HTTP {r.status}")
                        continue
                    event = await r.json()
            except Exception as e:
                print(f"  {slug}: error {e}")
                continue

            markets = event.get("markets") or []
            if not markets:
                print(f"  {slug}: no markets")
                continue
            m = markets[0]
            resolved = bool(m.get("closed") or m.get("resolved"))
            outcome_prices = m.get("outcomePrices")
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    pass
            w["outcome"] = {
                "resolved": resolved,
                "outcome": m.get("outcome"),
                "outcome_prices": outcome_prices,
            }
            label = "Up" if outcome_prices == ["1", "0"] else ("Down" if outcome_prices == ["0", "1"] else "?")
            print(f"  {slug}: resolved={resolved} -> {label}")
            await asyncio.sleep(0.3)

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, default=str)
    resolved_count = sum(1 for w in wins if w.get("outcome", {}).get("resolved"))
    print(f"\nDone. {resolved_count}/{len(wins)} resolved. Saved to {DATA_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
