from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class ExitPlan:
    symbol: str
    take_profits: tuple[tuple[Decimal, int], ...]
    trail_percent: Decimal

    def validate(self) -> None:
        if not self.symbol or not self.take_profits:
            raise ValueError("symbol and at least one take-profit are required")
        if self.trail_percent <= 0:
            raise ValueError("trail_percent must be positive")
        for price, qty in self.take_profits:
            if price <= 0 or qty <= 0:
                raise ValueError("take-profit prices and quantities must be positive")


@dataclass(frozen=True)
class FillEvent:
    order_id: str
    filled_qty: int


@dataclass
class ManagerState:
    remaining_qty: int
    take_profit_order_ids: list[str]
    trailing_stop_order_id: str
    processed_fills: set[str] = field(default_factory=set)


class ExitManager:
    """Deterministic coordinator for split take-profits plus one trailing stop.

    The broker object must expose submit_limit, submit_trailing_stop, cancel,
    and replace_qty. This core deliberately has no network or broker credentials.
    """

    def __init__(self, broker, plan: ExitPlan):
        plan.validate()
        self.broker = broker
        self.plan = plan
        self.state: ManagerState | None = None

    def start(self, position_qty: int) -> ManagerState:
        if position_qty <= 0:
            raise ValueError("position_qty must be positive")
        tp_qty = sum(qty for _, qty in self.plan.take_profits)
        if tp_qty > position_qty:
            raise ValueError("take-profit quantities exceed position quantity")

        tp_ids: list[str] = []
        for index, (price, qty) in enumerate(self.plan.take_profits, start=1):
            tp_ids.append(
                self.broker.submit_limit(
                    self.plan.symbol,
                    "sell",
                    qty,
                    price,
                    f"exit-tp-{index}",
                )
            )
        trail_qty = position_qty - tp_qty
        if trail_qty <= 0:
            raise ValueError("trailing stop requires quantity after take-profit allocation")
        trail_id = self.broker.submit_trailing_stop(
            self.plan.symbol,
            "sell",
            trail_qty,
            self.plan.trail_percent,
            "exit-trail",
        )
        self.state = ManagerState(position_qty, tp_ids, trail_id)
        return self.state

    def on_fill(self, event: FillEvent) -> ManagerState:
        if self.state is None:
            raise RuntimeError("exit manager has not been started")
        if event.filled_qty <= 0:
            raise ValueError("filled_qty must be positive")
        if event.order_id in self.state.processed_fills:
            return self.state
        if event.filled_qty > self.state.remaining_qty:
            raise ValueError("fill exceeds remaining position quantity")

        self.state.processed_fills.add(event.order_id)
        self.state.remaining_qty -= event.filled_qty
        if event.order_id == self.state.trailing_stop_order_id:
            for order_id in self.state.take_profit_order_ids:
                self.broker.cancel(order_id)
        elif event.order_id in self.state.take_profit_order_ids:
            if self.state.remaining_qty:
                self.broker.replace_qty(self.state.trailing_stop_order_id, self.state.remaining_qty)
            else:
                self.broker.cancel(self.state.trailing_stop_order_id)
        else:
            raise ValueError("unknown exit order id")
        return self.state
