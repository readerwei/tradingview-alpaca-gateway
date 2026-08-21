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
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pine_dry_runs (audit_id TEXT PRIMARY KEY, detail TEXT NOT NULL)"
            )
            # One row per broker order, not one per event. An event can produce
            # an entry, a protective stop and a flatten, and reconciliation has
            # to be able to find all three. Recording them in events.detail as
            # text made them unqueryable: a resync could discover the entry and
            # miss a protective order that was live at the broker.
            # Refusals live OUTSIDE the events table. `events` is the
            # idempotency namespace, and writing a refusal there claims the id
            # — which turns the kill switch into a one-shot ban: re-firing the
            # same alert after arming would be rejected as a duplicate.
            #
            # An audit record and an execution claim are different things and
            # conflating them makes one break the other.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS refusals ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, "
                "reason TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS broker_orders ("
                "order_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, "
                "role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new')"
            )
            # An open lot is a position the gateway is actively managing, so it
            # has to outlive the process. `stage` is a column rather than a key
            # inside the blob so "is this symbol busy?" is a query and not a
            # scan-and-parse — that question is asked on every entry.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lots ("
                "event_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, "
                "stage TEXT NOT NULL, state TEXT NOT NULL)"
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

    def record_pine_dry_run(self, audit_id: str, detail: str) -> None:
        """Persist a parsed Pine command outside the executable event-ID namespace."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO pine_dry_runs(audit_id, detail) VALUES (?, ?) "
                "ON CONFLICT(audit_id) DO UPDATE SET detail = excluded.detail",
                (audit_id, detail),
            )

    def pine_dry_run_status(self, audit_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT audit_id FROM pine_dry_runs WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        return "pine_dry_run" if row else None

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
            # Also update the per-order row, so a status arriving for a
            # protective order is not silently dropped for want of a matching
            # events row.
            side = conn.execute(
                "UPDATE broker_orders SET status = ? WHERE order_id = ?",
                (status.removeprefix("broker_"), order_id),
            )
            return cur.rowcount == 1 or side.rowcount == 1

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

    def record_refusal(self, event_id: str, reason: str, detail: str = "") -> None:
        """Record that an alert was refused, without claiming its identity.

        Appended rather than upserted: an alert refused three times is three
        facts, and collapsing them would hide how often the kill switch is
        catching something.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO refusals(event_id, reason, detail) VALUES (?, ?, ?)",
                (event_id, reason, detail[:2000]),
            )

    def refusals_for(self, event_id: str) -> list[tuple[str, str]]:
        with self._connect() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT reason, detail FROM refusals WHERE event_id = ? "
                "ORDER BY id", (event_id,))]

    def record_broker_order(self, order_id: str, event_id: str, role: str,
                            status: str = "new") -> None:
        """Record one broker order and what it is for.

        `role` is entry / protection / flatten. Reconciliation needs the role
        because the three have different consequences: a missed entry means a
        position nobody knows about, a missed protection means an unprotected
        one.
        """
        if not order_id:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO broker_orders(order_id, event_id, role, status) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(order_id) DO UPDATE SET "
                "status = excluded.status",
                (order_id, event_id, role, status),
            )

    def broker_orders_for(self, event_id: str) -> list[tuple[str, str, str]]:
        """(order_id, role, status) for one event, entry and protection alike."""
        with self._connect() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT order_id, role, status FROM broker_orders "
                "WHERE event_id = ? ORDER BY rowid", (event_id,))]

    def broker_order_for_event(self, event_id: str, role: str = "entry") -> str | None:
        """Return the recorded broker order for an event and role."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT order_id FROM broker_orders WHERE event_id = ? AND role = ? "
                "ORDER BY rowid LIMIT 1", (event_id, role)
            ).fetchone()
        return str(row[0]) if row else None

    def unresolved_broker_orders(self) -> list[str]:
        """Broker order ids whose last known status is not terminal.

        Alpaca does not replay trade_updates missed while the socket was down,
        so after a reconnect these are exactly the orders whose state we may be
        wrong about. Being wrong here means believing a position is flat when it
        filled — so the list is deliberately generous: an order re-checked
        needlessly costs one REST call, one missed costs a position.
        """
        placeholders = ",".join("?" for _ in self.TERMINAL)
        terminal_bare = tuple(t.removeprefix("broker_") for t in self.TERMINAL)
        bare_marks = ",".join("?" for _ in terminal_bare)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT broker_order_id FROM events "
                f"WHERE broker_order_id IS NOT NULL AND broker_order_id != '' "
                f"AND status NOT IN ({placeholders})",
                self.TERMINAL,
            ).fetchall()
            # Protective and flatten orders live here, and a resync that only
            # looked at events would never see them.
            rows += conn.execute(
                f"SELECT order_id FROM broker_orders "
                f"WHERE status NOT IN ({bare_marks})", terminal_bare,
            ).fetchall()
        seen = dict.fromkeys(row[0] for row in rows if row[0])
        return list(seen)

    def filled_without_protection(self) -> list[str]:
        """Events whose ENTRY filled and for which nothing was ever placed.

        The state a crash between fill and protection leaves behind, and until
        this query existed nothing looked for it. Three mechanisms each decline
        to help:

          * `_open_managed_lot` writes the lot row AFTER protection succeeds,
            so there is no lot for `supervisor.start()` to re-arm;
          * `broker_filled` is TERMINAL, so `unresolved_broker_orders` — the
            reconnect resync — deliberately skips this exact order;
          * re-firing the alert is refused as a duplicate, so the operator's
            obvious remedy silently does nothing.

        A `flatten` counts as resolved: the position is gone, and there is
        nothing left to protect. Only an entry with neither a protective order
        nor a flatten is reported.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT entry.event_id FROM broker_orders AS entry "
                "WHERE entry.role = 'entry' "
                "  AND entry.status IN ('filled', 'partially_filled') "
                "  AND NOT EXISTS ("
                "        SELECT 1 FROM broker_orders AS resolved "
                "        WHERE resolved.event_id = entry.event_id "
                "          AND resolved.role IN ('protection', 'flatten'))"
            ).fetchall()
        return [row[0] for row in rows]

    def save_lot(self, event_id: str, symbol: str, stage: str, state: str) -> None:
        """Upsert one managed lot. Called after every state change, because the
        window between deciding and persisting is the window a crash loses."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO lots(event_id, symbol, stage, state) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO UPDATE SET stage = excluded.stage, "
                "state = excluded.state",
                (event_id, symbol, stage, state),
            )

    def open_lots(self) -> list[tuple[str, str, str]]:
        """(event_id, symbol, state) for every lot not yet closed.

        What the gateway re-arms from at startup. Closed lots stay in the table
        as history rather than being deleted — a lot that closed unexpectedly is
        the record you most want afterwards.
        """
        with self._connect() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT event_id, symbol, state FROM lots WHERE stage != 'closed' "
                "ORDER BY rowid")]

    def open_lot_for(self, symbol: str) -> str | None:
        """The event id of the open lot on this symbol, if there is one.

        One lot at a time per crypto symbol is Wei's rule, and it has to be
        answerable across a restart — otherwise a gateway that came back up
        would happily open a second lot on top of the first.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM lots WHERE symbol = ? AND stage != 'closed' "
                "LIMIT 1", (symbol,)).fetchone()
        return row[0] if row else None

    def status(self, event_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return row[0] if row else None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)
