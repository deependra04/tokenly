"""Regression tests for v0.2.2 hardening fixes.

Covers: atexit flush, concurrent init idempotency, URL-masking, in-memory
budget + day rollover, queue overflow drop, malformed env-var tolerance,
negative token clamp, unknown-provider warn-once, pricing mtime hot-reload,
tag validation."""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import tokenly
from tokenly import core, pricing


@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    """Isolate each hardening test from bleed-over global state."""
    monkeypatch.delenv("TOKENLY_DB_URL", raising=False)
    monkeypatch.delenv("TOKENLY_DB", raising=False)
    monkeypatch.delenv("TOKENLY_DAILY_BUDGET", raising=False)
    monkeypatch.delenv("TOKENLY_DAILY_WARN", raising=False)
    core._config = core.Config()
    # Drain any leftover rows from previous tests.
    while True:
        try:
            core._write_queue.get_nowait()
        except queue.Empty:
            break
    core._warned_negative.clear()
    core._warned_unknown_provider.clear()
    core._last_drop_warn_ts = 0.0
    yield
    core._shutdown(timeout=2.0)


# ────────────────────────── URL masking ──────────────────────────

@pytest.mark.parametrize(
    "url,expected",
    [
        ("postgresql://user:secret@host:5432/db", "postgresql://user:***@host:5432/db"),
        ("mysql://admin:hunter2@db.example/prod", "mysql://admin:***@db.example/prod"),
        ("sqlite:///tmp/log.db", "sqlite:///tmp/log.db"),
        ("", ""),
        ("not a url", "not a url"),
    ],
)
def test_mask_url(url, expected):
    assert core._mask_url(url) == expected


# ────────────────────────── Validation ──────────────────────────

def test_configure_rejects_non_string_db_url():
    with pytest.raises(TypeError):
        tokenly.configure(db_url=123)  # type: ignore[arg-type]


def test_configure_rejects_non_bool_otel():
    with pytest.raises(TypeError):
        tokenly.configure(otel="yes")  # type: ignore[arg-type]


def test_configure_rejects_negative_budget(tmp_path):
    with pytest.raises(ValueError):
        tokenly.configure(
            db_url=f"sqlite:///{tmp_path}/log.db",
            budget_usd_day=-1.0,
        )


@pytest.mark.parametrize(
    "tags",
    [
        {1: "v"},                    # non-string key
        {"k": object()},             # non-scalar value
        {"k": [1, 2, 3]},            # list value
        {"k": {"nested": "dict"}},   # nested dict
    ],
)
def test_configure_rejects_bad_tags(tmp_path, tags):
    with pytest.raises((TypeError, ValueError)):
        tokenly.configure(db_url=f"sqlite:///{tmp_path}/log.db", tags=tags)


def test_configure_accepts_scalar_tags(tmp_path):
    tokenly.configure(
        db_url=f"sqlite:///{tmp_path}/log.db",
        tags={"env": "prod", "v": 1, "x": 1.5, "b": True, "n": None},
    )


