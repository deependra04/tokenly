"""Tests for core: track() writes to db, unknown models log warnings, budgets throw."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

import tokenly
from tokenly import core


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "log.db"
    core._config = core.Config()
    core._write_queue.queue.clear() if hasattr(core._write_queue, "queue") else None
    monkeypatch.setenv("TOKENLY_DB", str(db))
    core.init(db_path=db)
    yield db
    core._stop_event.set()
    if core._writer_thread:
        core._writer_thread.join(timeout=2)


def _wait_for_row(db: Path, expected: int = 1, timeout: float = 2.0) -> int:
    start = time.time()
    while time.time() - start < timeout:
        conn = sqlite3.connect(str(db))
        try:
            n = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        finally:
            conn.close()
        if n >= expected:
            return n
        time.sleep(0.05)
    return n


def test_track_writes_row(tmp_db):
    core.track(
        provider="openai",
        model="gpt-5-mini",
        input_tokens=100,
        output_tokens=50,
        latency_ms=120,
    )
    n = _wait_for_row(tmp_db, 1)
    assert n == 1

    conn = sqlite3.connect(str(tmp_db))
    row = conn.execute(
        "SELECT provider, model, input_tokens, output_tokens, cost_usd, latency_ms FROM calls"
    ).fetchone()
    conn.close()
    prov, model, in_t, out_t, cost, lat = row
    assert prov == "openai"
    assert model == "gpt-5-mini"
    assert in_t == 100
    assert out_t == 50
    assert cost > 0
    assert lat == 120


def test_budget_exceeded_raises(tmp_db):
    core.configure(budget_usd_day=0.0001)
    with pytest.raises(core.BudgetExceeded):
        for _ in range(5):
            core.track(
                provider="openai",
                model="gpt-5.2",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
            time.sleep(0.05)


def test_unknown_model_zero_cost(tmp_db):
    cost = core.track(provider="openai", model="totally-made-up", input_tokens=100, output_tokens=50)
    assert cost == 0.0


def test_init_is_idempotent(tmp_db):
    core.init(db_path=tmp_db)
    core.init(db_path=tmp_db)


def test_module_api_exports():
    assert hasattr(tokenly, "init")
    assert hasattr(tokenly, "track")
    assert hasattr(tokenly, "configure")
    assert hasattr(tokenly, "BudgetExceeded")
    assert hasattr(tokenly, "__version__")
