#!/usr/bin/env python
"""Requeue every conversation a superseded policy discarded.

The harness rejects a conversation for exactly one reason: too few turns to be
worth refining. Whenever that rule is loosened, conversations already sitting in
a terminal rejected state stay rejected — the queue is durable, so nothing
re-decides them. This script re-opens them.

The conversations it targets are defined structurally, not by status string: a
conversation that was audited but never produced a `refine` item is one the
pipeline threw away. That covers both visible rejections and audits that
dead-lettered before they could enqueue anything. For each, it:

  * deletes the terminal work item, so the pipeline re-derives an outcome
  * resets the `agent_audit` item to READY with a fresh attempt budget

Re-auditing overwrites the recorded verdict, which `FactoryQualificationStore`
permits deliberately; see `record_audit`.

Stop the run before using this. Usage:

    python scripts/requeue_rejected.py <run_id> [--queue PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import time


def reopen(db: sqlite3.Connection, run_id: str, *, dry_run: bool) -> dict[str, int]:
    """Re-open every audited conversation that never reached refinement."""

    def keys(stage: str) -> set[str]:
        return {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT job_key FROM factory_work WHERE run_id=? AND stage=?",
                (run_id, stage),
            )
        }

    discarded = keys("agent_audit") - keys("refine")
    dead = {
        row[0]
        for row in db.execute(
            "SELECT DISTINCT job_key FROM factory_work WHERE run_id=? "
            "AND stage='agent_audit' AND status='DEAD'",
            (run_id,),
        )
    }
    counts = {
        "conversations": len(discarded),
        "dead_recovered": len(discarded & dead),
        "terminal_cleared": 0,
    }
    if dry_run or not discarded:
        return counts

    now = time.time()
    with db:
        for job_key in discarded:
            cleared = db.execute(
                "DELETE FROM factory_work WHERE run_id=? AND job_key=? AND stage='terminal'",
                (run_id, job_key),
            )
            counts["terminal_cleared"] += cleared.rowcount
            db.execute(
                "UPDATE factory_work SET status='READY', attempt=0, error=NULL, "
                "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, "
                "available_at=?, updated_at=? "
                "WHERE run_id=? AND job_key=? AND stage='agent_audit'",
                (now, now, run_id, job_key),
            )
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--queue", default=".zen/factory-queue.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = sqlite3.connect(args.queue, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    try:
        counts = reopen(db, args.run_id, dry_run=args.dry_run)
    finally:
        db.close()

    prefix = "would re-open" if args.dry_run else "re-opened"
    print(
        f"{prefix} {counts['conversations']} discarded conversations "
        f"({counts['dead_recovered']} of them dead-lettered audits, "
        f"{counts['terminal_cleared']} terminal records cleared)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
