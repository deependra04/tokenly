"""SQLite backend — default, zero-dep."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .base import Backend


def _dsn_to_path(url: str) -> Path:
    if "://" in url:
        _, _, rest = url.partition("://")
        # sqlite:///absolute → rest='/absolute'; sqlite:///~/x → rest='/~/x'
        # Strip leading slash only if URL has three slashes (sqlite:///…)
        if url.startswith("sqlite:///") or url.startswith("file:///"):
            rest = rest  # keep as-is, has leading /
        return Path(rest).expanduser()
    return Path(url).expanduser()


class SqliteBackend(Backend):
    name = "sqlite"
    ph = "?"

    def _connect(self):
        p = _dsn_to_path(self.url)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ddl(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                tags TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts)",
            "CREATE INDEX IF NOT EXISTS idx_calls_model ON calls(model)",
            "CREATE INDEX IF NOT EXISTS idx_calls_provider ON calls(provider)",
        ]

    def tag_expr(self, key: str) -> str:
        return f"json_extract(tags, '$.{key}')"

    def describe(self) -> str:
        p = _dsn_to_path(self.url)
        exists = p.exists()
        return f"sqlite: {p} (exists={exists})"

    def reset(self) -> None:
        """For sqlite, delete the file — cleanest and removes WAL sidecars."""
        self.close()
        p = _dsn_to_path(self.url)
        for suffix in ("", "-wal", "-shm"):
            fp = Path(str(p) + suffix)
            if fp.exists():
                try:
                    os.remove(fp)
                except OSError:
                    pass
