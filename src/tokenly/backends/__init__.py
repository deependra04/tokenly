"""Pluggable storage backends: sqlite (default), mysql, postgres."""
from __future__ import annotations

import os
from pathlib import Path

from .base import Backend

_DEFAULT_SQLITE = Path.home() / ".tokenly" / "log.db"


def resolve_url(
    db_url: str | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Figure out which backend URL to use, honoring legacy env vars."""
    if db_url:
        return db_url
    env_url = os.environ.get("TOKENLY_DB_URL")
    if env_url:
        return env_url
    if db_path is not None:
        return f"sqlite:///{Path(db_path).expanduser()}"
    legacy = os.environ.get("TOKENLY_DB")
    if legacy:
        return f"sqlite:///{Path(legacy).expanduser()}"
    return f"sqlite:///{_DEFAULT_SQLITE}"


def get_backend(url: str) -> Backend:
    """Build a backend from a db URL."""
    scheme = url.split("://", 1)[0].lower() if "://" in url else "sqlite"
    if scheme in ("sqlite", "file", ""):
        from .sqlite import SqliteBackend

        return SqliteBackend(url)
    if scheme in ("mysql", "mariadb"):
        from .mysql import MysqlBackend

        return MysqlBackend(url)
    if scheme in ("postgres", "postgresql"):
        from .postgres import PostgresBackend

        return PostgresBackend(url)
    raise ValueError(
        f"tokenly: unsupported db scheme '{scheme}'. "
        f"Use sqlite://, mysql://, or postgresql://"
    )


__all__ = ["Backend", "get_backend", "resolve_url"]
