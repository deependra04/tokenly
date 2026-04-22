"""Local read-only dashboard for tokenly. Stdlib only — `http.server` +
inline HTML page that loads Chart.js from a CDN at runtime.

    tokenly dashboard                 # bind 127.0.0.1:8787, open browser
    tokenly dashboard --port 9000
    tokenly dashboard --no-open
    tokenly dashboard --host 0.0.0.0  # opt-in, local network (no auth)

Never writes. Every endpoint reads through the same Backend abstraction used
by the CLI, so sqlite / mysql / postgres all work identically.
"""
from __future__ import annotations

import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .backends import Backend, get_backend, resolve_url
from .backends.base import last_n_days_epoch, start_of_day_epoch, start_of_month_epoch

log = logging.getLogger("tokenly.dashboard")

_WINDOWS: dict[str, Any] = {
    "today": start_of_day_epoch,
    "week": lambda: last_n_days_epoch(7),
    "month": start_of_month_epoch,
    "all": lambda: None,
}


def _since_ts(window: str) -> float | None:
    fn = _WINDOWS.get(window, start_of_day_epoch)
    val = fn()
    return None if val is None else float(val)


def _bucket_for(window: str) -> int:
    """Pick a reasonable bucket size per window so the chart has ~30-60 points."""
    return {"today": 3600, "week": 3600 * 6, "month": 86400, "all": 86400 * 7}.get(
        window, 3600
    )


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    # Populated by serve() before the server starts accepting connections.
    backend: Backend
    db_url: str

    # Silence the per-request stderr spam.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _send_html(self, body: str) -> None:
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _query(self) -> dict[str, str]:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items() if v}

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route()
        except Exception as e:
            log.exception("dashboard error")
            self._send_json({"error": str(e)}, status=500)

    def _route(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(_PAGE_HTML)
            return
        if path == "/api/meta":
            self._api_meta()
            return
        if path == "/api/totals":
            self._api_totals()
            return
        if path == "/api/by-model":
            self._api_group("model")
            return
        if path == "/api/by-provider":
            self._api_group("provider")
            return
        if path == "/api/by-tag":
            key = self._query().get("key", "")
            self._api_group(f"tag.{key}" if key else "model")
            return
        if path == "/api/timeseries":
            self._api_timeseries()
            return
        if path == "/api/recent":
            self._api_recent()
            return
        self._send_json({"error": "not found", "path": path}, status=404)

    # ── endpoints ────────────────────────────────────────────────────
    def _api_meta(self) -> None:
        from . import __version__

        self._send_json(
            {
                "version": __version__,
                "backend": self.backend.describe(),
                "db_url": self.db_url,
            }
        )

    def _api_totals(self) -> None:
        window = self._query().get("window", "today")
        since = _since_ts(window)
        calls, in_t, out_t, cr, cw, cost, avg_lat = self.backend.totals(since)
        self._send_json(
            {
                "window": window,
                "calls": int(calls),
                "input_tokens": int(in_t),
                "output_tokens": int(out_t),
                "cache_read_tokens": int(cr),
                "cache_write_tokens": int(cw),
                "cost_usd": float(cost),
                "avg_latency_ms": float(avg_lat),
            }
        )

    def _api_group(self, field: str) -> None:
        q = self._query()
        window = q.get("window", "today")
        try:
            limit = max(1, min(int(q.get("limit", "10")), 100))
        except ValueError:
            limit = 10
        since = _since_ts(window)
        try:
            rows = self.backend.group_by(field, since, limit=limit)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
            return
        self._send_json(
            {
                "window": window,
                "field": field,
                "rows": [
                    {"key": r[0], "cost_usd": float(r[1]), "calls": int(r[2])}
                    for r in rows
                ],
            }
        )

    def _api_timeseries(self) -> None:
        q = self._query()
        window = q.get("window", "today")
        since = _since_ts(window)
        bucket = _bucket_for(window)
        # Explicit overrides are clamped to sane ranges — blocks `?bucket=0`
        # (SQL division by zero) and `?bucket=-1` / `?since=abc`.
        if "bucket" in q:
            try:
                bucket = int(q["bucket"])
            except (TypeError, ValueError):
                self._send_json({"error": "bucket must be an integer"}, status=400)
                return
            if bucket < 60 or bucket > 86_400:
                self._send_json(
                    {"error": "bucket must be between 60 and 86400 seconds"},
                    status=400,
                )
                return
        if "since" in q:
            try:
                since = float(q["since"])
            except (TypeError, ValueError):
                self._send_json({"error": "since must be a number"}, status=400)
                return
            if since < 0:
                self._send_json({"error": "since must be >= 0"}, status=400)
                return
        try:
            series = self.backend.time_series(since, bucket)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)
            return
        self._send_json(
            {
                "window": window,
                "bucket_seconds": bucket,
                "points": [
                    {"ts": p[0], "calls": p[1], "cost_usd": p[2]} for p in series
                ],
            }
        )

    def _api_recent(self) -> None:
        q = self._query()
        raw = q.get("limit", "50")
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            self._send_json({"error": "limit must be an integer"}, status=400)
            return
        if limit < 1 or limit > 1000:
            self._send_json(
                {"error": "limit must be between 1 and 1000"}, status=400
            )
            return
        rows = self.backend.recent_calls(limit=limit)
        self._send_json(
            {
                "rows": [
                    {
                        "id": int(r[0]),
                        "ts": float(r[1]),
                        "provider": r[2],
                        "model": r[3],
                        "input_tokens": int(r[4]),
                        "output_tokens": int(r[5]),
                        "cache_read_tokens": int(r[6]),
                        "cost_usd": float(r[7]),
                        "latency_ms": int(r[8]),
                    }
                    for r in rows
                ]
            }
        )


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def build_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    db_url: str | None = None,
) -> tuple[_Server, str, int]:
    """Construct a server but don't start it. Returns (server, host, bound_port).

    If `port` is taken (or 0), the OS assigns one.
    """
    url = resolve_url(db_url=db_url)
    backend = get_backend(url)
    # Force the schema to exist so the first request doesn't race the DDL.
    _ = backend.conn

    handler_cls = type(
        "_BoundHandler",
        (_Handler,),
        {"backend": backend, "db_url": url},
    )
    try:
        server = _Server((host, port), handler_cls)
    except OSError:
        server = _Server((host, 0), handler_cls)
    return server, host, server.server_port


