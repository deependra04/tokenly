"""Patch anthropic SDK (v1.x) to record every messages.create (incl. streaming)."""
from __future__ import annotations

import logging
import time
from typing import Any

from ..core import track

log = logging.getLogger("tokenly.anthropic")


def _get(obj, name, default=0):
    if hasattr(obj, name):
        return getattr(obj, name) or default
    if isinstance(obj, dict):
        return obj.get(name, default) or default
    return default


def _extract_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return {}
    return {
        "input_tokens": int(_get(usage, "input_tokens", 0)),
        "output_tokens": int(_get(usage, "output_tokens", 0)),
        "cache_read_tokens": int(_get(usage, "cache_read_input_tokens", 0)),
        "cache_write_tokens": int(_get(usage, "cache_creation_input_tokens", 0)),
    }


def _extract_model(response: Any, kwargs: dict) -> str:
    model = kwargs.get("model") or getattr(response, "model", None)
    return str(model) if model else "unknown"


def _update_usage_from_event(totals: dict, event: Any) -> None:
    """Accumulate usage fields across streaming events.

    message_start: has full prompt usage + cache fields (output_tokens=0 or 1).
    message_delta: has final output_tokens.
    """
    etype = getattr(event, "type", None) or (
        event.get("type") if isinstance(event, dict) else None
    )
    if etype == "message_start":
        message = getattr(event, "message", None) or (
            event.get("message") if isinstance(event, dict) else None
        )
        if message is not None:
            u = _extract_usage(message)
            # Keep cache/input from here; output gets overwritten by delta.
            totals.update(u)
    elif etype == "message_delta":
        usage = getattr(event, "usage", None) or (
            event.get("usage") if isinstance(event, dict) else None
        )
        if usage is not None:
            out = int(_get(usage, "output_tokens", 0))
            if out:
                totals["output_tokens"] = out


class _StreamTracker:
    def __init__(self, stream, kwargs: dict, start: float):
        self._stream = stream
        self._kwargs = kwargs
        self._start = start
        self._totals: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        self._recorded = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            event = next(self._stream)
        except StopIteration:
            self._record()
            raise
        _update_usage_from_event(self._totals, event)
        return event

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            return self._stream.__exit__(*exc)
        finally:
            self._record()

    def close(self):
        try:
            self._stream.close()
        finally:
            self._record()

    def __del__(self):
        # Fire once even if the consumer breaks out of the iterator / lets
        # it be garbage-collected without calling __exit__ or close().
        try:
            self._record()
        except Exception:
            pass

    def _record(self):
        if self._recorded:
            return
        self._recorded = True
        if not any(self._totals.values()):
            return
        latency_ms = int((time.perf_counter() - self._start) * 1000)
        try:
            track(
                provider="anthropic",
                model=_extract_model(None, self._kwargs),
                latency_ms=latency_ms,
                **self._totals,
            )
        except Exception as e:
            log.warning("tokenly: anthropic stream tracking failed: %s", e)


class _AsyncStreamTracker(_StreamTracker):
    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            event = await self._stream.__anext__()
        except StopAsyncIteration:
            self._record()
            raise
        _update_usage_from_event(self._totals, event)
        return event

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *exc):
        try:
            return await self._stream.__aexit__(*exc)
        finally:
            self._record()

    async def close(self):  # type: ignore[override]
        try:
            await self._stream.close()
        finally:
            self._record()


def patch() -> None:
    try:
        from anthropic.resources import messages as _m
    except Exception as e:
        log.warning("tokenly: anthropic SDK shape unrecognized: %s", e)
        return

    sync_cls = getattr(_m, "Messages", None)
    async_cls = getattr(_m, "AsyncMessages", None)

    if sync_cls and not getattr(sync_cls.create, "__tokenly_patched__", False):
        original = sync_cls.create

        def wrapped(self, *args, **kwargs):
            is_stream = bool(kwargs.get("stream"))
            start = time.perf_counter()
            response = original(self, *args, **kwargs)
            if is_stream:
                return _StreamTracker(response, kwargs, start)
            latency_ms = int((time.perf_counter() - start) * 1000)
            try:
                usage = _extract_usage(response)
                if usage:
                    track(
                        provider="anthropic",
                        model=_extract_model(response, kwargs),
                        latency_ms=latency_ms,
                        **usage,
                    )
            except Exception as e:
                log.warning("tokenly: anthropic tracking failed: %s", e)
            return response

        wrapped.__tokenly_patched__ = True
        sync_cls.create = wrapped

    if async_cls and not getattr(async_cls.create, "__tokenly_patched__", False):
        original_async = async_cls.create

        async def wrapped_async(self, *args, **kwargs):
            is_stream = bool(kwargs.get("stream"))
            start = time.perf_counter()
            response = await original_async(self, *args, **kwargs)
            if is_stream:
                return _AsyncStreamTracker(response, kwargs, start)
            latency_ms = int((time.perf_counter() - start) * 1000)
            try:
                usage = _extract_usage(response)
                if usage:
                    track(
                        provider="anthropic",
                        model=_extract_model(response, kwargs),
                        latency_ms=latency_ms,
                        **usage,
                    )
            except Exception as e:
                log.warning("tokenly: anthropic async tracking failed: %s", e)
            return response

        wrapped_async.__tokenly_patched__ = True
        async_cls.create = wrapped_async
