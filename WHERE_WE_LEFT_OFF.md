# Polybot – Where We Left Off

**Last updated:** Feb 14, 2026

## Project state

- **Bot:** Paper trading on **Bitcoin Up or Down 5 min** (Polymarket). Uses current 5m window, switches token every 5 min.
- **Discovery:** Gamma API by slug `btc-updown-5m-{unix_5m_boundary}`. CLOB book for prices (best bid = max of bids, best ask = min of asks).
- **Strategy:** Quote at best bid / best ask (paper mode). Relaxed fill: within 0.005 of limit = filled. First 200 cycles force-fill for testing (up to 400 trades).
- **Dashboard:** Streamlit reads `trades.csv` (same path as bot). Auto-refresh every 2s.

## How to run tomorrow

1. **Start the bot**
   ```bash
   cd c:\Users\Charl\Desktop\Cursor\polybot
   python main_amm.py
   ```
   Uses `config.json` (`"token_id": "auto"` = BTC 5m mode).

2. **Start the dashboard** (optional, in a second terminal)
   ```bash
   cd c:\Users\Charl\Desktop\Cursor\polybot
   python -m streamlit run dashboard.py
   ```
   Then open http://localhost:8501

3. **Debug script** (live CLOB book for current 5m window)
   ```bash
   python debug_btc_5m_live.py --live
   ```

4. **Place one real order** (verify live API + EIP-712 signing)
   ```bash
   pip install py-clob-client
   # Set in .env: POLY_PRIVATE_KEY=your_hex_private_key
   # Optional: POLY_SIGNATURE_TYPE=0 (EOA) or 1 (Magic), POLY_FUNDER=... for proxy
   python place_one_real_order.py
   ```
   Places a small BUY (5 @ 0.01) on the current BTC 5m market so it rests on the book. Cancel on Polymarket if desired. EOA/MetaMask users must set token allowances first (see Polymarket docs).

## Key files

| File | Role |
|------|------|
| `main_amm.py` | Main loop, BTC 5m slug/token, CLOB poll, run_cycle, force_fill_debug (first 200 cycles) |
| `execution.py` | simulate_place_order (relaxed fill 0.005, force_fill_debug), trades.csv path, P/L print on fill |
| `strategy.py` | get_bid_ask, get_mid_price (used when not quoting at book) |
| `dashboard.py` | Streamlit: PnL, win rate, trade table from trades.csv |
| `config.json` | token_id, spread_high_vol_pct, PAPER_TRADING, etc. |
| `trades.csv` | Written by bot on virtual fills; dashboard reads it |

## If you want to start completely fresh

- Reset trades: overwrite `trades.csv` with just the header row (or delete it; bot recreates it on first fill).
- Turn off force-fill after testing: in `main_amm.py` change `cycle_count < 200` to `cycle_count < 0` (or remove the force_fill_debug logic).

All edits from this session are already saved in these files in `c:\Users\Charl\Desktop\Cursor\polybot`. In Cursor, use **File > Save All** (or Ctrl+K S) to ensure nothing is left unsaved.
