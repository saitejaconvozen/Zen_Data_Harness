from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from zen_agent.review_feedback import (
    IdempotencyConflict,
    InvalidReviewTransition,
    ReviewFeedbackError,
    ReviewFeedbackStore,
)


def feedback(summary: str = "Looks correct") -> dict:
    return {
        "summary": summary,
        "reason_codes": ["human-reviewed"],
        "evidence_turn_ids": ["a1"],
        "metric_citations": [
            {
                "axis_id": "task-following",
                "subaxis_id": "workflow",
                "variant_id": "required-step",
                "turn_id": "a1",
                "verdict": "PASS",
            }
        ],
        "requested_changes": [],
    }


class ReviewFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = ReviewFeedbackStore(Path(self.directory.name) / "review.db")
        self.item = self.store.create_item(
            run_id="run-1",
            conversation_id="packet-1",
            source_content_sha256="source-sha",
            candidate_ref="sha256:candidate-1",
            assistant_turn_ids=["a2", "a1"],
        )

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_decision_is_idempotent_and_preserves_exact_reviewer_identity(self):
        identity = "Okta:Jane.Doe@example.org#A-104"
        first = self.store.record_decision(
            self.item["id"],
            action="APPROVE",
            reviewer_identity=identity,
            idempotency_key="browser-request-1",
            feedback=feedback(),
        )
        second = self.store.record_decision(
            self.item["id"],
            action="APPROVE",
            reviewer_identity=identity,
            idempotency_key="browser-request-1",
            feedback=feedback(),
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["reviewer_identity"], identity)
        self.assertEqual(len(self.store.list_decisions(self.item["id"])), 1)
        self.assertEqual(self.store.get_item(self.item["id"])["state"], "APPROVED")

        with self.assertRaises(IdempotencyConflict):
            self.store.record_decision(
                self.item["id"],
                action="APPROVE",
                reviewer_identity=identity,
                idempotency_key="browser-request-1",
                feedback=feedback("Different payload"),
            )

    def test_terminal_state_rejects_invalid_transition(self):
        self.store.record_decision(
            self.item["id"],
            action="REJECT",
            reviewer_identity="reviewer-1",
            idempotency_key="reject-1",
            feedback=feedback("Unsafe conversation"),
        )
        with self.assertRaisesRegex(InvalidReviewTransition, "cannot APPROVE from REJECTED"):
            self.store.record_decision(
                self.item["id"],
                action="APPROVE",
                reviewer_identity="reviewer-2",
                idempotency_key="approve-after-reject",
                feedback=feedback(),
            )

    def test_decisions_candidate_revisions_and_events_are_immutable(self):
        decision = self.store.record_decision(
            self.item["id"],
            action="REQUEST_REPAIR",
            reviewer_identity="reviewer-1",
            idempotency_key="repair-1",
            feedback=feedback("Repair task flow"),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "decisions are immutable"):
            self.store.db.execute(
                "UPDATE review_decisions SET action='APPROVE' WHERE id=?", (decision["id"],)
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "candidate revisions are immutable"):
            self.store.db.execute(
                "DELETE FROM review_candidate_revisions WHERE item_id=?", (self.item["id"],)
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "events are immutable"):
            self.store.db.execute(
                "UPDATE review_events SET event_type='changed' WHERE item_id=?", (self.item["id"],)
            )
        history = self.store.get_item(self.item["id"])
        self.assertEqual(history["decisions"][0]["action"], "REQUEST_REPAIR")
        self.assertEqual(history["decisions"][0]["revision"], 1)

    def test_request_repair_reopens_only_after_source_bound_candidate_revision(self):
        request = self.store.record_decision(
            self.item["id"],
            action="REQUEST_REPAIR",
            reviewer_identity="reviewer-1",
            idempotency_key="repair-1",
            feedback={
                **feedback("Greeting violates the selected tone variant"),
                "requested_changes": [
                    {"turn_id": "a1", "instruction": "Use the configured formal register"}
                ],
            },
        )
        pending = self.store.pending_repair_requests(run_id="run-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["repair_decision"]["id"], request["id"])

        with self.assertRaisesRegex(ReviewFeedbackError, "immutable source"):
            self.store.submit_candidate_revision(
                self.item["id"],
                candidate_ref="sha256:bad",
                source_content_sha256="changed-source",
                submitted_by="repair-worker-7",
                idempotency_key="candidate-2-bad",
            )
        candidate = self.store.submit_candidate_revision(
            self.item["id"],
            candidate_ref="sha256:candidate-2",
            source_content_sha256="source-sha",
            submitted_by="repair-worker-7",
            idempotency_key="candidate-2",
        )
        retry = self.store.submit_candidate_revision(
            self.item["id"],
            candidate_ref="sha256:candidate-2",
            source_content_sha256="source-sha",
            submitted_by="repair-worker-7",
            idempotency_key="candidate-2",
        )
        self.assertEqual(candidate["id"], retry["id"])
        reopened = self.store.get_item(self.item["id"])
        self.assertEqual(reopened["state"], "REVIEW_PENDING")
        self.assertEqual(reopened["current_candidate_revision"], 2)
        self.assertEqual([row["revision"] for row in reopened["candidate_revisions"]], [1, 2])
        self.assertEqual(reopened["decisions"][0]["action"], "REQUEST_REPAIR")

    def test_edit_accepts_only_known_assistant_turns_and_never_user_payloads(self):
        with self.assertRaisesRegex(ReviewFeedbackError, "exactly turn_id and text"):
            self.store.record_decision(
                self.item["id"],
                action="EDIT",
                reviewer_identity="reviewer-1",
                idempotency_key="edit-user",
                feedback=feedback("Edit requested"),
                assistant_edits=[{"turn_id": "u1", "role": "user", "text": "changed"}],
            )
        with self.assertRaisesRegex(ReviewFeedbackError, "not an assistant turn"):
            self.store.record_decision(
                self.item["id"],
                action="EDIT",
                reviewer_identity="reviewer-1",
                idempotency_key="edit-unknown",
                feedback=feedback("Edit requested"),
                assistant_edits={"u1": "changed user text"},
            )
        decision = self.store.record_decision(
            self.item["id"],
            action="EDIT",
            reviewer_identity="reviewer-1",
            idempotency_key="edit-assistant",
            feedback=feedback("Shorten the assistant response"),
            assistant_edits={"a1": "Corrected assistant response."},
        )
        self.assertEqual(
            decision["assistant_edits"],
            [{"turn_id": "a1", "text": "Corrected assistant response."}],
        )
        self.assertEqual(
            self.store.get_item(self.item["id"])["state"], "EDITED_PENDING_VERIFICATION"
        )

    def test_query_api_filters_by_run_and_state(self):
        self.store.create_item(
            run_id="run-2",
            conversation_id="packet-2",
            source_content_sha256="source-sha-2",
            candidate_ref="sha256:candidate-2",
            assistant_turn_ids=["a1"],
        )
        self.store.record_decision(
            self.item["id"],
            action="APPROVE",
            reviewer_identity="reviewer-1",
            idempotency_key="approve-1",
            feedback=feedback(),
        )
        approved = self.store.list_items(run_id="run-1", state="APPROVED")
        self.assertEqual([row["conversation_id"] for row in approved], ["packet-1"])
        by_conversation = self.store.get_item_by_conversation("run-1", "packet-1")
        self.assertEqual(by_conversation["id"], self.item["id"])


if __name__ == "__main__":
    unittest.main()
