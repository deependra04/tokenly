"""Core: config, storage (pluggable backend), background writer, call logging.

Public surface: `init`, `configure`, `track`, `flush`, `BudgetExceeded`.

Design rules (do not break):
    * Never block the caller. All writes go through a bounded queue drained
      by a background thread.
    * Never crash the caller. Tracking wraps every side-effect in try/except
      and downgrades to a warning.
    * Stay zero-dep on the default path. Only stdlib is imported from
      `core`; optional extras (mysql/postgres/otel) load lazily.
"""
from __future__ import annotations

import atexit
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
from urllib.parse import urlparse, urlunparse

from .backends import Backend, get_backend, resolve_url
from .pricing import compute_cost, is_known

log = logging.getLogger("tokenly")

# Tuning knobs. Kept module-level so tests can override.
_QUEUE_MAXSIZE = 10_000          # drop new rows past this, never block caller
_BATCH_MAX = 100                 # max rows per DB transaction
_BATCH_WAIT_SEC = 0.5            # max time to collect a partial batch
_DROP_WARN_COOLDOWN = 60.0       # seconds between "queue full, dropping" warnings
_SHUTDOWN_TIMEOUT = 5.0          # default timeout for flush() / atexit

_TAG_SCALARS = (str, int, float, bool, type(None))


class BudgetExceeded(RuntimeError):
    """Raised when a configured daily budget is exceeded."""


@dataclass
class Config:
    db_url: str = ""
    budget_usd_day: float | None = None
    warn_usd_day: float | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    otel: bool = False
    initialized: bool = False
    patched: set[str] = field(default_factory=set)


_config = Config()
_write_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
_writer_thread: threading.Thread | None = None
_stop_event = threading.Event()
_init_lock = threading.Lock()
_atexit_registered = False

# `flush()` signals the writer to commit its in-progress batch right away
# instead of waiting for the batch-collection window to expire. Cleared by
# the writer once the batch is committed and the queue is empty.
_flush_now = threading.Event()
_idle_event = threading.Event()
_idle_event.set()  # starts idle

# In-memory rolling budget counter. Seeded from the DB at writer startup; kept
# up to date by `track()`. This keeps the hot path off the database — every
# call used to open a fresh connection, which dominated CPU under load.
_budget_lock = threading.Lock()
_budget_epoch_day: int = -1      # UTC day index
_budget_spend_usd: float = 0.0

# De-duped warning registries. Each entry is a tuple key; we emit once per key.
_warned_negative: set[tuple[str, str]] = set()
_warned_unknown_provider: set[str] = set()

# Last time we warned about a full queue. Gate to avoid log spam.
_last_drop_warn_ts: float = 0.0


# ────────────────────────────────────────────────────────────────────────
# URL masking (security 2.1)
# ────────────────────────────────────────────────────────────────────────

def _mask_url(url: str) -> str:
    """Return a display-safe copy of a DB URL with the password redacted.

    sqlite paths have no userinfo so they pass through unchanged.
    """
    if not url or "://" not in url:
        return url
    try:
        u = urlparse(url)
    except Exception:
        return url
    if not u.password:
        return url
    user = u.username or ""
    host = u.hostname or ""
    port = f":{u.port}" if u.port else ""
    netloc = f"{user}:***@{host}{port}" if user else f"***@{host}{port}"
    return urlunparse(u._replace(netloc=netloc))


# ────────────────────────────────────────────────────────────────────────
# Validation (workstream 4.1 / 4.6)
# ────────────────────────────────────────────────────────────────────────

