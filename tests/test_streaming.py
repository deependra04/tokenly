"""Smoke test for the OpenAI streaming tracker using a fake stream."""
from __future__ import annotations

import time
from types import SimpleNamespace


def _fake_usage(prompt=1000, completion=200, cached=50):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def _fake_stream_chunks():
    # Content chunks first, final chunk carries usage.
    yield SimpleNamespace(model="gpt-4o-mini", choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))], usage=None)
    yield SimpleNamespace(model="gpt-4o-mini", choices=[SimpleNamespace(delta=SimpleNamespace(content=" there"))], usage=None)
    yield SimpleNamespace(model="gpt-4o-mini", choices=[], usage=_fake_usage())


def test_stream_tracker_records_on_end(tmp_path, monkeypatch):
    from tokenly import configure, init
    from tokenly.backends import get_backend
    from tokenly.providers.openai import _StreamTracker

    monkeypatch.delenv("TOKENLY_DB_URL", raising=False)
    configure(db_url=f"sqlite:///{tmp_path}/log.db")
    init(db_url=f"sqlite:///{tmp_path}/log.db")

    tracker = _StreamTracker(
        iter(_fake_stream_chunks()),
        kwargs={"model": "gpt-4o-mini"},
        start=time.perf_counter(),
    )
    # Iterate like a user would.
    consumed = list(tracker)
    assert len(consumed) == 3

    # Writer is async; give it a moment.
    for _ in range(20):
        time.sleep(0.05)
        b = get_backend(f"sqlite:///{tmp_path}/log.db")
        try:
            totals = b.totals(since_ts=None)
        finally:
            b.close()
        if totals[0]:
            break

    calls, in_tok, out_tok, cr, cw, cost, _ = totals
    assert calls == 1
    # cache_read split out of prompt_tokens
    assert in_tok == 950
    assert out_tok == 200
    assert cr == 50


def test_stream_tracker_close_idempotent(tmp_path, monkeypatch):
    from tokenly import init
    from tokenly.providers.openai import _StreamTracker

    init(db_url=f"sqlite:///{tmp_path}/log.db")

    class FakeStream:
        def __init__(self, chunks):
            self._it = iter(chunks)

        def __iter__(self):
            return self._it

        def __next__(self):
            return next(self._it)

        def close(self):
            pass

    tracker = _StreamTracker(
        FakeStream(list(_fake_stream_chunks())),
        kwargs={"model": "gpt-4o-mini"},
        start=time.perf_counter(),
    )
    # Drain then close — should only record once.
    list(iter(tracker))
    tracker.close()
    tracker.close()
    assert tracker._recorded is True