def test_env_var_malformed_is_ignored(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("TOKENLY_DAILY_BUDGET", "not-a-number")
    with caplog.at_level("WARNING", logger="tokenly"):
        tokenly.configure(db_url=f"sqlite:///{tmp_path}/log.db")
    assert core._config.budget_usd_day is None
    assert any("TOKENLY_DAILY_BUDGET" in r.getMessage() for r in caplog.records)


def test_env_var_negative_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLY_DAILY_WARN", "-1.0")
    tokenly.configure(db_url=f"sqlite:///{tmp_path}/log.db")
    assert core._config.warn_usd_day is None


# ────────────────────────── Budget counter ──────────────────────────

def test_budget_tick_rolls_over_utc_day():
    # Seed day N-1, then tick on day N → counter resets.
    core._budget_epoch_day = core._utc_epoch_day(time.time()) - 1
    core._budget_spend_usd = 999.0
    spent = core._budget_tick(0.01)
    assert spent == pytest.approx(0.01)
    assert core._budget_epoch_day == core._utc_epoch_day(time.time())


def test_budget_tick_accumulates_same_day():
    core._budget_epoch_day = core._utc_epoch_day(time.time())
    core._budget_spend_usd = 0.0
    core._budget_tick(1.0)
    total = core._budget_tick(2.5)
    assert total == pytest.approx(3.5)


# ────────────────────────── Token clamping ──────────────────────────

def test_negative_tokens_are_clamped_and_warned_once(tmp_path, caplog):
    tokenly.init(db_url=f"sqlite:///{tmp_path}/log.db")
    with caplog.at_level("WARNING", logger="tokenly"):
        tokenly.track("openai", "gpt-4o-mini", input_tokens=-5, output_tokens=10)
        tokenly.track("openai", "gpt-4o-mini", input_tokens=-100, output_tokens=1)
        tokenly.track("openai", "gpt-4o-mini", input_tokens=-7, output_tokens=1)
    warns = [r for r in caplog.records if "clamped" in r.getMessage()]
    assert len(warns) == 1  # warned once per (provider, model) pair


# ────────────────────────── Unknown provider warn-once ──────────────────

def test_unknown_provider_warns_once(caplog):
    pricing._warned_unknown.clear()
    with caplog.at_level("WARNING", logger="tokenly.pricing"):
        c1 = pricing.compute_cost("nope", "model-a", 10, 5)
        c2 = pricing.compute_cost("nope", "model-a", 10, 5)
        c3 = pricing.compute_cost("nope", "model-b", 10, 5)
    assert c1 == c2 == c3 == 0.0
    warns = [r for r in caplog.records if "no pricing" in r.getMessage()]
    # Warn once per (provider, model): nope/model-a + nope/model-b.
    assert len(warns) == 2


# ────────────────────────── Pricing mtime hot-reload ──────────────────

def test_pricing_reloads_when_file_mtime_changes(tmp_path, monkeypatch):
    # Point pricing.py at a writable copy, verify change shows up without
    # forcing anyone to restart the process.
    src = Path(pricing._PRICING_FILE)
    target = tmp_path / "pricing.json"
    target.write_text(src.read_text())
    monkeypatch.setattr(pricing, "_PRICING_FILE", target)
    pricing._cache = None
    pricing._cache_mtime = 0.0

    data = pricing._load()
    assert "openai/gpt-4o-mini" in data
    original = float(data["openai/gpt-4o-mini"]["input"])

    # Rewrite with a bumped rate and a future mtime.
    data["openai/gpt-4o-mini"]["input"] = original + 7.0
    target.write_text(json.dumps(data))
    os.utime(target, (target.stat().st_atime, time.time() + 10))

    reloaded = pricing._load()
    assert reloaded["openai/gpt-4o-mini"]["input"] == pytest.approx(original + 7.0)


# ────────────────────────── Concurrent init idempotency ──────────────────

def test_concurrent_init_only_spawns_one_writer(tmp_path):
    url = f"sqlite:///{tmp_path}/log.db"
    errors: list[Exception] = []

    def _go():
        try:
            tokenly.init(db_url=url)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)

    assert errors == []
    # One live writer thread named "tokenly-writer".
    alive = [
        t for t in threading.enumerate()
        if t.name == "tokenly-writer" and t.is_alive()
    ]
    assert len(alive) == 1


# ────────────────────────── Queue overflow drop ──────────────────

def test_queue_full_drops_without_blocking(tmp_path, monkeypatch, caplog):
    # Shrink queue so we can overflow it deterministically. Also use a
    # non-running writer so rows pile up.
    tokenly.configure(db_url=f"sqlite:///{tmp_path}/log.db")
    core._config.initialized = True  # skip writer spawn
    monkeypatch.setattr(core, "_write_queue", queue.Queue(maxsize=3))

    with caplog.at_level("WARNING", logger="tokenly"):
        start = time.monotonic()
        for _ in range(50):
            tokenly.track("openai", "gpt-4o-mini", 1, 1)
        elapsed = time.monotonic() - start

    # 50 calls must not block anywhere close to even half a second.
    assert elapsed < 0.5
    drop_warns = [r for r in caplog.records if "queue full" in r.getMessage()]
    assert len(drop_warns) >= 1


# ────────────────────────── Atexit registration ──────────────────

def test_atexit_registered_after_init(tmp_path):
    tokenly.init(db_url=f"sqlite:///{tmp_path}/log.db")
    assert core._atexit_registered is True


# ────────────────────────── Flush drains queue ──────────────────

def test_flush_persists_pending_rows(tmp_path):
    url = f"sqlite:///{tmp_path}/log.db"
    tokenly.init(db_url=url)
    for _ in range(25):
        tokenly.track("openai", "gpt-4o-mini", 10, 5)
    tokenly.flush(timeout=3.0)
    conn = sqlite3.connect(str(tmp_path / "log.db"))
    try:
        (n,) = conn.execute("SELECT COUNT(*) FROM calls").fetchone()
    finally:
        conn.close()
    assert n == 25
