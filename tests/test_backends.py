"""Tests for the pluggable backend layer (sqlite variant is exercised here;
MySQL / Postgres backends are lightly tested via URL parsing + import guards
since CI doesn't run those daemons)."""
from __future__ import annotations

import json
import os
import time

import pytest

from tokenly.backends import get_backend, resolve_url


def test_resolve_url_default_is_sqlite(monkeypatch):
    monkeypatch.delenv("TOKENLY_DB_URL", raising=False)
    monkeypatch.delenv("TOKENLY_DB", raising=False)
    url = resolve_url()
    assert url.startswith("sqlite://")


def test_resolve_url_kwarg_wins(monkeypatch):
    monkeypatch.setenv("TOKENLY_DB_URL", "sqlite:///env.db")
    url = resolve_url(db_url="postgresql://u:p@h/d")
    assert url == "postgresql://u:p@h/d"


def test_resolve_url_legacy_db_env(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKENLY_DB_URL", raising=False)
    monkeypatch.setenv("TOKENLY_DB", str(tmp_path / "legacy.db"))
    url = resolve_url()
    assert url.startswith("sqlite:///")
    assert "legacy.db" in url


def test_get_backend_unknown_scheme_raises():
    with pytest.raises(ValueError):
        get_backend("redis://localhost/0")


def test_sqlite_roundtrip(tmp_path):
    url = f"sqlite:///{tmp_path}/log.db"
    b = get_backend(url)
    try:
        b.write_row(
            (
                time.time(),
                "openai",
                "gpt-4o-mini",
                1000,
                200,
                50,
                0,
                0.0012,
                500,
                json.dumps({"user": "alice"}),
            )
        )
        calls, in_tok, out_tok, cr, cw, cost, avg_lat = b.totals(since_ts=None)
        assert calls == 1
        assert in_tok == 1000
        assert out_tok == 200
        assert cr == 50
        assert cw == 0
        assert cost == pytest.approx(0.0012)
    finally:
        b.close()


def test_sqlite_group_by_tag(tmp_path):
    url = f"sqlite:///{tmp_path}/log.db"
    b = get_backend(url)
    try:
        for user, cost in [("alice", 0.01), ("bob", 0.02), ("alice", 0.03)]:
            b.write_row(
                (
                    time.time(),
                    "openai",
                    "gpt-4o-mini",
                    100,
                    50,
                    0,
                    0,
                    cost,
                    100,
                    json.dumps({"user": user}),
                )
            )
        rows = b.group_by("tag.user", since_ts=None)
        totals = {r[0]: r[1] for r in rows}
        assert totals["alice"] == pytest.approx(0.04)
        assert totals["bob"] == pytest.approx(0.02)
    finally:
        b.close()


def test_tag_key_sanitization(tmp_path):
    url = f"sqlite:///{tmp_path}/log.db"
    b = get_backend(url)
    try:
        with pytest.raises(ValueError):
            b.group_by("tag.drop; DROP TABLE calls", since_ts=None)
    finally:
        b.close()


def test_reset_clears_calls(tmp_path):
    url = f"sqlite:///{tmp_path}/log.db"
    b = get_backend(url)
    try:
        b.write_row(
            (time.time(), "openai", "gpt-4o-mini", 10, 5, 0, 0, 0.0, 50, None)
        )
        assert b.totals(since_ts=None)[0] == 1
        b.reset()
    finally:
        b.close()
    # Reopen — for sqlite reset deletes the file; schema is recreated on reconnect.
    b2 = get_backend(url)
    try:
        assert b2.totals(since_ts=None)[0] == 0
    finally:
        b2.close()


def test_mysql_backend_missing_driver_is_clear(monkeypatch):
    from tokenly.backends.mysql import MysqlBackend

    b = MysqlBackend("mysql://u:p@h/d")
    # Force pymysql import to fail.
    import sys

    monkeypatch.setitem(sys.modules, "pymysql", None)
    with pytest.raises(RuntimeError, match="pip install tokenly\\[mysql\\]"):
        _ = b.conn


def test_postgres_backend_missing_driver_is_clear(monkeypatch):
    from tokenly.backends.postgres import PostgresBackend

    b = PostgresBackend("postgresql://u:p@h/d")
    import sys

    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(RuntimeError, match="pip install tokenly\\[postgres\\]"):
        _ = b.conn


def test_real_postgres_backend_if_available():
    """Opt-in: set TOKENLY_TEST_POSTGRES_URL to run against a live db."""
    url = os.environ.get("TOKENLY_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TOKENLY_TEST_POSTGRES_URL not set")
    b = get_backend(url)
    try:
        b.write_row(
            (time.time(), "openai", "gpt-4o-mini", 10, 5, 0, 0, 0.0001, 50, None)
        )
        assert b.totals(since_ts=None)[0] >= 1
    finally:
        b.close()


def test_real_mysql_backend_if_available():
    """Opt-in: set TOKENLY_TEST_MYSQL_URL to run against a live db."""
    url = os.environ.get("TOKENLY_TEST_MYSQL_URL")
    if not url:
        pytest.skip("TOKENLY_TEST_MYSQL_URL not set")
    b = get_backend(url)
    try:
        b.write_row(
            (time.time(), "openai", "gpt-4o-mini", 10, 5, 0, 0, 0.0001, 50, None)
        )
        assert b.totals(since_ts=None)[0] >= 1
    finally:
        b.close()
