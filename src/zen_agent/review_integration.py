from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any

from .review_feedback import ReviewFeedbackStore


def _candidate_ref(conversation: dict[str, Any]) -> str:
    assistant = [
        {
            "turn_id": turn["turn_id"],
            "action": turn.get("action"),
            "golden_text": turn.get("golden_text"),
            "metric_citations": turn.get("metric_citations", []),
        }
        for turn in conversation["turns"]
        if turn["role"] == "assistant"
    ]
    payload = {
        "packet_id": conversation["packet_id"],
        "source_id": conversation["source_id"],
        "assistant_turns": assistant,
        "terminal": conversation["terminal"],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    decisions = item.get("decisions", [])
    return {
        "item_id": item["id"],
        "state": item["state"],
        "candidate_revision": item["current_candidate_revision"],
        "decision_revision": item["current_decision_revision"],
        "latest_decision": decisions[-1] if decisions else None,
        "candidate_revisions": item.get("candidate_revisions", []),
        "decisions": decisions,
        "events": item.get("events", []),
    }


def sync_review_items(
    root: Path,
    run_id: str,
    conversations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create review tasks and attach completed feedback-repair revisions."""
    path = root / ".zen" / "review-feedback.db"
    states: Counter[str] = Counter()
    by_conversation: dict[str, dict[str, Any]] = {}
    with ReviewFeedbackStore(path) as store:
        for conversation in conversations:
            packet_id = conversation["packet_id"]
            candidate_ref = _candidate_ref(conversation)
            assistant_ids = [
                turn["turn_id"]
                for turn in conversation["turns"]
                if turn["role"] == "assistant"
            ]
            try:
                item = store.get_item_by_conversation(run_id, packet_id)
            except KeyError:
                item = store.create_item(
                    run_id=run_id,
                    conversation_id=packet_id,
                    source_content_sha256=conversation["source_id_full"],
                    candidate_ref=candidate_ref,
                    assistant_turn_ids=assistant_ids,
                )
            latest = item.get("decisions", [])[-1] if item.get("decisions") else None
            terminal_decision = conversation["terminal"].get("review_decision_id")
            repair_finished = (
                latest is not None
                and item["state"] in {
                    "REPAIR_REQUESTED", "EDITED_PENDING_VERIFICATION",
                }
                and terminal_decision == latest["id"]
            )
            if repair_finished:
                current_ref = item["candidate_revisions"][-1]["candidate_ref"]
                revision_key = "verified-feedback-" + latest["id"]
                if candidate_ref != current_ref:
                    store.submit_candidate_revision(
                        item["id"],
                        candidate_ref=candidate_ref,
                        source_content_sha256=conversation["source_id_full"],
                        submitted_by="zen-feedback-autopilot",
                        idempotency_key=revision_key,
                    )
                else:
                    store.submit_candidate_revision(
                        item["id"],
                        candidate_ref=candidate_ref + ":reverified",
                        source_content_sha256=conversation["source_id_full"],
                        submitted_by="zen-feedback-autopilot",
                        idempotency_key=revision_key,
                    )
                item = store.get_item(item["id"])
            states[item["state"]] += 1
            by_conversation[packet_id] = _public_item(item)
    return {"states": dict(states), "items": by_conversation}
