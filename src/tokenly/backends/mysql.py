"""MySQL / MariaDB backend. Requires `pip install tokenly[mysql]`."""
from __future__ import annotations

from urllib.parse import unquote, urlparse

from .base import Backend


class MysqlBackend(Backend):
    name = "mysql"
    ph = "%s"

    def _connect(self):
        try:
            import pymysql
        except ImportError as e:
            raise RuntimeError(
                "tokenly: MySQL backend requires pymysql. "
                "Install with `pip install tokenly[mysql]`."
            ) from e

        u = urlparse(self.url)
        if not u.hostname:
            raise ValueError(f"tokenly: invalid mysql url {self.url!r}")
        kwargs = dict(
            host=u.hostname,
            port=u.port or 3306,
            user=unquote(u.username) if u.username else None,
            password=unquote(u.password) if u.password else None,
            database=u.path.lstrip("/") or None,
            charset="utf8mb4",
            autocommit=False,
        )
        return pymysql.connect(**{k: v for k, v in kwargs.items() if v is not None})

    def _ddl(self) -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS calls (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                ts DOUBLE NOT NULL,
                provider VARCHAR(64) NOT NULL,
                model VARCHAR(128) NOT NULL,
                input_tokens BIGINT NOT NULL DEFAULT 0,
                output_tokens BIGINT NOT NULL DEFAULT 0,
                cache_read_tokens BIGINT NOT NULL DEFAULT 0,
                cache_write_tokens BIGINT NOT NULL DEFAULT 0,
                cost_usd DOUBLE NOT NULL DEFAULT 0,
                latency_ms INT NOT NULL DEFAULT 0,
                tags JSON NULL,
                INDEX idx_calls_ts (ts),
                INDEX idx_calls_model (model),
                INDEX idx_calls_provider (provider)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        ]

    def tag_expr(self, key: str) -> str:
        # JSON_UNQUOTE so grouping key is a plain string, not '"value"'.
        return f"JSON_UNQUOTE(JSON_EXTRACT(tags, '$.{key}'))"

    def describe(self) -> str:
        u = urlparse(self.url)
        return f"mysql: {u.hostname}:{u.port or 3306}/{u.path.lstrip('/')}"
