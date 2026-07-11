"""Store-and-forward buffer (SQLite).

Telemetry that can't be published (broker unreachable, publish failed) is persisted
locally and flushed on the next successful connect, so no reading is lost when the
network blips. The buffer is capped: the oldest rows are trimmed past `max_rows`.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import List, Tuple


class Buffer:
    def __init__(self, path: str, max_rows: int = 5000):
        self.path = path
        self.max_rows = max_rows
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbox ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, topic TEXT, payload TEXT)"
        )
        self._db.commit()

    def append(self, topic: str, payload: dict) -> None:
        self._db.execute(
            "INSERT INTO outbox (ts, topic, payload) VALUES (?, ?, ?)",
            (int(time.time()), topic, json.dumps(payload)),
        )
        # trim oldest rows beyond the cap
        self._db.execute(
            "DELETE FROM outbox WHERE id IN "
            "(SELECT id FROM outbox ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (self.max_rows,),
        )
        self._db.commit()

    def pending(self, limit: int = 200) -> List[Tuple[int, str, dict]]:
        cur = self._db.execute(
            "SELECT id, topic, payload FROM outbox ORDER BY id ASC LIMIT ?", (limit,)
        )
        return [(r[0], r[1], json.loads(r[2])) for r in cur.fetchall()]

    def delete(self, ids: List[int]) -> None:
        if not ids:
            return
        self._db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])
        self._db.commit()

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def close(self) -> None:
        self._db.close()
