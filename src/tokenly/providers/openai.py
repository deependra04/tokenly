"""Patch openai SDK (v1.x) to record every chat completion (incl. streaming)."""
from __future__ import annotations

import logging
import time
from typing import Any

from ..core import track

log = logging.getLogger("tokenly.openai")


def _extract_usage(response_or_chunk: Any) -> dict[str, int]:
    """Pull token usage from an OpenAI response or streaming chunk."""
    usage = getattr(response_or_chunk, "usage", None)
    if usage is None and isinstance(response_or_chunk, dict):
        usage = response_or_chunk.get("usage")
    if usage is None:
        return {}

    def _get(obj, name, default=0):
        if hasattr(obj, name):
            return getattr(obj, name) or default
        if isinstance(obj, dict):
            return obj.get(name, default) or default
        return default

    input_tokens = _get(usage, "prompt_tokens", 0)
    output_tokens = _get(usage, "completion_tokens", 0)

    cache_read = 0
    details = _get(usage, "prompt_tokens_details", None)
    if details:
        cache_read = _get(details, "cached_tokens", 0)
        input_tokens = max(0, input_tokens - cache_read)

    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cache_read_tokens": int(cache_read),
        "cache_write_tokens": 0,
    }


def _extract_model(response_or_chunk: Any, kwargs: dict) -> str:
    model = kwargs.get("model") or getattr(response_or_chunk, "model", None)
    return str(model) if model else "unknown"


def _force_stream_usage(kwargs: dict) -> dict:
    """Ensure usage is included in the final streaming chunk."""
    opts = dict(kwargs.get("stream_options") or {})
    opts.setdefault("include_usage", True)
    kwargs["stream_options"] = opts
    return kwargs


class _StreamTracker:
    """Iterator proxy that records usage once the stream finishes."""

    def __init__(self, stream, kwargs: dict, start: float):
        self._stream = stream
        self._kwargs = kwargs
        self._start = start
        self._last_chunk = None
        self._recorded = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self._stream)
        except StopIteration:
            self._record()
            raise
        self._last_chunk = chunk
        return chunk

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
        # Catch the case where the caller breaks out of the iterator or
        # lets it be GC'd without ever calling __exit__/close.
        try:
            self._record()
        except Exception:
            pass

    def _record(self):
        if self._recorded:
            return
        self._recorded = True
        latency_ms = int((time.perf_counter() - self._start) * 1000)
        try:
            usage = _extract_usage(self._last_chunk) if self._last_chunk else {}
            if usage:
                track(
                    provider="openai",
                    model=_extract_model(self._last_chunk, self._kwargs),
                    latency_ms=latency_ms,
                    **usage,
                )
        except Exception as e:
            log.warning("tokenly: openai stream tracking failed: %s", e)


class _AsyncStreamTracker:
    def __init__(self, stream, kwargs: dict, start: float):
        self._stream = stream
        self._kwargs = kwargs
        self._start = start
        self._last_chunk = None
        self._recorded = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self._stream.__anext__()
        except StopAsyncIteration:
            self._record()
            raise
        self._last_chunk = chunk
        return chunk

    def __getattr__(self, name):
        return getattr(self._stream, name)

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *exc):
        try:
            return await self._stream.__aexit__(*exc)
        finally:
            self._record()

    async def close(self):
        try:
            await self._stream.close()
        finally:
            self._record()

    def __del__(self):
        try:
            self._record()
        except Exception:
            pass

    def _record(self):
        if self._recorded:
            return
        self._recorded = True
        latency_ms = int((time.perf_counter() - self._start) * 1000)
        try:
            usage = _extract_usage(self._last_chunk) if self._last_chunk else {}
            if usage:
                track(
                    provider="openai",
                    model=_extract_model(self._last_chunk, self._kwargs),
                    latency_ms=latency_ms,
                    **usage,
                )
        except Exception as e:
            log.warning("tokenly: openai async stream tracking failed: %s", e)


def patch() -> None:
    """Monkey-patch openai.resources.chat.completions.Completions.create."""
    try:
        from openai.resources.chat import completions as _cc
    except Exception as e:
        log.warning("tokenly: openai SDK shape unrecognized: %s", e)
        return

    sync_cls = getattr(_cc, "Completions", None)
    async_cls = getattr(_cc, "AsyncCompletions", None)

    if sync_cls and not getattr(sync_cls.create, "__tokenly_patched__", False):
        original = sync_cls.create

        def wrapped(self, *args, **kwargs):
            is_stream = bool(kwargs.get("stream"))
            if is_stream:
                kwargs = _force_stream_usage(kwargs)
            start = time.perf_counter()
            response = original(self, *args, **kwargs)
            if is_stream:
                return _StreamTracker(response, kwargs, start)
            latency_ms = int((time.perf_counter() - start) * 1000)
            try:
                usage = _extract_usage(response)
                if usage:
                    track(
                        provider="openai",
                        model=_extract_model(response, kwargs),
                        latency_ms=latency_ms,
                        **usage,
                    )
            except Exception as e:
                log.warning("tokenly: openai tracking failed: %s", e)
            return response

        wrapped.__tokenly_patched__ = True
        sync_cls.create = wrapped

    if async_cls and not getattr(async_cls.create, "__tokenly_patched__", False):
        original_async = async_cls.create

        async def wrapped_async(self, *args, **kwargs):
            is_stream = bool(kwargs.get("stream"))
            if is_stream:
                kwargs = _force_stream_usage(kwargs)
            start = time.perf_counter()
            response = await original_async(self, *args, **kwargs)
            if is_stream:
                return _AsyncStreamTracker(response, kwargs, start)
            latency_ms = int((time.perf_counter() - start) * 1000)
            try:
                usage = _extract_usage(response)
                if usage:
                    track(
                        provider="openai",
                        model=_extract_model(response, kwargs),
                        latency_ms=latency_ms,
                        **usage,
                    )
            except Exception as e:
                log.warning("tokenly: openai async tracking failed: %s", e)
            return response

        wrapped_async.__tokenly_patched__ = True
        async_cls.create = wrapped_async
