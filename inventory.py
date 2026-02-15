"""
Inventory management: position tracking, delta limit (±500 USDC), 5% hard stop loss.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Default limits (can be overridden via config)
INVENTORY_DELTA_LIMIT_USDC = 500.0
HARD_STOP_LOSS_PCT = 5.0
VIRTUAL_WALLET_START_USDC = 1000.0


@dataclass
class Position:
    """Single leg position (e.g. YES or NO token)."""
    size: float = 0.0   # positive = long
    cost_usdc: float = 0.0


@dataclass
class InventoryState:
    """Aggregate inventory state. Paper trading uses virtual_balance_usdc (start 1000 USDC)."""
    positions: dict[str, Position] = field(default_factory=dict)
    starting_equity_usdc: float = 0.0
    realized_pnl_usdc: float = 0.0
    delta_limit_usdc: float = INVENTORY_DELTA_LIMIT_USDC
    stop_loss_pct: float = HARD_STOP_LOSS_PCT
    virtual_balance_usdc: float = VIRTUAL_WALLET_START_USDC

    def get_net_position_usdc(self, mark_prices: dict[str, float] | None = None) -> float:
        """Net position value in USDC (positive = net long)."""
        total = 0.0
        for token_id, pos in self.positions.items():
            mark = (mark_prices or {}).get(token_id, 0.5)
            total += pos.size * mark
        return total

    def get_cost_basis_usdc(self) -> float:
        """Total cost basis in USDC."""
        return sum(p.cost_usdc for p in self.positions.values())

    def update_position(
        self,
        token_id: str,
        side: Literal["BUY", "SELL"],
        size: float,
        price_usdc: float,
    ) -> None:
        """Update position after a fill."""
        if token_id not in self.positions:
            self.positions[token_id] = Position()
        pos = self.positions[token_id]
        notional = size * price_usdc
        if side == "BUY":
            pos.size += size
            pos.cost_usdc += notional
        else:
            # Realized PnL on sale: proceeds - cost of sold portion (proportional)
            cost_sold = (pos.cost_usdc / pos.size * size) if pos.size else 0.0
            pos.cost_usdc -= cost_sold
            pos.size -= size
            self.realized_pnl_usdc += notional - cost_sold

    def is_within_delta_limit(
        self,
        additional_usdc: float,
        mark_prices: dict[str, float] | None = None,
    ) -> bool:
        """True if adding additional_usdc to net position keeps us within ±delta_limit_usdc."""
        current = self.get_net_position_usdc(mark_prices)
        new_net = current + additional_usdc
        return abs(new_net) <= self.delta_limit_usdc

    def get_pnl_pct(self, mark_prices: dict[str, float] | None = None) -> float:
        """Realized + unrealized PnL as % of starting equity."""
        if self.starting_equity_usdc <= 0:
            return 0.0
        unrealized = self.get_net_position_usdc(mark_prices) - self.get_cost_basis_usdc()
        total_pnl = self.realized_pnl_usdc + unrealized
        return 100.0 * total_pnl / self.starting_equity_usdc

    def is_stop_loss_hit(self, mark_prices: dict[str, float] | None = None) -> bool:
        """True if total PnL <= -stop_loss_pct% of starting equity."""
        return self.get_pnl_pct(mark_prices) <= -self.stop_loss_pct

    def simulate_fill(
        self,
        token_id: str,
        side: Literal["BUY", "SELL"],
        size: float,
        price_usdc: float,
    ) -> tuple[bool, float]:
        """
        Apply a simulated fill: update position and virtual wallet.
        BUY: deduct size*price from virtual_balance. SELL: add size*price, realize PnL.
        Returns (success, realized_pnl_delta). realized_pnl_delta is 0 for BUY.
        """
        notional = size * price_usdc
        if side == "BUY":
            if self.virtual_balance_usdc < notional:
                return False, 0.0
            self.virtual_balance_usdc -= notional
            self.update_position(token_id, "BUY", size, price_usdc)
            return True, 0.0
        # SELL: require enough position
        pos = self.positions.get(token_id)
        if not pos or pos.size < size:
            return False, 0.0
        pnl_before = self.realized_pnl_usdc
        self.virtual_balance_usdc += notional
        self.update_position(token_id, "SELL", size, price_usdc)
        return True, self.realized_pnl_usdc - pnl_before
