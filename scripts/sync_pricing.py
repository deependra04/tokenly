"""Sync tokenly's pricing.json against the LiteLLM community pricing feed.

LiteLLM publishes a well-maintained MIT-licensed JSON at
  https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
that the community updates when providers change prices. This script
cherry-picks updates for the models we already ship and writes a
proposed pricing.json + markdown summary.

Runs weekly via .github/workflows/pricing-sync.yml. Stdlib only.

Usage:
    python scripts/sync_pricing.py                   # real run
    python scripts/sync_pricing.py --dry-run         # don't write anything
    python scripts/sync_pricing.py --fixture foo.json  # use local file, not HTTP

Exit codes:
    0  sync succeeded (with or without changes).
    1  structural error: feed root is not a JSON object, or the feed is
       unreachable *and* we were given a fixture we can't read. A single
       bad entry inside an otherwise-good feed is skipped, not fatal.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

FEED_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
ROOT = Path(__file__).resolve().parent.parent
PRICING_PATH = ROOT / "src" / "tokenly" / "pricing.json"
SUMMARY_PATH = ROOT / "PRICING_SYNC_SUMMARY.md"

# Map LiteLLM's `litellm_provider` values to tokenly's provider namespace.
PROVIDER_ALIASES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "google",
    "vertex_ai-language-models": "google",
    "bedrock": "anthropic",  # claude-* on bedrock, rare in practice
    "deepseek": "deepseek",
    "xai": "xai",
    "mistral": "mistral",
    "cohere_chat": "cohere",
    "cohere": "cohere",
}

# Hard sanity limits. Providers don't publish rates outside this window in
# practice; seeing $5001/1M means the feed is wrong, and we'd rather keep
# stale but correct pricing than adopt a bogus number.
_MIN_PRICE = 0.0
_MAX_PRICE = 5000.0
_ALLOWED_FIELDS = {"input", "output", "cache_read", "cache_write"}


def fetch_feed(fixture: Path | None) -> dict:
    if fixture is not None:
        return json.loads(fixture.read_text())
    req = urllib.request.Request(
        FEED_URL, headers={"User-Agent": "tokenly-pricing-sync/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def to_per_million(usd_per_token: float | None) -> float | None:
    """LiteLLM stores USD per single token; we store USD per 1M tokens."""
    if usd_per_token is None:
        return None
    return round(float(usd_per_token) * 1_000_000, 6)


def normalize_key(litellm_key: str, litellm_entry: dict) -> str | None:
    """Map a LiteLLM entry to our "provider/model" key, if we can."""
    provider = litellm_entry.get("litellm_provider")
    alias = PROVIDER_ALIASES.get(provider)
    if alias is None:
        return None
    model = litellm_key
    for prefix in (
        f"{provider}/",
        "vertex_ai/",
        "bedrock/",
        "anthropic.",
        "gemini/",
    ):
        if model.startswith(prefix):
            model = model[len(prefix):]
    return f"{alias}/{model}"


def _validate_price(field: str, value: object) -> float | None:
    """Return a clean float price, or raise ValueError with a clear message.

    None passes through unchanged for optional cache_* fields.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number, got {type(value).__name__}")
    v = float(value)
    if v != v:  # NaN
        raise ValueError(f"{field} is NaN")
    if v < _MIN_PRICE:
        raise ValueError(f"{field} is negative ({v})")
    if v > _MAX_PRICE:
        raise ValueError(
            f"{field} is suspiciously large ({v} USD/1M tokens); "
            f"capping sync at {_MAX_PRICE}"
        )
    return v


def _validate_entry(key: str, entry: dict) -> dict:
    """Validate + normalize a proposed pricing entry. Raises ValueError
    with the key name prefixed for log clarity."""
    extra = set(entry) - _ALLOWED_FIELDS
    if extra:
        raise ValueError(f"{key}: unknown fields {sorted(extra)}")
    clean: dict[str, float | None] = {}
    for field in ("input", "output"):
        if entry.get(field) is None:
            raise ValueError(f"{key}: required field {field} missing")
        clean[field] = _validate_price(field, entry[field])
    for field in ("cache_read", "cache_write"):
        clean[field] = _validate_price(field, entry.get(field))
    return clean


