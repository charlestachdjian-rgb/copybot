"""
Streamlit dashboard: Realized PnL, Win Rate, Live Trade Table from trades.csv.
Auto-refreshes every 2 seconds.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

TRADES_CSV = Path(__file__).resolve().parent / "trades.csv"
AUTOREFRESH_INTERVAL_MS = 2000


def load_trades() -> pd.DataFrame:
    """Load trades from trades.csv; return empty DataFrame if missing."""
    if not TRADES_CSV.exists():
        return pd.DataFrame(columns=["timestamp", "side", "price", "size", "token_id", "realized_pnl"])
    try:
        df = pd.read_csv(TRADES_CSV)
        if "realized_pnl" not in df.columns:
            df["realized_pnl"] = 0.0
        return df
    except Exception:
        return pd.DataFrame(columns=["timestamp", "side", "price", "size", "token_id", "realized_pnl"])


def main() -> None:
    st.set_page_config(page_title="Polybot Dashboard", layout="wide")
    st_autorefresh(interval=AUTOREFRESH_INTERVAL_MS, limit=None, key="polybot_refresh")
    st.title("Polybot Market Making Dashboard")

    df = load_trades()

    col1, col2, col3 = st.columns(3)
    with col1:
        realized_pnl = df["realized_pnl"].sum() if "realized_pnl" in df.columns and len(df) else 0.0
        st.metric("Realized PnL (USDC)", f"{realized_pnl:,.2f}")
    with col2:
        if len(df) and "realized_pnl" in df.columns:
            wins = (df["realized_pnl"] > 0).sum()
            win_rate = 100.0 * wins / len(df)
        else:
            win_rate = 0.0
        st.metric("Win Rate (%)", f"{win_rate:.1f}%")
    with col3:
        st.metric("Total Trades", len(df))

    st.subheader("Live Trade Table")
    if df.empty:
        st.info(
            "No fills yet. The bot is placing paper orders every cycle; a row appears here only when "
            "the order book **crosses your limit price** (virtual fill). If the market never trades "
            "through your quote, this table stays empty. Check the bot terminal for 'Virtual Trade FILLED'."
        )
        st.caption(f"Reading from: `{TRADES_CSV}` — make sure the bot is run from the same project folder.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.caption("Data from trades.csv — auto-refresh every 2 seconds.")


if __name__ == "__main__":
    main()
