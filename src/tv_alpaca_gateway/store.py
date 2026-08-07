from __future__ import annotations

import sqlite3
from pathlib import Path


class EventStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')"
            )

    def claim(self, event_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events(event_id, status) VALUES (?, 'claimed')",
                (event_id,),
            )
            return cur.rowcount == 1

    def update(self, event_id: str, status: str, detail: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE events SET status = ?, detail = ? WHERE event_id = ?",
                (status, detail[:2000], event_id),
            )

    def status(self, event_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return row[0] if row else None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)
