"""Re-apply trace sanitisation to conversations bound before it existed.

Sanitisation now runs at the boundary, in `bind_conversation`, so anything
acquired from here on is clean. Conversations bound earlier still carry the
scaffolding a transcript arrives with — session-metadata blocks, silence
placeholders, speech-to-text diagnostics — because the original guard only
matched a metadata block that was the *entire* turn.

This rewrites those already on disk. Two places hold the text:

* the immutable packet batches, which hold what the agent and caller said
* the refiner and repairer decisions, which hold the corrected assistant text

Both are rewritten, because a turn the refiner chose to KEEP carries the source
text forward verbatim — a metadata block included.

What is deliberately *not* changed:

* `source_content_sha256`. Conversation identity stays tied to the raw Mongo
  document, so re-fetching the same call is still idempotent and the queue's
  `job_key` keeps pointing at the same work.
* Any judgement. No stage re-runs and no model is called; this only removes
  text that was never speech.

    python scripts/resanitise_packets.py <run_id> [--dry-run]
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zen_agent.adapters.mongodb import sanitise_turn  # noqa: E402


def _packet_batches(run_id: str) -> set[str]:
    db = sqlite3.connect("file:.zen/factory-qualification.db?mode=ro", uri=True)
    try:
        return {
            row[0] for row in db.execute(
                "SELECT DISTINCT packet_batch FROM factory_configuration_sample "
                "WHERE run_id=?", (run_id,))
        }
    finally:
        db.close()


def _clean_turn(turn: dict, counts: collections.Counter) -> bool:
    """Sanitise one packet turn in place. True if anything changed."""
    text = turn.get("text")
    if not isinstance(text, str) or not text:
        return False
    speech, removed = sanitise_turn(text)
    if not removed:
        return False
    for name in removed:
        counts[name] += 1
    turn["text"] = speech
    turn.setdefault("raw_text_sha256", turn.get("text_sha256"))
    turn["sanitised"] = list(removed)
    if not speech.strip() and not turn.get("tool_calls"):
        # Nothing was said. Keep the turn on the record but out of the dialogue.
        turn["role"] = "runtime_metadata"
        counts["demoted_to_runtime_metadata"] += 1
    return True


def resanitise(run_id: str, *, dry_run: bool) -> dict[str, int]:
    counts: collections.Counter[str] = collections.Counter()

    for batch in sorted(_packet_batches(run_id)):
        path = Path(batch)
        if not path.is_file():
            counts["missing_batch"] += 1
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        packets = document.get("result", {}).get("packets") or []
        touched = False
        for packet in packets:
            for turn in packet.get("turns") or []:
                if _clean_turn(turn, counts):
                    touched = True
            # Turn counts describe speech, so a call that is mostly silence
            # stops claiming to be a conversation.
            speech_turns = [
                t for t in packet.get("turns") or []
                if t.get("role") in {"user", "assistant"} and (t.get("text") or "").strip()
            ]
            packet["assistant_turn_count"] = sum(
                1 for t in speech_turns if t["role"] == "assistant")
        if touched:
            counts["batches_rewritten"] += 1
            if not dry_run:
                backup = path.with_suffix(path.suffix + ".preclean")
                if not backup.exists():
                    shutil.copy2(path, backup)
                path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    # Decisions carry the corrected assistant text forward; a KEPT turn repeats
    # the source verbatim, scaffolding included.
    for decision_path in sorted(Path(".zen/jobs", run_id).glob("*/*.json")):
        if decision_path.name not in {"refiner.json", "repair.json"}:
            continue
        try:
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            counts["unreadable_decision"] += 1
            continue
        rows = (decision.get("decision") or {}).get("assistant_turns") or []
        touched = False
        for row in rows:
            for field in ("golden_text", "golden_text_final"):
                value = row.get(field)
                if not isinstance(value, str) or not value:
                    continue
                speech, removed = sanitise_turn(value)
                if removed:
                    row[field] = speech
                    counts[f"decision_{field}"] += 1
                    touched = True
        if touched:
            counts["decisions_rewritten"] += 1
            if not dry_run:
                backup = decision_path.with_suffix(".json.preclean")
                if not backup.exists():
                    shutil.copy2(decision_path, backup)
                decision_path.write_text(
                    json.dumps(decision, ensure_ascii=False), encoding="utf-8")

    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    counts = resanitise(args.run_id, dry_run=args.dry_run)
    prefix = "would change" if args.dry_run else "changed"
    print(f"{prefix}:")
    for key, value in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {value:6d}  {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
