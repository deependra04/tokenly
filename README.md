# tokenly

[![CI](https://github.com/deependra04/tokenly/actions/workflows/ci.yml/badge.svg)](https://github.com/deependra04/tokenly/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://pypi.org/project/tokenly/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.2.2-orange)](https://github.com/deependra04/tokenly/releases)

> One line to track every AI API cost. Sentry for AI costs. No proxy, no account, free forever.

```python
import tokenly
tokenly.init()
```

That's it. Now every OpenAI / Anthropic / Google call you make is logged — tokens, cost, latency, cache hits — to a local SQLite file.

```
$ tokenly stats

  tokenly · Today
  ────────────────────────────────────────────────────
  Spend                    $4.21
  Calls                       89
  Input               1,240,500 tokens
  Output                210,400 tokens
  Cache read             87,200 tokens
  Avg latency            842 ms
```

## Why

- Your monthly AI bill came back at $847 and you have no idea which feature caused it.
- Your bill swings 2-3× every quarter for no reason you can explain.
- Every existing tool wants you to change your base URL, run a proxy, or create an account.
- tokenly is a *tracker*, not a gateway. One line, zero config, local first.

## Install

```bash
pip install tokenly
```

Python 3.10+. Zero runtime dependencies.

## Use it

```python
import tokenly
tokenly.init()

import openai
client = openai.OpenAI()
client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "hi"}],
)
```

Then any time:

```bash
tokenly stats              # today
tokenly stats --week       # last 7 days
tokenly stats --month      # this month
tokenly stats --by=model   # group by model
tokenly tail               # live stream
tokenly export > calls.csv
tokenly doctor             # diagnose setup
```

## Tag calls by user / feature

```python
tokenly.configure(tags={"user": "alice", "feature": "chat"})
```

Then:

```bash
tokenly stats --by=tag.user
tokenly stats --by=tag.feature
```

## Budget alerts

```bash
export TOKENLY_DAILY_BUDGET=10   # raise BudgetExceeded when spend hits $10/day
export TOKENLY_DAILY_WARN=5      # warn at $5/day, keep going
```

Or in code:

```python
tokenly.init(budget_usd_day=10, warn_usd_day=5)
```

## Works with

| Provider | Tracks |
|---|---|
| OpenAI | prompt / completion tokens, cached tokens, cost |
| Anthropic | input / output tokens, cache read, cache write, cost |
| Google Gemini | prompt / output tokens, cached content tokens, cost |
| DeepSeek, xAI, Mistral, Cohere | via pricing DB; patches coming |

Because tokenly patches the underlying SDKs, **LangChain, LlamaIndex, and any other framework built on these SDKs work automatically** — no integration needed. See `examples/langchain_example.py` and `examples/llamaindex_example.py`.

## OpenTelemetry GenAI export (optional)

Emit an OpenTelemetry span per tracked call, following the [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) (`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`). That means tokenly plugs straight into Grafana, Datadog, Honeycomb, Jaeger, or any OTel-compatible backend — no extra integration.

```bash
pip install tokenly[otel]
```

```python
import tokenly
tokenly.init(otel=True)   # or: export TOKENLY_OTEL=1
```

Span `start_time` is reconstructed from the measured latency so backends see a span that actually covers the model call, not a zero-width marker. The GenAI semconv is still experimental upstream — we track the latest and will bump as it stabilizes.

## Where is the data?

By default: `~/.tokenly/log.db` — a single SQLite file. One table, ten columns. Move it, query it, back it up, delete it. It's yours.

### Pick any backend

SQLite is the default and needs nothing. For a team setup, point tokenly at your own MySQL or Postgres:

```bash
# One of these:
export TOKENLY_DB_URL="sqlite:///~/.tokenly/log.db"                 # default
export TOKENLY_DB_URL="mysql://user:pass@host:3306/tokenly"         # pip install tokenly[mysql]
export TOKENLY_DB_URL="postgresql://user:pass@host:5432/tokenly"    # pip install tokenly[postgres]
```

Or in code:

```python
tokenly.init(db_url="postgresql://user:pass@db.internal/tokenly")
```

The schema is created automatically on first connect. The legacy `TOKENLY_DB=/path/to.db` env var still works (treated as a SQLite path).

## Local dashboard

```bash
tokenly dashboard
```

Boots a local, read-only web dashboard on `http://127.0.0.1:8787` (auto-picks a free port if that's taken) and opens your browser. Spend cards, cost-by-model bars, cost-over-time line chart, and a live table of recent calls. Tabs for Today / Week / Month / All. Refreshes every 5 seconds.

Stdlib HTTP server, no JS framework, Chart.js via CDN. Stays zero-dep. Pass `--no-open` for headless, `--host 0.0.0.0` to expose on your LAN (no auth — only do this on trusted networks).

### Dashboard security

- Binds `127.0.0.1` by default — reachable only from the same machine.
- `--host 0.0.0.0` (or `::`) prints a yellow warning at startup: no authentication, read-only, and reachable by anyone on your network. Run it behind a reverse proxy with auth before exposing it publicly.
- Query params are validated: `/api/timeseries?bucket=` must be a positive int between 60 and 86 400 seconds; `/api/recent?limit=` must be in `[1, 1000]`. Bad input → HTTP 400 with a JSON error, no crashes, no resource exhaustion.
- Tag keys in `/api/by-tag?key=...` are sanitized against an identifier allowlist so SQL injection is off the table.

## Concurrency & shutdown

- `tokenly.init()` is **thread-safe and idempotent.** Call it from any thread, any number of times — a module-level lock prevents duplicate writer threads or re-patched SDKs. If the configured DB URL changes across calls the writer restarts against the new URL.
- **Writes never block your API call.** Every tracked row goes into a bounded in-memory queue (`maxsize=10 000`) drained by a background thread. Past the queue limit, new rows are dropped with a rate-limited warning — the caller still returns normally.
- **Shutdown is handled for you.** An `atexit` hook flushes the queue and joins the writer with a 5 s timeout. Rows in flight at interpreter exit are persisted cleanly.
- For workers about to be **SIGKILLed** (container teardown, cron timeout), call `tokenly.flush(timeout=5.0)` explicitly to force a drain + commit before you exit.

```python
import tokenly
tokenly.init()
...
tokenly.flush()   # block until everything is on disk
```

## Production notes

- **Batched writes.** The writer coalesces up to 100 rows (or 500 ms) into one transaction — 1 000 calls turn into ~10 commits, not 1 000. Trades ≤500 ms of observability latency for dramatically lower disk I/O.
- **Budget check is O(1).** Daily budget / warn thresholds are tracked in an in-memory counter seeded from the DB at startup and reset on UTC day rollover. No per-call SQL query.
- **Pricing auto-reloads.** Tokenly compares `pricing.json` mtime on every lookup; the weekly `sync_pricing.py` cron doesn't need a process restart.
- **SQLite WAL.** Default backend runs in WAL mode with `wal_autocheckpoint=1000` and `synchronous=NORMAL`. Back up with `sqlite3 ~/.tokenly/log.db ".backup /path/to/backup.db"` — safe while the process is writing.
- **Tested Python versions.** 3.10, 3.11, 3.12, 3.13. CI matrix runs the full test suite on all four.
- **Zero deps on the default path.** The SQLite + OpenAI/Anthropic/Google install is stdlib only. MySQL / Postgres / OTel are opt-in extras.
- **Overhead per call.** Measured at ~35 µs on the hot path (token clamp + cost lookup + queue put), well under 0.1 % of even a 20 ms model call.

## Troubleshooting

- **`tokenly doctor`** — one-shot diagnostic: tokenly version, resolved DB URL *(password-masked)*, backend connect status, which provider SDKs are installed, which optional DB drivers are available, and the values of the `TOKENLY_*` env vars. Start here for any setup issue.
- **"no pricing for foo/bar" warning** — that model isn't in `pricing.json` yet. The call is still logged at `$0`; open a PR with the rate.
- **Dashboard port already in use** — tokenly auto-falls back to the next free port starting from `8787` and prints the chosen URL. Pass `--port N` to pin one explicitly.
- **Switching DB URLs** — just call `tokenly.init(db_url="...")` again. The writer restarts cleanly against the new backend. Nothing in the old DB moves; both files remain on disk.
- **Logs leaking passwords?** They shouldn't. `doctor`, `configure()`, and internal warnings all mask DB passwords via `urllib.parse`. If you see an unmasked URL in our output, please file an issue.

## vs other tools

| | tokenly | LiteLLM | Helicone | Langfuse |
|---|---|---|---|---|
| One-line setup | ✓ | ✗ | ✗ | ✗ |
| Requires URL change | ✗ | ✓ | ✓ | ✗ |
| Needs account | ✗ | ✗ | ✓ | ✓ |
| Local-first | ✓ | ~ | ✗ | ~ |
| Gateway / routing | ✗ | ✓ | ✓ | ✗ |
| Pure cost tracking | ✓ | ~ | ~ | ~ |
| Zero runtime deps | ✓ | ✗ | ✗ | ✗ |

tokenly is tracking-only by design. If you want routing, fallbacks, or an auth proxy, use LiteLLM or Portkey. If you just want to know what you're spending, use tokenly.

## Roadmap

- [x] OpenAI, Anthropic, Google auto-patch
- [x] CLI: stats, tail, export, reset, doctor
- [x] Tags and budget alerts
- [x] Streaming-response support (OpenAI, Anthropic)
- [x] Multi-DB backend: SQLite (default), MySQL, Postgres
- [x] Local web dashboard (`tokenly dashboard`)
- [x] OpenTelemetry GenAI export (`pip install tokenly[otel]`)
- [x] Weekly auto-updated pricing DB
- [ ] Node / TypeScript SDK (same storage)

## License

MIT © 2026 Deependra Vishwakarma.

Pricing numbers are best-effort; verify with the provider before basing decisions on them. Unknown models log with $0 cost; please PR them in `src/tokenly/pricing.json`.