def build_feed_prices(feed: dict) -> tuple[dict[str, dict], list[str]]:
    """Walk the upstream feed and return (normalized_prices, skipped_warnings).

    Entries with missing/bad fields are skipped with a warning, not fatal.
    """
    feed_prices: dict[str, dict] = {}
    skipped: list[str] = []
    for key, entry in feed.items():
        if not isinstance(entry, dict):
            continue
        norm = normalize_key(key, entry)
        if not norm:
            continue
        inp = to_per_million(entry.get("input_cost_per_token"))
        out = to_per_million(entry.get("output_cost_per_token"))
        cache_read = to_per_million(
            entry.get("cache_read_input_token_cost")
            or entry.get("input_cost_per_token_cache_hit")
        )
        cache_write = to_per_million(entry.get("cache_creation_input_token_cost"))
        proposed = {
            "input": inp,
            "output": out,
            "cache_read": cache_read,
            "cache_write": cache_write,
        }
        try:
            feed_prices[norm] = _validate_entry(norm, proposed)
        except ValueError as e:
            skipped.append(str(e))
    return feed_prices, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync tokenly pricing.json.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't modify pricing.json or write the summary",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="read feed from a local JSON file instead of HTTP",
    )
    args = parser.parse_args(argv)

    try:
        feed = fetch_feed(args.fixture)
    except Exception as e:
        if args.fixture is not None:
            print(f"sync_pricing: cannot read fixture: {e}", file=sys.stderr)
            return 1
        # Network hiccup from cron: don't fail noisily, just skip.
        print(f"sync_pricing: failed to fetch feed: {e}", file=sys.stderr)
        return 0

    if not isinstance(feed, dict):
        print("sync_pricing: feed root is not a JSON object", file=sys.stderr)
        return 1

    feed_prices, skipped = build_feed_prices(feed)
    for s in skipped:
        print(f"sync_pricing: skip {s}", file=sys.stderr)

    current: dict = json.loads(PRICING_PATH.read_text())

    changes: list[str] = []
    updated = dict(current)

    for key, ours in current.items():
        theirs = feed_prices.get(key)
        if not theirs:
            continue
        new_entry = dict(ours)
        fields_changed = []
        for field in ("input", "output", "cache_read", "cache_write"):
            old = ours.get(field)
            new = theirs.get(field)
            if new is None:
                continue
            if old is None or abs(float(old) - float(new)) / max(float(new), 1e-9) > 0.005:
                fields_changed.append(f"{field}: {old} → {new}")
                new_entry[field] = new
        if fields_changed:
            updated[key] = new_entry
            changes.append(f"- **{key}**: " + ", ".join(fields_changed))

    if not changes:
        print("sync_pricing: no pricing changes detected.")
        if not args.dry_run and SUMMARY_PATH.exists():
            SUMMARY_PATH.unlink()
        return 0

    if args.dry_run:
        print(f"sync_pricing: (dry-run) would update {len(changes)} model(s):")
        for line in changes:
            print(f"  {line}")
        return 0

    PRICING_PATH.write_text(json.dumps(updated, indent=2) + "\n")
    SUMMARY_PATH.write_text(
        "# Weekly pricing sync\n\n"
        f"Source: [{FEED_URL}]({FEED_URL}) (LiteLLM, MIT).\n\n"
        "Pricing in USD per 1M tokens.\n\n"
        "## Detected changes\n\n"
        + "\n".join(changes)
        + "\n\n"
        "_Review each line before merging. Providers occasionally restructure SKUs; "
        "LiteLLM catches most but not all of that._\n"
    )
    print(f"sync_pricing: {len(changes)} model(s) updated. See {SUMMARY_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
