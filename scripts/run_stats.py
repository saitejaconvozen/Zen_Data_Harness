"""Small queue queries the supervisor shell needs.

Kept as a file rather than inline heredocs: a Python heredoc nested inside a
shell heredoc inside a script is how the supervisor got silently corrupted once
already, and these three questions are worth being able to run by hand.

    python scripts/run_stats.py <run_id> outstanding | candidates | requeue-dead
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import time


QUEUE = Path(".zen/factory-queue.db")
CANDIDATE_STATUSES = {"VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE"}


def _read_only() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{QUEUE}?mode=ro", uri=True, timeout=20)


def outstanding(run_id: str) -> int:
    with _read_only() as db:
        return list(db.execute(
            "SELECT COUNT(*) FROM factory_work WHERE run_id=? "
            "AND status IN ('READY','LEASED')", (run_id,)))[0][0]


def candidates(run_id: str) -> int:
    """Conversations that reached a releasable terminal state."""
    total = 0
    with _read_only() as db:
        for (payload,) in db.execute(
            "SELECT payload_json FROM factory_work WHERE run_id=? "
            "AND stage='terminal' AND status='SUCCEEDED'", (run_id,)
        ):
            try:
                status = (json.loads(payload).get("inputs") or {}).get("terminal_status")
            except ValueError:
                continue
            if status in CANDIDATE_STATUSES:
                total += 1
    return total


def requeue_dead(run_id: str) -> int:
    """Return dead-lettered work to the queue with a fresh attempt budget.

    Most dead letters here are transient — a provider hiccup, a rate limit, a
    bug fixed since. `attempt` is charged on claim, so without this reset a
    conversation that hit three bad minutes is discarded permanently.
    """
    db = sqlite3.connect(QUEUE, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    now = time.time()
    try:
        with db:
            return db.execute(
                "UPDATE factory_work SET status='READY', attempt=0, error=NULL,"
                " lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,"
                " available_at=?, updated_at=? WHERE run_id=? AND status='DEAD'",
                (now, now, run_id),
            ).rowcount
    finally:
        db.close()


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    run_id, command = sys.argv[1], sys.argv[2]
    if command == "outstanding":
        print(outstanding(run_id))
    elif command == "candidates":
        print(candidates(run_id))
    elif command == "requeue-dead":
        print(f"requeued {requeue_dead(run_id)} dead items")
    else:
        print(f"unknown command {command!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
