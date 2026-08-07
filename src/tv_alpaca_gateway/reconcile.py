from __future__ import annotations

from dataclasses import dataclass

from .broker import BrokerResult
from .store import EventStore


@dataclass(frozen=True)
class Reconciliation:
    event_id: str
    order_id: str
    status: str


def reconcile(event_id: str, order_id: str, broker, store: EventStore) -> Reconciliation:
    result: BrokerResult = broker.get_order(order_id)
    store.update(event_id, f"broker_{result.status}", result.order_id)
    return Reconciliation(event_id, result.order_id, result.status)
