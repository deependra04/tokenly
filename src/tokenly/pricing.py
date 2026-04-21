"""Pricing lookup and cost calculation."""
from __future__ import annotations

import json
from pathlib import Path

_PRICING_FILE = Path(__file__).parent / "pricing.json"
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        with _PRICING_FILE.open() as f:
            _cache = json.load(f)
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
    """Compute USD cost for a call. Returns 0.0 for unknown models."""
    rates = get_rates(provider, model)
    if rates is None:
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
