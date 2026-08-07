from __future__ import annotations

import sqlite3
from pathlib import Path


class EventStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', broker_order_id TEXT)"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if "broker_order_id" not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN broker_order_id TEXT")

    def claim(self, event_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events(event_id, status) VALUES (?, 'claimed')",
                (event_id,),
            )
            return cur.rowcount == 1

    def update(self, event_id: str, status: str, detail: str = "", broker_order_id: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE events SET status = ?, detail = ?, broker_order_id = COALESCE(?, broker_order_id) WHERE event_id = ?",
                (status, detail[:2000], broker_order_id, event_id),
            )

    def update_by_order_id(self, order_id: str, status: str, detail: str = "") -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE events SET status = ?, detail = ? WHERE broker_order_id = ?",
                (status, detail[:2000], order_id),
            )
            return cur.rowcount == 1

    def release(self, event_id: str) -> bool:
        """Release an event after submission failure so the same alert can retry."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM events WHERE event_id = ? AND status IN ('claimed', 'failed', 'market_data_failed') AND broker_order_id IS NULL",
                (event_id,),
            )
            return cur.rowcount == 1

    # Statuses that mean the broker is finished with the order. Anything else
    # is still live as far as we know, and is what has to be re-checked after a
    # stream outage.
    TERMINAL = (
        "broker_filled", "broker_canceled", "broker_rejected",
        "broker_expired", "broker_done_for_day",
    )

    def unresolved_broker_orders(self) -> list[str]:
        """Broker order ids whose last known status is not terminal.

        Alpaca does not replay trade_updates missed while the socket was down,
        so after a reconnect these are exactly the orders whose state we may be
        wrong about. Being wrong here means believing a position is flat when it
        filled — so the list is deliberately generous: an order re-checked
        needlessly costs one REST call, one missed costs a position.
        """
        placeholders = ",".join("?" for _ in self.TERMINAL)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT broker_order_id FROM events "
                f"WHERE broker_order_id IS NOT NULL AND broker_order_id != '' "
                f"AND status NOT IN ({placeholders})",
                self.TERMINAL,
            ).fetchall()
        return [row[0] for row in rows]

    def status(self, event_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return row[0] if row else None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)
