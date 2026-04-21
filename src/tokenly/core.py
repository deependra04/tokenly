"""Core: config, storage (pluggable backend), background writer, call logging."""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backends import Backend, get_backend, resolve_url
from .pricing import compute_cost, is_known

log = logging.getLogger("tokenly")


class BudgetExceeded(RuntimeError):
    """Raised when a configured daily budget is exceeded."""


@dataclass
class Config:
    db_url: str = ""
    budget_usd_day: float | None = None
    warn_usd_day: float | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    initialized: bool = False
    patched: set[str] = field(default_factory=set)


_config = Config()
_write_queue: queue.Queue = queue.Queue()
_writer_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _writer_loop(url: str) -> None:
    backend: Backend | None = None
    try:
        backend = get_backend(url)
        _ = backend.conn  # force connect + ensure schema
        while not _stop_event.is_set() or not _write_queue.empty():
            try:
                row = _write_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                backend.write_row(row)
            except Exception as e:
                log.warning("tokenly: failed to write call: %s", e)
    finally:
        if backend is not None:
            backend.close()


def _start_writer() -> None:
    global _writer_thread
    if _writer_thread and _writer_thread.is_alive():
        return
    _stop_event.clear()
    _writer_thread = threading.Thread(
        target=_writer_loop, args=(_config.db_url,), name="tokenly-writer", daemon=True
    )
    _writer_thread.start()


def _today_spend_usd() -> float:
    """Budget check — opens a short-lived backend."""
    backend = get_backend(_config.db_url)
    try:
        return backend.today_spend_usd()
    finally:
        backend.close()


def track(
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    latency_ms: int = 0,
    tags: dict[str, Any] | None = None,
) -> float:
    """Record a single LLM call. Returns computed cost in USD."""
    if not _config.initialized:
        init()

    cost = compute_cost(
        provider,
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )

    if not is_known(provider, model):
        log.warning(
            "tokenly: unknown model %s/%s — logged with $0 cost. "
            "PR pricing at https://github.com/deependra04/tokenly",
            provider,
            model,
        )

    merged_tags = {**_config.tags, **(tags or {})}
    tags_json = json.dumps(merged_tags) if merged_tags else None

    _write_queue.put(
        (
            time.time(),
            provider,
            model,
            int(input_tokens),
            int(output_tokens),
            int(cache_read_tokens),
            int(cache_write_tokens),
            float(cost),
            int(latency_ms),
            tags_json,
        )
    )

    if _config.budget_usd_day is not None:
        spent = _today_spend_usd() + cost
        if spent >= _config.budget_usd_day:
            raise BudgetExceeded(
                f"Daily budget ${_config.budget_usd_day:.2f} exceeded (spent ${spent:.2f})"
            )

    if _config.warn_usd_day is not None:
        spent = _today_spend_usd() + cost
        if spent >= _config.warn_usd_day:
            print(
                f"tokenly: daily spend ${spent:.2f} passed warn threshold "
                f"${_config.warn_usd_day:.2f}",
                file=sys.stderr,
            )

    return cost


def configure(
    db_url: str | None = None,
    db_path: str | Path | None = None,
    budget_usd_day: float | None = None,
    warn_usd_day: float | None = None,
    tags: dict[str, Any] | None = None,
) -> None:
    """Configure tokenly without initializing patches.

    Priority for storage location:
        1. db_url kwarg   (e.g. "postgresql://user:pass@host/db")
        2. db_path kwarg  (legacy, sqlite only — wrapped into sqlite://)
        3. TOKENLY_DB_URL env
        4. TOKENLY_DB env (legacy)
        5. default ~/.tokenly/log.db
    """
    resolved = resolve_url(db_url=db_url, db_path=db_path)
    _config.db_url = resolved

    if budget_usd_day is not None:
        _config.budget_usd_day = float(budget_usd_day)
    elif os.environ.get("TOKENLY_DAILY_BUDGET"):
        _config.budget_usd_day = float(os.environ["TOKENLY_DAILY_BUDGET"])

    if warn_usd_day is not None:
        _config.warn_usd_day = float(warn_usd_day)
    elif os.environ.get("TOKENLY_DAILY_WARN"):
        _config.warn_usd_day = float(os.environ["TOKENLY_DAILY_WARN"])

    if tags is not None:
        _config.tags = dict(tags)


def init(
    db_url: str | None = None,
    db_path: str | Path | None = None,
    budget_usd_day: float | None = None,
    warn_usd_day: float | None = None,
    tags: dict[str, Any] | None = None,
) -> None:
    """Initialize tokenly. Call once at app startup.

    Detects installed provider SDKs and patches them.

    Storage is SQLite by default. Pass db_url to use MySQL or Postgres:
        tokenly.init(db_url="mysql://user:pass@host/dbname")
        tokenly.init(db_url="postgresql://user:pass@host/dbname")
    """
    configure(
        db_url=db_url,
        db_path=db_path,
        budget_usd_day=budget_usd_day,
        warn_usd_day=warn_usd_day,
        tags=tags,
    )

    # Open once to validate connection + ensure schema exists.
    backend = get_backend(_config.db_url)
    try:
        _ = backend.conn
    finally:
        backend.close()

    _start_writer()
    _config.initialized = True

    import importlib.util

    def _has_module(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ModuleNotFoundError, ValueError):
            return False

    if _has_module("openai") and "openai" not in _config.patched:
        try:
            from .providers import openai as p

            p.patch()
            _config.patched.add("openai")
        except Exception as e:
            log.warning("tokenly: failed to patch openai: %s", e)

    if _has_module("anthropic") and "anthropic" not in _config.patched:
        try:
            from .providers import anthropic as p

            p.patch()
            _config.patched.add("anthropic")
        except Exception as e:
            log.warning("tokenly: failed to patch anthropic: %s", e)

    if (
        _has_module("google.genai") or _has_module("google.generativeai")
    ) and "google" not in _config.patched:
        try:
            from .providers import google as p

            p.patch()
            _config.patched.add("google")
        except Exception as e:
            log.warning("tokenly: failed to patch google: %s", e)
