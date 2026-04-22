"""Dashboard smoke test: boot the threading HTTP server on an OS-assigned port,
hit each JSON endpoint, assert shape."""
from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from tokenly.backends import get_backend
from tokenly.dashboard import build_server


def _seed(url: str, n: int = 5) -> None:
    b = get_backend(url)
    try:
        now = time.time()
        for i in range(n):
            b.write_row(
                (
                    now - (n - i) * 60,
                    "openai",
                    "gpt-4o-mini" if i % 2 == 0 else "gpt-5-mini",
                    100 + i,
                    50,
                    5 if i == 0 else 0,
                    0,
                    0.001 * (i + 1),
                    100 + i,
                    json.dumps({"user": "alice" if i < 3 else "bob"}),
                )
            )
    finally:
        b.close()


@pytest.fixture
def live_server(tmp_path):
    url = f"sqlite:///{tmp_path}/log.db"
    _seed(url)
    server, host, port = build_server(host="127.0.0.1", port=0, db_url=url)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=3) as r:
        assert r.status == 200
        return json.loads(r.read())


def test_index_serves_html(live_server):
    with urllib.request.urlopen(live_server + "/", timeout=3) as r:
        body = r.read().decode()
    assert r.status == 200
    assert "<!doctype html>" in body.lower()
    assert "tokenly" in body
    assert "chart.js" in body.lower()


def test_meta_endpoint(live_server):
    data = _get(live_server, "/api/meta")
    assert "version" in data
    assert "backend" in data
    assert data["backend"].startswith("sqlite:")


def test_totals_endpoint(live_server):
    data = _get(live_server, "/api/totals?window=all")
    assert data["calls"] == 5
    assert data["cost_usd"] == pytest.approx(0.001 + 0.002 + 0.003 + 0.004 + 0.005)
    assert data["cache_read_tokens"] == 5
    assert data["window"] == "all"


def test_by_model_endpoint(live_server):
    data = _get(live_server, "/api/by-model?window=all&limit=5")
    assert data["field"] == "model"
    assert len(data["rows"]) == 2
    assert {r["key"] for r in data["rows"]} == {"gpt-4o-mini", "gpt-5-mini"}


def test_by_tag_endpoint(live_server):
    data = _get(live_server, "/api/by-tag?key=user&window=all")
    assert data["field"] == "tag.user"
    keys = {r["key"] for r in data["rows"]}
    assert "alice" in keys and "bob" in keys


def test_by_tag_rejects_bad_key(live_server):
    # "drop; ..." fails the _IDENT regex → 400
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(live_server + "/api/by-tag?key=drop;table", timeout=3)
    assert ei.value.code == 400


def test_timeseries_endpoint(live_server):
    data = _get(live_server, "/api/timeseries?window=all")
    assert data["bucket_seconds"] > 0
    assert isinstance(data["points"], list)
    for p in data["points"]:
        assert {"ts", "calls", "cost_usd"} <= p.keys()


def test_recent_endpoint(live_server):
    data = _get(live_server, "/api/recent?limit=3")
    assert len(data["rows"]) == 3
    first = data["rows"][0]
    assert {"id", "ts", "provider", "model", "cost_usd", "latency_ms"} <= first.keys()
    # ordered DESC by id
    ids = [r["id"] for r in data["rows"]]
    assert ids == sorted(ids, reverse=True)


def test_unknown_path_404(live_server):
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(live_server + "/api/does-not-exist", timeout=3)
    assert ei.value.code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/timeseries?bucket=0",
        "/api/timeseries?bucket=-1",
        "/api/timeseries?bucket=abc",
        "/api/timeseries?bucket=999999",
        "/api/timeseries?since=abc",
        "/api/timeseries?since=-5",
        "/api/recent?limit=0",
        "/api/recent?limit=-5",
        "/api/recent?limit=abc",
        "/api/recent?limit=99999999",
    ],
)
def test_query_param_validation_returns_400(live_server, path):
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(live_server + path, timeout=3)
    assert ei.value.code == 400
    body = json.loads(ei.value.read())
    assert "error" in body


def test_recent_limit_within_range_ok(live_server):
    data = _get(live_server, "/api/recent?limit=1")
    assert len(data["rows"]) == 1


def test_timeseries_bucket_explicit_ok(live_server):
    data = _get(live_server, "/api/timeseries?window=all&bucket=60")
    assert data["bucket_seconds"] == 60
