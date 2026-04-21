# Changelog

All notable changes to tokenly are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-04-22

Initial public release.

### Added
- One-line `tokenly.init()` auto-instrumentation for OpenAI, Anthropic, and Google Gemini SDKs (both legacy `google.generativeai` and new `google.genai`).
- Cache-aware token tracking: OpenAI `cached_tokens`, Anthropic `cache_read_input_tokens` / `cache_creation_input_tokens`, Google `cached_content_token_count`.
- **Streaming-response support** for OpenAI (`stream=True` — tokenly auto-forces `stream_options.include_usage=True` so the final chunk carries usage) and Anthropic (tracks input/cache on `message_start`, final output on `message_delta`).
- **Multi-DB storage.** SQLite is the default (zero-dep). Optional MySQL (`pip install tokenly[mysql]`) and PostgreSQL (`pip install tokenly[postgres]`) backends. Select via `TOKENLY_DB_URL` env or `tokenly.init(db_url=...)` using standard URL schemes (`sqlite:///...`, `mysql://...`, `postgresql://...`).
- Local SQLite log at `~/.tokenly/log.db` (override with `TOKENLY_DB` or `TOKENLY_DB_URL`), WAL mode, background writer thread — never blocks the caller.
- Static pricing DB (`pricing.json`) with 20 models at April 2026 rates.
- CLI: `stats`, `stats --week`, `stats --month`, `stats --all`, `stats --by=model|provider|tag.<key>`, `tail`, `export`, `reset`, `doctor`.
- `tokenly doctor` reports the resolved DB URL, backend connect status, installed provider SDKs, and whether optional drivers (pymysql, psycopg) are available.
- Tag calls per user / feature via `tokenly.configure(tags={...})`.
- Budget alerts via `TOKENLY_DAILY_BUDGET` (hard stop, raises `BudgetExceeded`) and `TOKENLY_DAILY_WARN` (soft).
- Zero runtime dependencies for the default SQLite path. Python 3.10 / 3.11 / 3.12 / 3.13.
- CI on GitHub Actions: ruff + pytest across all four Python versions.

[Unreleased]: https://github.com/deependra04/tokenly/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/deependra04/tokenly/releases/tag/v0.1.0
