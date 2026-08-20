"""Report what the harness is actually doing, from recorded model calls.

`zen-observe <run_id>` answers the questions the queue cannot: what each role
costs, where the latency is, how often models are retried, and whether the run
is making progress right now or merely restarting.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_config
from .observability import MetricsStore


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "  (no calls recorded)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    head = "  " + "  ".join(c.ljust(widths[c]) for c in columns)
    rule = "  " + "  ".join("-" * widths[c] for c in columns)
    body = [
        "  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns)
        for r in rows
    ]
    return "\n".join([head, rule, *body])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zen-observe", description="Model-call observability for a factory run"
    )
    parser.add_argument("run_id", nargs="?", help="omit for all runs")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--window", type=int, default=300,
                        help="throughput window in seconds (default 300)")
    args = parser.parse_args(argv)

    config = load_config(args.root)
    store = MetricsStore(config.state_directory / "metrics.db")
    try:
        report = {
            "run_id": args.run_id,
            "totals": store.totals(args.run_id),
            "by_role": store.by_role(args.run_id),
            "failures": store.failure_reasons(args.run_id),
        }
        if args.run_id:
            report["throughput"] = store.throughput(args.run_id, args.window)
            report["economics"] = store.cost_per_conversation(args.run_id)

        if args.json:
            print(json.dumps(report, indent=2))
            return 0

        totals = report["totals"]
        print(f"\n=== {args.run_id or 'all runs'} ===")
        print(f"  model calls   : {totals.get('calls') or 0}")
        print(f"  failures      : {totals.get('failures') or 0}")
        print(f"  tokens        : {totals.get('tokens') or 0:,}")
        print(f"  spend         : ${totals.get('cost_usd') or 0:.4f}")

        if args.run_id:
            rate = report["throughput"]
            print(f"\n  last {rate['window_seconds']}s: {rate['calls']} calls "
                  f"({rate['calls_per_minute']}/min), {rate.get('failures') or 0} failed")
            economics = report["economics"]
            if economics.get("cost_per_conversation") is not None:
                print(f"  cost/conversation : ${economics['cost_per_conversation']:.5f}")
                print(f"  projected 10k     : ${economics['projected_10k_usd']:.2f}")

        print("\n  by role:")
        print(_table(report["by_role"], [
            "role", "model", "calls", "failures", "retries",
            "mean_ms", "input_tokens", "output_tokens", "cost_usd",
        ]))
        if report["failures"]:
            print("\n  failures:")
            print(_table(report["failures"], ["error_class", "role", "n"]))
        print()
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
