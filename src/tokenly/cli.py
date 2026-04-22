"""tokenly CLI: stats, tail, export, reset, doctor."""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time

from . import __version__
from .backends import get_backend, resolve_url
from .backends.base import (
    last_n_days_epoch,
    start_of_day_epoch,
    start_of_month_epoch,
)
from .core import _mask_url


def _open_backend():
    return get_backend(resolve_url())


def _fmt_usd(x: float) -> str:
    x = float(x or 0)
    if x < 0.01:
        return f"${x:.4f}"
    return f"${x:.2f}"


def _fmt_int(n) -> str:
    return f"{int(n or 0):,}"


def _window_since(args) -> tuple[float | None, str]:
    if args.all:
        return None, "All time"
    if args.month:
        return start_of_month_epoch(), "This month"
    if args.week:
        return last_n_days_epoch(7), "Last 7 days"
    return start_of_day_epoch(), "Today"


def cmd_stats(args) -> int:
    try:
        backend = _open_backend()
    except Exception as e:
        print(f"tokenly: {e}", file=sys.stderr)
        return 1

    try:
        since_ts, label = _window_since(args)
        totals = backend.totals(since_ts)
        calls, in_tok, out_tok, cr_tok, cw_tok, total_cost, avg_lat = totals

        print()
        print(f"  tokenly · {label}")
        print("  " + "─" * 52)
        print(f"  Spend       {_fmt_usd(total_cost):>14}")
        print(f"  Calls       {_fmt_int(calls):>14}")
        print(f"  Input       {_fmt_int(in_tok):>14} tokens")
        print(f"  Output      {_fmt_int(out_tok):>14} tokens")
        if cr_tok or cw_tok:
            print(f"  Cache read  {_fmt_int(cr_tok):>14} tokens")
            if cw_tok:
                print(f"  Cache write {_fmt_int(cw_tok):>14} tokens")
        print(f"  Avg latency {int(avg_lat or 0):>10} ms")
        print()

        if args.by:
            rows = backend.group_by(args.by, since_ts)
            header = args.by if not args.by.startswith("tag.") else args.by
            print(f"  By {header}")
            print("  " + "─" * 52)
            for row in rows:
                name, cost, n = row[0], row[1], row[2]
                name_str = str(name) if name is not None else "(none)"
                print(
                    f"  {name_str[:32]:<32} {_fmt_usd(cost):>10}  "
                    f"{_fmt_int(n):>6} calls"
                )
            print()
    finally:
        backend.close()
    return 0


def cmd_tail(args) -> int:
    backend = _open_backend()
    last_id = backend.max_id()
    print("tokenly: tailing calls (Ctrl-C to stop)")
    try:
        while True:
            rows = backend.tail_since(last_id)
            for row in rows:
                rid, ts, prov, model, in_t, out_t, cr, cost, lat = row
                tstr = time.strftime("%H:%M:%S", time.localtime(ts))
                cache_str = f" cache:{cr}" if cr else ""
                print(
                    f"  {tstr}  {prov}/{model:<25} "
                    f"in:{in_t:>6} out:{out_t:>6}{cache_str} "
                    f"{_fmt_usd(cost):>10}  {lat}ms"
                )
                last_id = rid
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        backend.close()
    return 0


def cmd_export(args) -> int:
    backend = _open_backend()
    try:
        rows = backend.export_all()
        writer = csv.writer(sys.stdout)
        writer.writerow(
            [
                "ts",
                "provider",
                "model",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cost_usd",
                "latency_ms",
                "tags",
            ]
        )
        for r in rows:
            writer.writerow(r)
    finally:
        backend.close()
    return 0


def cmd_reset(args) -> int:
    backend = _open_backend()
    try:
        if not args.yes:
            resp = input(f"wipe {backend.describe()}? [y/N] ").strip().lower()
            if resp != "y":
                print("aborted.")
                return 1
        backend.reset()
        print(f"tokenly: reset {backend.name}")
    finally:
        backend.close()
    return 0


def cmd_dashboard(args) -> int:
    from .dashboard import serve

    if args.host in ("0.0.0.0", "::"):
        # ANSI yellow — graceful if the terminal doesn't render it.
        print(
            "\033[33m"
            "  ⚠  dashboard bound to "
            f"{args.host} — no authentication. Read-only, but "
            "anyone on the network can see your spend data.\n"
            "     Use only on trusted networks."
            "\033[0m",
            file=sys.stderr,
        )
    serve(
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
    return 0


def cmd_doctor(args) -> int:
    import importlib.util

    url = resolve_url()
    print()
    print("  tokenly · doctor")
    print("  " + "─" * 52)
    print(f"  version:  {__version__}")
    print(f"  db url:   {_mask_url(url)}")
    try:
        backend = get_backend(url)
        try:
            print(f"  backend:  {backend.describe()}")
            # Try connecting to surface any error.
            _ = backend.conn
            print("  connect:  ok")
        finally:
            backend.close()
    except Exception as e:
        print(f"  connect:  FAILED ({e})")

    print()

    def _has(mod: str) -> bool:
        try:
            return importlib.util.find_spec(mod) is not None
        except (ModuleNotFoundError, ValueError):
            return False

    print("  SDKs:")
    for name, mod in [
        ("openai", "openai"),
        ("anthropic", "anthropic"),
        ("google-genai", "google.genai"),
        ("google-generativeai", "google.generativeai"),
    ]:
        mark = "ok" if _has(mod) else "not installed"
        print(f"    {name:<22} {mark}")
    print()
    print("  DB drivers:")
    for name, mod in [("pymysql", "pymysql"), ("psycopg", "psycopg")]:
        mark = "ok" if _has(mod) else "not installed"
        print(f"    {name:<22} {mark}")
    print()
    for var in [
        "TOKENLY_DB_URL",
        "TOKENLY_DB",
        "TOKENLY_DAILY_BUDGET",
        "TOKENLY_DAILY_WARN",
    ]:
        val = os.environ.get(var, "(unset)")
        if var == "TOKENLY_DB_URL" and val != "(unset)":
            val = _mask_url(val)
        print(f"  {var:<22} {val}")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tokenly", description="Track AI API costs.")
    p.add_argument("--version", action="version", version=f"tokenly {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stats", help="show spend summary")
    s.add_argument("--month", action="store_true", help="this calendar month")
    s.add_argument("--week", action="store_true", help="last 7 days")
    s.add_argument("--all", action="store_true", help="all time")
    s.add_argument("--by", help="group by: model | provider | tag.<key>", default=None)
    s.set_defaults(func=cmd_stats)

    t = sub.add_parser("tail", help="stream calls live")
    t.set_defaults(func=cmd_tail)

    e = sub.add_parser("export", help="dump calls as CSV to stdout")
    e.add_argument("--csv", action="store_true", default=True)
    e.set_defaults(func=cmd_export)

    r = sub.add_parser("reset", help="wipe log storage")
    r.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    r.set_defaults(func=cmd_reset)

    d = sub.add_parser("doctor", help="diagnose setup")
    d.set_defaults(func=cmd_doctor)

    dash = sub.add_parser("dashboard", help="launch the local web dashboard")
    dash.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    dash.add_argument("--port", type=int, default=8787, help="bind port (default: 8787)")
    dash.add_argument("--no-open", action="store_true", help="do not open browser")
    dash.set_defaults(func=cmd_dashboard)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