def _validate_number(
    value: Any, name: str, *, allow_none: bool = True, non_negative: bool = True
) -> float | None:
    if value is None:
        if not allow_none:
            raise TypeError(f"tokenly: {name} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"tokenly: {name} must be a number, got {type(value).__name__}")
    val = float(value)
    if non_negative and val < 0:
        raise ValueError(f"tokenly: {name} must be >= 0, got {val}")
    return val


def _validate_tags(tags: Any) -> dict[str, Any] | None:
    """Require a flat dict of str → scalar. Fails loudly at config time."""
    if tags is None:
        return None
    if not isinstance(tags, dict):
        raise TypeError(
            f"tokenly: tags must be a dict, got {type(tags).__name__}"
        )
    clean: dict[str, Any] = {}
    for k, v in tags.items():
        if not isinstance(k, str):
            raise TypeError(f"tokenly: tag keys must be str, got {type(k).__name__}")
        if not isinstance(v, _TAG_SCALARS):
            raise TypeError(
                f"tokenly: tag {k!r} value must be str/int/float/bool/None, "
                f"got {type(v).__name__}"
            )
        clean[k] = v
    # Catch any JSON edge cases (inf/nan) early rather than at write time.
    try:
        json.dumps(clean, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ValueError(f"tokenly: tags are not JSON-serializable: {e}") from e
    return clean


def _parse_env_float(name: str) -> float | None:
    """Read a float env var. On parse failure, warn and return None."""
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    try:
        val = float(raw)
    except ValueError:
        log.warning("tokenly: ignoring malformed %s=%r", name, raw)
        return None
    if val < 0:
        log.warning("tokenly: ignoring negative %s=%r", name, raw)
        return None
    return val


# ────────────────────────────────────────────────────────────────────────
# Background writer (workstream 1.1 / 3.1 / 3.2)
# ────────────────────────────────────────────────────────────────────────

def _utc_epoch_day(ts: float) -> int:
    return int(ts // 86400)


def _seed_budget_counter(backend: Backend) -> None:
    """Called once on writer startup. Pre-loads today's spend so the
    in-memory counter doesn't start at $0 after a restart."""
    global _budget_epoch_day, _budget_spend_usd
    try:
        today_spend = float(backend.today_spend_usd())
    except Exception as e:
        log.warning("tokenly: seeding budget counter failed: %s", e)
        today_spend = 0.0
    with _budget_lock:
        _budget_epoch_day = _utc_epoch_day(time.time())
        _budget_spend_usd = today_spend


def _write_batch(backend: Backend, rows: list[tuple]) -> None:
    """Write a batch. Prefers backend.write_rows (single commit); falls back
    to per-row writes so subclasses can opt out."""
    if not rows:
        return
    try:
        backend.write_rows(rows)
    except Exception as e:
        log.warning(
            "tokenly: batched write failed (%s); falling back to per-row",
            e,
        )
        for row in rows:
            try:
                backend.write_row(row)
            except Exception as e2:
                log.warning("tokenly: failed to write call: %s", e2)


def _writer_loop(url: str) -> None:
    backend: Backend | None = None
    try:
        backend = get_backend(url)
        _ = backend.conn  # force connect + ensure schema
        _seed_budget_counter(backend)

        batch: list[tuple] = []
        batch_deadline = 0.0

        while not _stop_event.is_set() or not _write_queue.empty() or batch:
            # Mark busy when we have work in flight.
            if batch or not _write_queue.empty():
                _idle_event.clear()

            # Block briefly so we collect multiple rows into one txn. If
            # `flush()` has asked for an immediate commit, shorten the wait.
            try:
                if _flush_now.is_set():
                    timeout = 0.01
                elif batch:
                    timeout = max(0.01, batch_deadline - time.monotonic())
                else:
                    timeout = _BATCH_WAIT_SEC
                row = _write_queue.get(timeout=timeout)
                if not batch:
                    batch_deadline = time.monotonic() + _BATCH_WAIT_SEC
                batch.append(row)
                if len(batch) >= _BATCH_MAX or _flush_now.is_set():
                    _write_batch(backend, batch)
                    batch = []
            except queue.Empty:
                if batch:
                    _write_batch(backend, batch)
                    batch = []
                if _write_queue.empty():
                    _idle_event.set()

        if batch:
            _write_batch(backend, batch)
        _idle_event.set()
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


def _shutdown(timeout: float = _SHUTDOWN_TIMEOUT) -> None:
    """Signal the writer to stop and wait for it to drain the queue."""
    _stop_event.set()
    t = _writer_thread
    if t is not None and t.is_alive():
        t.join(timeout=timeout)


def flush(timeout: float = _SHUTDOWN_TIMEOUT) -> None:
    """Block until every queued row has been persisted (or `timeout` elapses).

    Intended for workers that are about to be SIGKILLed or for tests that
    need deterministic state. Safe to call multiple times.
    """
    t = _writer_thread
    if t is None or not t.is_alive():
        return
    deadline = time.monotonic() + float(timeout)
    _flush_now.set()
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            # Idle = batch empty AND queue empty. The writer re-arms this
            # event every time a new row arrives, so the loop exits only
            # when we've truly drained.
            if _idle_event.wait(timeout=min(remaining, 0.05)):
                if _write_queue.empty():
                    return
                _idle_event.clear()
    finally:
        _flush_now.clear()


def _register_atexit_once() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(_shutdown)
    _atexit_registered = True


# ────────────────────────────────────────────────────────────────────────
# Budget (workstream 1.3)
# ────────────────────────────────────────────────────────────────────────

def _budget_tick(cost: float) -> float:
    """Add `cost` to today's rolling total (handling day rollover) and
    return the running total."""
    global _budget_epoch_day, _budget_spend_usd
    now = time.time()
    today = _utc_epoch_day(now)
    with _budget_lock:
        if today != _budget_epoch_day:
            _budget_epoch_day = today
            _budget_spend_usd = 0.0
        _budget_spend_usd += float(cost)
        return _budget_spend_usd


# ────────────────────────────────────────────────────────────────────────
# Public: track
# ────────────────────────────────────────────────────────────────────────

def _clamp_tokens(provider: str, model: str, **kw: int) -> dict[str, int]:
    """Clamp negative token counts to 0 with a warn-once per (provider, model)."""
    out: dict[str, int] = {}
    flagged = False
    for k, v in kw.items():
        try:
            iv = int(v)
        except (TypeError, ValueError):
            iv = 0
            flagged = True
        if iv < 0:
            iv = 0
            flagged = True
        out[k] = iv
    if flagged:
        key = (provider, model)
        if key not in _warned_negative:
            _warned_negative.add(key)
            log.warning(
                "tokenly: clamped invalid token counts for %s/%s to 0 "
                "(warned once per model)",
                provider,
                model,
            )
    return out


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
    """Record a single LLM call. Returns computed cost in USD.

    Token counts must be non-negative; negatives are clamped to 0 with a
    warning. This function never raises except for `BudgetExceeded`.
    """
    if not _config.initialized:
        init()

    clamped = _clamp_tokens(
        provider,
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )

    cost = compute_cost(provider, model, **clamped)

    if not is_known(provider, model):
        log.warning(
            "tokenly: unknown model %s/%s — logged with $0 cost. "
            "PR pricing at https://github.com/deependra04/tokenly",
            provider,
            model,
        )

    # Tags are validated at configure() time; per-call `tags` may still be
    # arbitrary. Serialize with a fallback so a bad per-call dict doesn't
    # break the caller's API request.
    merged_tags = {**_config.tags, **(tags or {})}
    try:
        tags_json = json.dumps(merged_tags, allow_nan=False) if merged_tags else None
    except (TypeError, ValueError) as e:
        log.warning("tokenly: dropping unserializable per-call tags: %s", e)
        tags_json = json.dumps(_config.tags) if _config.tags else None

    latency_ms = max(0, int(latency_ms or 0))

    try:
        _write_queue.put_nowait(
            (
                time.time(),
                provider,
                model,
                clamped["input_tokens"],
                clamped["output_tokens"],
                clamped["cache_read_tokens"],
                clamped["cache_write_tokens"],
                float(cost),
                latency_ms,
                tags_json,
            )
        )
    except queue.Full:
        global _last_drop_warn_ts
        now = time.monotonic()
        if now - _last_drop_warn_ts > _DROP_WARN_COOLDOWN:
            _last_drop_warn_ts = now
            log.warning(
                "tokenly: write queue full (%d rows), dropping new calls — "
                "consider increasing throughput with a faster backend",
                _QUEUE_MAXSIZE,
            )

    if _config.otel:
        try:
            from . import otel

            otel.emit_span(
                provider=provider,
                model=model,
                input_tokens=clamped["input_tokens"],
                output_tokens=clamped["output_tokens"],
                cache_read_tokens=clamped["cache_read_tokens"],
                cache_write_tokens=clamped["cache_write_tokens"],
                cost_usd=cost,
                latency_ms=latency_ms,
            )
        except Exception as e:
            log.warning("tokenly: otel bridge error: %s", e)

    # In-memory rolling counter; O(1), no DB round trip on the hot path.
    spent = _budget_tick(cost) if (
        _config.budget_usd_day is not None or _config.warn_usd_day is not None
    ) else 0.0

    if _config.warn_usd_day is not None and spent >= _config.warn_usd_day:
        print(
            f"tokenly: daily spend ${spent:.2f} passed warn threshold "
            f"${_config.warn_usd_day:.2f}",
            file=sys.stderr,
        )

    if _config.budget_usd_day is not None and spent >= _config.budget_usd_day:
        raise BudgetExceeded(
            f"Daily budget ${_config.budget_usd_day:.2f} exceeded (spent ${spent:.2f})"
        )

    return cost


# ────────────────────────────────────────────────────────────────────────
# Public: configure / init
# ────────────────────────────────────────────────────────────────────────

def configure(
    db_url: str | None = None,
    db_path: str | Path | None = None,
    budget_usd_day: float | None = None,
    warn_usd_day: float | None = None,
    tags: dict[str, Any] | None = None,
    otel: bool | None = None,
) -> None:
    """Configure tokenly without initializing patches.

    Priority for storage location:
        1. db_url kwarg   (e.g. "postgresql://user:pass@host/db")
        2. db_path kwarg  (legacy, sqlite only — wrapped into sqlite://)
        3. TOKENLY_DB_URL env
        4. TOKENLY_DB env (legacy)
        5. default ~/.tokenly/log.db

    Pass `otel=True` to emit OpenTelemetry GenAI spans for every call
    (requires `pip install tokenly[otel]`).
    """
    if db_url is not None and not isinstance(db_url, str):
        raise TypeError(
            f"tokenly: db_url must be a str, got {type(db_url).__name__}"
        )
    if otel is not None and not isinstance(otel, bool):
        raise TypeError(
            f"tokenly: otel must be a bool, got {type(otel).__name__}"
        )

    resolved = resolve_url(db_url=db_url, db_path=db_path)
    _config.db_url = resolved

    if budget_usd_day is not None:
        _config.budget_usd_day = _validate_number(budget_usd_day, "budget_usd_day")
    else:
        env_budget = _parse_env_float("TOKENLY_DAILY_BUDGET")
        if env_budget is not None:
            _config.budget_usd_day = env_budget

    if warn_usd_day is not None:
        _config.warn_usd_day = _validate_number(warn_usd_day, "warn_usd_day")
    else:
        env_warn = _parse_env_float("TOKENLY_DAILY_WARN")
        if env_warn is not None:
            _config.warn_usd_day = env_warn

    if tags is not None:
        _config.tags = dict(_validate_tags(tags) or {})

    if otel is not None:
        _config.otel = bool(otel)
    elif os.environ.get("TOKENLY_OTEL") in ("1", "true", "TRUE", "yes"):
        _config.otel = True


def init(
    db_url: str | None = None,
    db_path: str | Path | None = None,
    budget_usd_day: float | None = None,
    warn_usd_day: float | None = None,
    tags: dict[str, Any] | None = None,
    otel: bool | None = None,
) -> None:
    """Initialize tokenly. Call once at app startup — additional calls are
    idempotent and only update config.

    Detects installed provider SDKs and patches them.

    Storage is SQLite by default. Pass db_url to use MySQL or Postgres:
        tokenly.init(db_url="mysql://user:pass@host/dbname")
        tokenly.init(db_url="postgresql://user:pass@host/dbname")

    Pass `otel=True` to emit OpenTelemetry GenAI spans.
    """
    with _init_lock:
        prev_url = _config.db_url
        configure(
            db_url=db_url,
            db_path=db_path,
            budget_usd_day=budget_usd_day,
            warn_usd_day=warn_usd_day,
            tags=tags,
            otel=otel,
        )

        writer_alive = _writer_thread is not None and _writer_thread.is_alive()
        url_changed = _config.initialized and prev_url != _config.db_url

        if _config.initialized and writer_alive and not url_changed:
            # Already live — honor updated config but don't respawn.
            return

        # Either first init, or the writer is dead (e.g. test fixture torn
        # down), or the DB URL changed (e.g. per-test tmp_path). In all
        # cases: cycle the writer cleanly.
        if writer_alive:
            _shutdown()

        # Open once to validate connection + ensure schema exists.
        backend = get_backend(_config.db_url)
        try:
            _ = backend.conn
        finally:
            backend.close()

        _start_writer()
        _register_atexit_once()
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
