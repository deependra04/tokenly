"""PostgreSQL backend. Requires `pip install tokenly[postgres]`."""
from __future__ import annotations

from urllib.parse import urlparse

from .base import Backend


class PostgresBackend(Backend):
    name = "postgres"
    ph = "%s"

    def _connect(self):
        try:
            import psycopg  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "tokenly: Postgres backend requires psycopg. "
                "Install with `pip install tokenly[postgres]`."
            ) from e

        # psycopg accepts postgresql:// URLs directly.
        dsn = self.url
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://"):]
        return psycopg.connect(dsn)

    def _ddl(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS calls (
                id BIGSERIAL PRIMARY KEY,
                ts DOUBLE PRECISION NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens BIGINT NOT NULL DEFAULT 0,
                output_tokens BIGINT NOT NULL DEFAULT 0,
                cache_read_tokens BIGINT NOT NULL DEFAULT 0,
                cache_write_tokens BIGINT NOT NULL DEFAULT 0,
                cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                tags TEXT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts)",
            "CREATE INDEX IF NOT EXISTS idx_calls_model ON calls(model)",
            "CREATE INDEX IF NOT EXISTS idx_calls_provider ON calls(provider)",
        ]

    def tag_expr(self, key: str) -> str:
        return f"(tags::jsonb->>'{key}')"

    def describe(self) -> str:
        u = urlparse(self.url)
        return f"postgres: {u.hostname}:{u.port or 5432}/{u.path.lstrip('/')}"
