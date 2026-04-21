"""Tests for pricing.py — no network, no providers needed."""
from __future__ import annotations

from tokenly import pricing


def test_known_model_cost_openai():
    cost = pricing.compute_cost(
        provider="openai",
        model="gpt-5-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == 0.25 + 2.00


def test_known_model_cost_anthropic_with_cache():
    cost = pricing.compute_cost(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert abs(cost - (3.00 + 0.30 + 3.75)) < 1e-6


def test_unknown_model_returns_zero():
    cost = pricing.compute_cost(
        provider="openai", model="nonexistent-model", input_tokens=1000, output_tokens=1000
    )
    assert cost == 0.0


def test_is_known():
    assert pricing.is_known("openai", "gpt-5-mini")
    assert not pricing.is_known("openai", "does-not-exist")


def test_prefix_match():
    rates = pricing.get_rates("openai", "gpt-5-mini-2026-01-01")
    assert rates is not None
    assert rates["input"] == 0.25


def test_cache_read_default_10_percent():
    cost = pricing.compute_cost(
        provider="openai",
        model="gpt-5.2",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert abs(cost - 0.175) < 1e-6
