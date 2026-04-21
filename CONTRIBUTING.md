# Contributing to tokenly

Thanks for your interest! tokenly aims to stay small, zero-dep, and local-first. Contributions that preserve those properties are warmly welcomed.

## Ground rules

- **Zero runtime dependencies.** Stdlib only in `src/tokenly/*`. Optional extras (like `otel`) go in `pyproject.toml` under `[project.optional-dependencies]`.
- **Python 3.10+.** Must work on 3.10 / 3.11 / 3.12 / 3.13 — CI enforces this.
- **Tracker, not gateway.** tokenly patches SDKs passively. Proposals that proxy, route, or change the user's base URL are out of scope (use LiteLLM or Portkey).
- **Never block the caller.** All DB writes go through the background thread. If our logging fails, the user's API call must still succeed.

## Dev setup

```bash
git clone https://github.com/deependra04/tokenly.git
cd tokenly
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q
ruff check src tests
```

## Pull request checklist

- [ ] `pytest -q` passes locally.
- [ ] `ruff check src tests` passes.
- [ ] Added tests for new behavior (`tests/test_*.py`).
- [ ] If you added a model, updated `src/tokenly/pricing.json` with input / output / (optional) cache_read / cache_write rates, cited the provider pricing page in the PR description.
- [ ] Updated `CHANGELOG.md` under `[Unreleased]`.
- [ ] Kept public API backwards-compatible — if not, flagged it in the PR.

## Adding a new provider

1. Create `src/tokenly/providers/<provider>.py` with a `patch()` function.
2. Extract usage into the normalized schema: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`.
3. Register it behind an `importlib.util.find_spec` check in `src/tokenly/__init__.py` (use the safe `_has_module` helper).
4. Add a VCR-style test in `tests/` (no live API calls in CI).

## Adding a model to `pricing.json`

Key format: `"<provider>/<model-id>"`. Values are **USD per 1M tokens**:

```json
"openai/gpt-5-mini": { "input": 0.25, "output": 2.00, "cache_read": 0.025, "cache_write": null }
```

If you omit `cache_read`, tokenly defaults to 10% of input. If you omit `cache_write`, it defaults to 125% of input. Link the official pricing page in your PR.

## Reporting bugs

Use the issue template. Include: Python version, provider + SDK version, a minimal repro, and the output of `tokenly doctor`.

## Security

Please do **not** open public issues for security reports. Email `dvip34456@gmail.com` directly.

## License

By contributing, you agree your work is released under the MIT License (see `LICENSE`).
