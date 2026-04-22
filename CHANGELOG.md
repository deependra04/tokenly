# Changelog

All notable changes to tokenly are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [SemVer](https://semver.org/).

## [Unreleased]

## [0.2.2] - 2026-04-22

A hardening release focused purely on quality — no new features. Backward
compatible with 0.2.x; drop-in upgrade.

### Added
- **Public `tokenly.flush(timeout=5.0)`** for workers facing SIGKILL or tests that need deterministic state. Drains the write queue and waits for any in-flight batch to commit.
- `atexit` hook registered at `init()` so clean shutdowns never lose the final rows.

### Changed
- **Batched writer.** The background thread now coalesces up to 100 rows (or a 500 ms window) into a single transaction, collapsing `N` commits into `~N/100`. Large bursts land in roughly the time a single write used to take.
- **Bounded write queue** (`maxsize=10_000`). Past the limit, new rows are dropped with a rate-limited warning — the caller is never blocked.
- **Budget check is O(1) on the hot path.** Replaced the per-call SQL query with an in-memory rolling counter seeded from the DB at writer startup and reset on UTC day rollover.
- `init()` restarts the writer if the DB URL changes across calls (fixes per-test tmp_path fixtures and production DB-URL rotation).
- SQLite: `PRAGMA wal_autocheckpoint=1000` and `PRAGMA synchronous=NORMAL` set at schema init — healthy WAL growth without sacrificing durability.
- Pricing table hot-reloads when `pricing.json` mtime changes; the weekly sync no longer needs a process restart.

### Fixed
- Streaming trackers record exactly once even when the caller breaks out of the iterator early or lets it be garbage-collected (`__del__` fallback + `_recorded` guard).
- Dashboard shutdown catches `SystemExit` and wraps `server.shutdown()` / `server_close()` in `try/except OSError` to survive double-shutdown.
- Malformed env vars (`TOKENLY_DAILY_BUDGET`, `TOKENLY_DAILY_WARN`) log a warning and fall back to unset instead of crashing `configure()`.
- Negative token counts passed to `track()` are clamped to 0 with a warn-once per `(provider, model)` pair; the caller's API call still succeeds.
- Unknown provider/model pairs warn once and return `$0.00`, no log spam.

### Security
- **DB URL passwords are masked** in `tokenly doctor` output, the `TOKENLY_DB_URL` env-var echo, and all internal log lines (`_mask_url` helper uses `urllib.parse`).
- **Dashboard prints a yellow warning** when bound to `0.0.0.0` or `::` reminding the operator that the server has no auth and is read-only — trusted networks only.
- **Dashboard query params are validated** with HTTP 400 responses: `/api/timeseries?bucket=` must be an int in `[60, 86400]`, `?since=` must be a non-negative number, `/api/recent?limit=` must be an int in `[1, 1000]`. Blocks `?limit=9999999` resource-exhaustion and `?bucket=0` SQL division-by-zero.
- **Pricing sync validates upstream JSON.** `scripts/sync_pricing.py` rejects non-numeric, negative, NaN, or >$5000/1M values; rejects unknown fields; skips bad entries rather than corrupting the whole table.
- **Backend reconnect on transient errors.** Each dialect carries an `_is_transient()` predicate (sqlite `OperationalError`, pymysql 2006/2013/2014, psycopg `OperationalError` / `InterfaceError`); a single stale-connection fault retries once transparently.
- **Concurrent `init()` is safe** — guarded by a module-level lock; SDK patches are idempotent via `__tokenly_patched__`.

## [0.2.0] - 2026-04-22

### Added
- **Local dashboard.** `tokenly dashboard` boots a stdlib HTTP server with a single-page web UI (Chart.js via CDN, no npm build step). Spend cards, cost-by-model bar chart, cost-over-time line chart, live table of recent calls, Today / Week / Month / All tabs, dark-mode aware. Read-only, auto-port-fallback, binds `127.0.0.1` by default.
- **OpenTelemetry GenAI export.** Opt in with `tokenly.init(otel=True)` or `export TOKENLY_OTEL=1`. Emits one span per call following the experimental GenAI semantic conventions (`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`) + `tokenly.*` attributes for cost / cache / latency. Span `start_time` reconstructed from measured latency so the span actually covers the model call. Requires `pip install tokenly[otel]`; default install stays zero-dep.
- **Weekly pricing auto-sync.** New `.github/workflows/pricing-sync.yml` cron (Mondays 12:00 UTC) runs `scripts/sync_pricing.py`, diffs tokenly's `pricing.json` against LiteLLM's MIT-licensed community pricing feed, and opens a PR when prices drift. Human review required before merge.
- **LangChain / LlamaIndex examples.** `examples/langchain_example.py` + `examples/llamaindex_example.py` — no integration code needed; tokenly patches the underlying SDKs so these frameworks are tracked automatically.
- Backend adds `time_series(since_ts, bucket_seconds)` and `recent_calls(limit)` methods, portable across sqlite / mysql / postgres.

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

[Unreleased]: https://github.com/deependra04/tokenly/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/deependra04/tokenly/compare/v0.2.0...v0.2.2
[0.2.0]: https://github.com/deependra04/tokenly/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/deependra04/tokenly/releases/tag/v0.1.0
