from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
import time

from .config import load_config
from .factory_review import build_review, publish


def _rows(path: Path, sql: str, run_id: str) -> list[dict]:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(sql, (run_id,)).fetchall()]
    finally:
        db.close()


def known_run_ids(root: Path) -> list[str]:
    path = root / ".zen" / "factory-control.db"
    if not path.is_file():
        return []
    db = sqlite3.connect(path)
    try:
        return [row[0] for row in db.execute(
            "SELECT id FROM factory_runs ORDER BY rowid DESC"
        ).fetchall()]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def snapshot(root: Path, run_id: str) -> dict:
    # An unknown run used to report a tidy page of zeros, which reads exactly
    # like a run that has not started yet. Fail loudly instead.
    known = known_run_ids(root)
    if known and run_id not in known:
        raise ValueError(
            f"unknown run_id {run_id!r}. Known runs (newest first): "
            + ", ".join(known[:5])
        )
    zen = root / ".zen"
    samples = _rows(
        zen / "factory-qualification.db",
        "SELECT verdict FROM factory_configuration_sample WHERE run_id=?",
        run_id,
    )
    work = _rows(
        zen / "factory-queue.db",
        "SELECT stage,status,payload_json FROM factory_work WHERE run_id=?",
        run_id,
    )
    queue = Counter((row["stage"], row["status"]) for row in work)
    terminal = Counter()
    for row in work:
        if row["stage"] == "terminal" and row["status"] == "SUCCEEDED":
            payload = json.loads(row["payload_json"])
            terminal[payload["inputs"]["terminal_status"]] += 1
    review = build_review(root, run_id)
    selected = len(samples)
    terminal_total = sum(terminal.values())
    return {
        "run_id": run_id,
        "selected": selected,
        "agent_audited": sum(row["verdict"] is not None for row in samples),
        "refined": queue[("refine", "SUCCEEDED")],
        "refine_active": queue[("refine", "LEASED")],
        "refine_queued": queue[("refine", "READY")],
        "verified_initial": queue[("verify", "SUCCEEDED")],
        "verification_active": queue[("verify", "LEASED")],
        "verification_queued": queue[("verify", "READY")],
        "repair_rounds": queue[("repair", "SUCCEEDED")],
        "terminal": terminal_total,
        "terminal_statuses": dict(terminal),
        "remaining": max(0, selected - terminal_total),
        "changed_assistant_turns": review["counts"]["replaced_assistant_turns"],
        "metric_citations": review["counts"]["metric_citations"],
        "dead_work": sum(
            count
            for (stage, status), count in queue.items()
            if status == "DEAD"
        ),
    }


def _print(value: dict) -> None:
    terminal = value["terminal_statuses"]
    print(
        f"Run {value['run_id']}\n"
        f"Selected {value['selected']} | audited {value['agent_audited']} | "
        f"terminal {value['terminal']} | remaining {value['remaining']}\n"
        f"Refine: {value['refined']} done, {value['refine_active']} active, "
        f"{value['refine_queued']} queued\n"
        f"Verify: {value['verified_initial']} done, "
        f"{value['verification_active']} active, "
        f"{value['verification_queued']} queued\n"
        f"Repair rounds {value['repair_rounds']} | "
        f"verified candidates {terminal.get('VERIFIED_CANDIDATE', 0)} | "
        f"partial candidates {terminal.get('PARTIAL_CANDIDATE', 0)} | "
        f"quarantined {terminal.get('QUARANTINED', 0)}\n"
        f"Changed assistant turns {value['changed_assistant_turns']} | "
        f"metric citations {value['metric_citations']} | "
        f"dead work {value['dead_work']}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zen-factory-progress",
        description="Show concise factory progress and optionally refresh the protected UI",
    )
    parser.add_argument(
        "run_id", nargs="?",
        help="factory run id; omit with --list to see known runs",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--list", action="store_true", help="list known run ids and exit")
    parser.add_argument("--site", type=Path)
    parser.add_argument(
        "--watch", type=int, default=0,
        help="refresh interval in seconds; zero prints once",
    )
    args = parser.parse_args(argv)
    if args.watch < 0 or args.watch > 60:
        parser.error("watch interval must be between 0 and 60 seconds")
    config = load_config(args.root)
    if args.list or not args.run_id:
        runs = known_run_ids(config.root)
        if not runs:
            print("no factory runs found; create one with: zen-factory create --target N")
            return 1
        print("known run ids (newest first):")
        for item in runs:
            print(f"  {item}")
        return 0
    try:
        while True:
            value = snapshot(config.root, args.run_id)
            if args.site:
                publish(config.root, args.run_id, args.site.resolve())
            _print(value)
            if not args.watch or value["remaining"] == 0 or value["dead_work"]:
                return 0 if value["dead_work"] == 0 else 2
            print(f"Next refresh in {args.watch}s. Press Ctrl-C to stop watching.\n")
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("Progress watch stopped; factory workers continue.")
        return 0
    except ValueError as exc:
        print(f"zen-factory-progress: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
