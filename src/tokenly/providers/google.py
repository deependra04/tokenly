"""Patch google-genai / google-generativeai SDK to record every generate call."""
from __future__ import annotations

import logging
import time
from typing import Any

from ..core import track

log = logging.getLogger("tokenly.google")


def _extract_usage(response: Any) -> dict[str, int]:
    meta = getattr(response, "usage_metadata", None)
    if meta is None and isinstance(response, dict):
        meta = response.get("usage_metadata") or response.get("usageMetadata")
    if meta is None:
        return {}

    def _get(obj, *names, default=0):
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None:
                    return v
            if isinstance(obj, dict):
                v = obj.get(n)
                if v is not None:
                    return v
        return default

    input_tokens = _get(meta, "prompt_token_count", "promptTokenCount", default=0)
    output_tokens = _get(meta, "candidates_token_count", "candidatesTokenCount", default=0)
    cache_read = _get(meta, "cached_content_token_count", "cachedContentTokenCount", default=0)
    if cache_read:
        input_tokens = max(0, input_tokens - cache_read)

    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cache_read_tokens": int(cache_read),
        "cache_write_tokens": 0,
    }


def _extract_model(response: Any, kwargs: dict) -> str:
    model = kwargs.get("model") or getattr(response, "model_version", None)
    if not model and hasattr(response, "model"):
        model = response.model
    return str(model) if model else "unknown"


def patch() -> None:
    try:
        from google.genai import models as _m
    except Exception:
        try:
            import google.generativeai as genai  # type: ignore

            _patch_legacy_genai(genai)
            return
        except Exception as e:
            log.warning("tokenly: google SDK shape unrecognized: %s", e)
            return

    target = getattr(_m, "Models", None)
    async_target = getattr(_m, "AsyncModels", None)

    if target and not getattr(target.generate_content, "__tokenly_patched__", False):
        original = target.generate_content

        def wrapped(self, *args, **kwargs):
            start = time.perf_counter()
            response = original(self, *args, **kwargs)
            latency_ms = int((time.perf_counter() - start) * 1000)
            try:
                usage = _extract_usage(response)
                if usage:
                    track(
                        provider="google",
                        model=_extract_model(response, kwargs),
                        latency_ms=latency_ms,
                        **usage,
                    )
            except Exception as e:
                log.warning("tokenly: google tracking failed: %s", e)
            return response

        wrapped.__tokenly_patched__ = True
        target.generate_content = wrapped

    if async_target and not getattr(async_target.generate_content, "__tokenly_patched__", False):
        original_async = async_target.generate_content

        async def wrapped_async(self, *args, **kwargs):
            start = time.perf_counter()
            response = await original_async(self, *args, **kwargs)
            latency_ms = int((time.perf_counter() - start) * 1000)
            try:
                usage = _extract_usage(response)
                if usage:
                    track(
                        provider="google",
                        model=_extract_model(response, kwargs),
                        latency_ms=latency_ms,
                        **usage,
                    )
            except Exception as e:
                log.warning("tokenly: google async tracking failed: %s", e)
            return response

        wrapped_async.__tokenly_patched__ = True
        async_target.generate_content = wrapped_async


def _patch_legacy_genai(genai: Any) -> None:
    """Fallback for the older google-generativeai package."""
    model_cls = getattr(genai, "GenerativeModel", None)
    if not model_cls:
        return
    if getattr(model_cls.generate_content, "__tokenly_patched__", False):
        return

    original = model_cls.generate_content

    def wrapped(self, *args, **kwargs):
        start = time.perf_counter()
        response = original(self, *args, **kwargs)
        latency_ms = int((time.perf_counter() - start) * 1000)
        try:
            usage = _extract_usage(response)
            if usage:
                model_name = getattr(self, "model_name", "unknown")
                track(
                    provider="google",
                    model=str(model_name).replace("models/", ""),
                    latency_ms=latency_ms,
                    **usage,
                )
        except Exception as e:
            log.warning("tokenly: google legacy tracking failed: %s", e)
        return response

    wrapped.__tokenly_patched__ = True
    model_cls.generate_content = wrapped
