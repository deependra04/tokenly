"""Pricing lookup and cost calculation.

The pricing table is loaded once and re-read only when the file's mtime
changes — lets the weekly `sync_pricing.py` script update prices without a
process restart.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("tokenly.pricing")

_PRICING_FILE = Path(__file__).parent / "pricing.json"

_cache: dict | None = None
_cache_mtime: float | None = None

_warned_unknown: set[tuple[str, str]] = set()


def _load() -> dict:
    """Load pricing.json, reloading automatically if the file has changed."""
    global _cache, _cache_mtime
    try:
        mtime = _PRICING_FILE.stat().st_mtime
    except OSError as e:
        if _cache is not None:
            return _cache
        raise RuntimeError(f"tokenly: cannot read pricing.json: {e}") from e

    if _cache is None or _cache_mtime != mtime:
        with _PRICING_FILE.open() as f:
            _cache = json.load(f)
        _cache_mtime = mtime
    return _cache


def get_rates(provider: str, model: str) -> dict | None:
    """Return rate dict for provider/model or None if unknown."""
    data = _load()
    key = f"{provider}/{model}"
    rates = data.get(key)
    if rates and not key.startswith("_"):
        return rates
    for k, v in data.items():
        if k.startswith("_"):
            continue
        if k.startswith(f"{provider}/") and model.startswith(k.split("/", 1)[1]):
            return v
    return None


def compute_cost(
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Compute USD cost for a call. Returns 0.0 for unknown models.

    Unknown (provider, model) pairs log a one-time warning so the user sees
    it in their logs without spamming every call.
    """
    rates = get_rates(provider, model)
    if rates is None:
        key = (provider, model)
        if key not in _warned_unknown:
            _warned_unknown.add(key)
            log.warning(
                "tokenly: no pricing for %s/%s — logging with $0 cost. "
                "PR it in src/tokenly/pricing.json.",
                provider,
                model,
            )
        return 0.0

    input_price = rates["input"]
    output_price = rates["output"]
    cache_read_price = rates.get("cache_read")
    if cache_read_price is None:
        cache_read_price = input_price * 0.10
    cache_write_price = rates.get("cache_write")
    if cache_write_price is None:
        cache_write_price = input_price * 1.25

    cost = (
        input_tokens * input_price
        + output_tokens * output_price
        + cache_read_tokens * cache_read_price
        + cache_write_tokens * cache_write_price
    ) / 1_000_000.0
    return cost


def is_known(provider: str, model: str) -> bool:
    return get_rates(provider, model) is not None