def serve(
    host: str = "127.0.0.1",
    port: int = 8787,
    db_url: str | None = None,
    open_browser: bool = True,
) -> None:
    """Run the dashboard until Ctrl-C."""
    server, bound_host, bound_port = build_server(host=host, port=port, db_url=db_url)
    url = f"http://{bound_host}:{bound_port}"
    print(f"  tokenly dashboard → {url}   (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("\n  stopping…")
    finally:
        # Both calls can raise OSError if the socket is already torn down
        # (e.g. a second SIGINT). We just want to exit cleanly.
        try:
            server.shutdown()
        except OSError:
            pass
        try:
            server.server_close()
        except OSError:
            pass


# ── embedded HTML ────────────────────────────────────────────────────
_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>tokenly · dashboard</title>
<style>
  :root {
    --bg: #0b0d10;
    --panel: #12161b;
    --border: #1f2630;
    --text: #e7edf3;
    --muted: #7f8a97;
    --accent: #6ee7b7;
    --accent2: #60a5fa;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f7f8fa;
      --panel: #ffffff;
      --border: #e3e7ee;
      --text: #0f1419;
      --muted: #5c6673;
      --accent: #047857;
      --accent2: #1d4ed8;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); }
  body { font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  header { display: flex; align-items: center; gap: 16px; padding: 20px 28px; border-bottom: 1px solid var(--border); }
  header h1 { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: -0.01em; }
  header .meta { color: var(--muted); font-size: 12px; }
  .tabs { margin-left: auto; display: flex; gap: 4px; }
  .tabs button {
    background: transparent; color: var(--muted); border: 1px solid var(--border); padding: 6px 12px;
    border-radius: 999px; font-size: 12px; cursor: pointer; font-family: inherit;
  }
  .tabs button.active { color: var(--text); border-color: var(--text); }
  main { padding: 24px 28px; max-width: 1200px; margin: 0 auto; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .card { background: var(--panel); border: 1px solid var(--border); padding: 16px; border-radius: 10px; }
  .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
  .card .value { font-size: 24px; font-weight: 600; margin-top: 6px; letter-spacing: -0.01em; }
  .card .sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 24px; }
  @media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .panel h2 { margin: 0 0 12px 0; font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  canvas { max-height: 260px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  footer { color: var(--muted); font-size: 12px; text-align: center; padding: 24px; }
</style>
</head>
<body>
<header>
  <h1>tokenly</h1>
  <div class="meta" id="meta">loading…</div>
  <div class="tabs">
    <button data-w="today" class="active">Today</button>
    <button data-w="week">Week</button>
    <button data-w="month">Month</button>
    <button data-w="all">All</button>
  </div>
</header>
<main>
  <section class="cards" id="cards"></section>
  <section class="grid2">
    <div class="panel"><h2>Cost by model</h2><canvas id="byModel"></canvas></div>
    <div class="panel"><h2>Cost over time</h2><canvas id="series"></canvas></div>
  </section>
  <section class="panel">
    <h2>Recent calls</h2>
    <table>
      <thead><tr>
        <th>Time</th><th>Provider</th><th>Model</th>
        <th class="num">Input</th><th class="num">Output</th><th class="num">Cache</th>
        <th class="num">Cost</th><th class="num">Latency</th>
      </tr></thead>
      <tbody id="recent"></tbody>
    </table>
  </section>
</main>
<footer>tokenly · local dashboard · read-only</footer>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
(() => {
  const fmtUSD = n => n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
  const fmtInt = n => n.toLocaleString();
  const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString();
  const fmtDate = ts => new Date(ts * 1000).toLocaleString();
  const fmtMs = n => n >= 1000 ? (n / 1000).toFixed(1) + ' s' : Math.round(n) + ' ms';
  let window_ = 'today';
  let charts = {};

  async function fetchJSON(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(r.status + ' ' + path);
    return r.json();
  }

  function setCards(totals) {
    const el = document.getElementById('cards');
    const cards = [
      { label: 'Spend', value: fmtUSD(totals.cost_usd), sub: `${fmtInt(totals.calls)} calls` },
      { label: 'Input tokens', value: fmtInt(totals.input_tokens) },
      { label: 'Output tokens', value: fmtInt(totals.output_tokens) },
      { label: 'Cache read', value: fmtInt(totals.cache_read_tokens), sub: 'tokens' },
      { label: 'Avg latency', value: fmtMs(totals.avg_latency_ms) },
    ];
    el.innerHTML = cards.map(c => `
      <div class="card">
        <div class="label"></div>
        <div class="value"></div>
        <div class="sub"></div>
      </div>`).join('');
    [...el.children].forEach((node, i) => {
      node.querySelector('.label').textContent = cards[i].label;
      node.querySelector('.value').textContent = cards[i].value;
      node.querySelector('.sub').textContent = cards[i].sub || '';
    });
  }

  function makeOrUpdate(id, type, data, opts = {}) {
    const ctx = document.getElementById(id).getContext('2d');
    if (charts[id]) { charts[id].data = data; Object.assign(charts[id].options, opts); charts[id].update(); return; }
    charts[id] = new Chart(ctx, { type, data, options: { responsive: true, maintainAspectRatio: false, ...opts } });
  }

  function setByModel(resp) {
    const rows = resp.rows.slice(0, 10);
    makeOrUpdate('byModel', 'bar', {
      labels: rows.map(r => r.key || '(unknown)'),
      datasets: [{ label: 'cost (USD)', data: rows.map(r => r.cost_usd), backgroundColor: '#60a5fa' }],
    }, { indexAxis: 'y', plugins: { legend: { display: false } } });
  }

  function setSeries(resp) {
    const pts = resp.points;
    makeOrUpdate('series', 'line', {
      labels: pts.map(p => fmtTime(p.ts)),
      datasets: [{ label: 'cost (USD)', data: pts.map(p => p.cost_usd), borderColor: '#6ee7b7', backgroundColor: '#6ee7b733', tension: 0.25, fill: true }],
    }, { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } });
  }

  function setRecent(resp) {
    const tbody = document.getElementById('recent');
    tbody.innerHTML = '';
    for (const r of resp.rows) {
      const tr = document.createElement('tr');
      const cells = [
        fmtDate(r.ts),
        r.provider,
        r.model,
        fmtInt(r.input_tokens),
        fmtInt(r.output_tokens),
        fmtInt(r.cache_read_tokens),
        fmtUSD(r.cost_usd),
        fmtMs(r.latency_ms),
      ];
      cells.forEach((v, i) => {
        const td = document.createElement('td');
        if (i >= 3) td.className = 'num';
        td.textContent = v;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
  }

  async function refresh() {
    try {
      const [totals, byModel, series, recent] = await Promise.all([
        fetchJSON(`/api/totals?window=${window_}`),
        fetchJSON(`/api/by-model?window=${window_}&limit=10`),
        fetchJSON(`/api/timeseries?window=${window_}`),
        fetchJSON('/api/recent?limit=30'),
      ]);
      setCards(totals);
      setByModel(byModel);
      setSeries(series);
      setRecent(recent);
    } catch (e) {
      console.error(e);
    }
  }

  async function init() {
    try {
      const meta = await fetchJSON('/api/meta');
      document.getElementById('meta').textContent = `v${meta.version} · ${meta.backend}`;
    } catch {}
    document.querySelectorAll('.tabs button').forEach(b => {
      b.addEventListener('click', () => {
        document.querySelectorAll('.tabs button').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        window_ = b.dataset.w;
        refresh();
      });
    });
    refresh();
    setInterval(refresh, 5000);
  }
  init();
})();
</script>
</body>
</html>
"""
