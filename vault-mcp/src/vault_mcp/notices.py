"""Which person-maintained pages already got an "agent updated this page" notice today
(SPEC-write-policy: one notice per page per UTC day). Lives in `<data>/state.db`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_edit_notices (
    page TEXT NOT NULL,
    day TEXT NOT NULL,
    PRIMARY KEY (page, day)
);
"""


class NoticeLog:
    def __init__(self, data_dir: Path) -> None:
        self.db_path = data_dir / "state.db"
        with self._db() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def claim(self, page: str, day: str) -> bool:
        """True the first time a page is claimed on a day, so exactly one caller adds the notice."""
        with self._db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO agent_edit_notices (page, day) VALUES (?, ?)", (page, day)
            )
            return cur.rowcount == 1

    def release(self, page: str, day: str) -> None:
        """Undo a claim whose notice couldn't be written, so the next edit tries again."""
        with self._db() as conn:
            conn.execute("DELETE FROM agent_edit_notices WHERE page = ? AND day = ?", (page, day))
