"""Base backend. All SQL shared across dialects lives here; each subclass
only fills in DDL, connection, the placeholder token, and JSON extraction."""
from __future__ import annotations

import re
import time
from collections.abc import Iterable
from typing import Any

_COLS = (
    "ts",
    "provider",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "latency_ms",
    "tags",
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_tag_key(key: str) -> str:
    """Reject non-identifier tag keys to keep SQL injection off the table."""
    if not _IDENT.match(key):
        raise ValueError(f"tokenly: unsafe tag key {key!r}")
    return key


class Backend:
    """Storage backend. Subclass and override the marked hooks."""

    name: str = "base"
    ph: str = "?"  # parameter placeholder

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn: Any = None

    # ── subclass hooks ────────────────────────────────────────────────
    def _connect(self) -> Any:
        raise NotImplementedError

    def _ddl(self) -> list[str]:
        raise NotImplementedError

    def tag_expr(self, key: str) -> str:
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}: {self.url}"

    def reset(self) -> None:
        """Drop all logged calls. Default: DELETE FROM calls."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM calls")
        self._conn.commit()

    # ── connection lifecycle ──────────────────────────────────────────
    @property
    def conn(self) -> Any:
        if self._conn is None:
            self._conn = self._connect()
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            for stmt in self._ddl():
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            self._conn.commit()
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> Backend:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── helpers ───────────────────────────────────────────────────────
    class _CursorCtx:
        def __init__(self, conn: Any) -> None:
            self.cur = conn.cursor()

        def __enter__(self) -> Any:
            return self.cur

        def __exit__(self, *exc) -> None:
            try:
                self.cur.close()
            except Exception:
                pass

    def _cursor(self) -> Any:
        return Backend._CursorCtx(self.conn)

    def _ph_list(self, n: int) -> str:
        return ", ".join([self.ph] * n)

    # ── writes ────────────────────────────────────────────────────────
    def write_row(self, row: tuple) -> None:
        sql = (
            f"INSERT INTO calls ({', '.join(_COLS)}) "
            f"VALUES ({self._ph_list(len(_COLS))})"
        )
        with self._cursor() as cur:
            cur.execute(sql, tuple(row))
        self.conn.commit()

    # ── reads ─────────────────────────────────────────────────────────
    def totals(self, since_ts: float | None) -> tuple:
        where, params = self._window(since_ts)
        sql = (
            "SELECT COUNT(*), "
            "COALESCE(SUM(input_tokens),0), "
            "COALESCE(SUM(output_tokens),0), "
            "COALESCE(SUM(cache_read_tokens),0), "
            "COALESCE(SUM(cache_write_tokens),0), "
            "COALESCE(SUM(cost_usd),0), "
            "COALESCE(AVG(latency_ms),0) "
            f"FROM calls WHERE {where}"
        )
        with self._cursor() as cur:
            cur.execute(sql, params)
            return tuple(cur.fetchone())

    def group_by(
        self, field: str, since_ts: float | None, limit: int = 10
    ) -> list[tuple]:
        where, params = self._window(since_ts)
        if field.startswith("tag."):
            expr = self.tag_expr(_safe_tag_key(field.split(".", 1)[1]))
            sql = (
                f"SELECT {expr} AS k, COALESCE(SUM(cost_usd),0), COUNT(*) "
                f"FROM calls WHERE {where} "
                f"GROUP BY k ORDER BY SUM(cost_usd) DESC LIMIT {int(limit)}"
            )
        else:
            col = {"model": "model", "provider": "provider"}.get(field, "model")
            sql = (
                f"SELECT {col}, COALESCE(SUM(cost_usd),0), COUNT(*) "
                f"FROM calls WHERE {where} "
                f"GROUP BY {col} ORDER BY SUM(cost_usd) DESC LIMIT {int(limit)}"
            )
        with self._cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def tail_since(self, last_id: int) -> list[tuple]:
        sql = (
            "SELECT id, ts, provider, model, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd, latency_ms "
            f"FROM calls WHERE id > {self.ph} ORDER BY id ASC"
        )
        with self._cursor() as cur:
            cur.execute(sql, (last_id,))
            return list(cur.fetchall())

    def export_all(self) -> Iterable[tuple]:
        sql = (
            "SELECT ts, provider, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, cost_usd, latency_ms, tags "
            "FROM calls ORDER BY ts ASC"
        )
        with self._cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())

    def max_id(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM calls")
            row = cur.fetchone()
        return int(row[0] or 0)

    def today_spend_usd(self) -> float:
        start = _start_of_day_epoch()
        sql = (
            f"SELECT COALESCE(SUM(cost_usd), 0) FROM calls WHERE ts >= {self.ph}"
        )
        with self._cursor() as cur:
            cur.execute(sql, (start,))
            row = cur.fetchone()
        return float(row[0] or 0.0)

    # ── window helper ─────────────────────────────────────────────────
    def _window(self, since_ts: float | None) -> tuple[str, tuple]:
        if since_ts is None:
            return "1=1", ()
        return f"ts >= {self.ph}", (float(since_ts),)


def _start_of_day_epoch() -> float:
    """Start-of-day (local time) as unix seconds."""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def start_of_day_epoch() -> float:
    return _start_of_day_epoch()


def start_of_month_epoch() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def last_n_days_epoch(days: int) -> float:
    return time.time() - days * 86400
