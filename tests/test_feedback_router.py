from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from zen_agent.factory_queue import LocalFactoryQueue
from zen_agent.feedback_router import (
    FeedbackPolicyViolation,
    FeedbackRouter,
    FeedbackRoutingError,
    STAGE,
)


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "run-feedback"
PACKET_ID = "rp_" + "a" * 64
SOURCE_SHA = "b" * 64
DECISION_ID = "hfd_" + "c" * 64


def fixture_packet() -> dict:
    user = "haan, booking kal kar do"
    user_sha = sha256(user.encode("utf-8")).hexdigest()
    assistant = "Sure, I have booked it."
    return {
        "packet_id": PACKET_ID,
        "source": {"source_content_sha256": SOURCE_SHA},
        "turns": [
            {
                "turn_id": "turn_0000", "source_index": 0, "role": "user",
                "text": user, "text_sha256": user_sha,
            },
            {
                "turn_id": "turn_0001", "source_index": 1, "role": "assistant",
                "text": assistant,
                "text_sha256": sha256(assistant.encode("utf-8")).hexdigest(),
            },
        ],
        "user_turn_sha256": [user_sha],
    }


def fixture_decision() -> dict:
    return {
        "schema_version": "zen.human-feedback/1",
        "decision_id": DECISION_ID,
        "run_id": RUN_ID,
        "packet_id": PACKET_ID,
        "source_content_sha256": SOURCE_SHA,
        "packet_locator": {"packet_batch": "packet-batch.json", "packet_index": 0},
        "approval": {
            "status": "APPROVED",
            "reviewer_id": "reviewer-17",
            "approved_at": "2026-08-14T08:00:00Z",
        },
        "feedback": {
            "action": "REQUEST_REPAIR",
            "targets": [
                {
                    "turn_id": "turn_0001",
                    "instruction": "Do not claim booking success until the workflow confirms it.",
                }
            ],
        },
    }


class FeedbackRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.workspace = Path(self.temporary.name)
        self.batch_path = self.workspace / "packet-batch.json"
        self.batch_path.write_text(
            json.dumps({"result": {"packets": [fixture_packet()]}}), encoding="utf-8"
        )
        self.queue = LocalFactoryQueue(self.workspace / "queue.db")
        self.router = FeedbackRouter(
            self.workspace, self.queue, RUN_ID, max_feedback_rounds=2
        )

    def tearDown(self) -> None:
        self.queue.close()
        self.temporary.cleanup()

    def test_approved_feedback_is_source_bound_and_idempotently_enqueued(self):
        first = self.router.route(fixture_decision())
        second = self.router.route(fixture_decision())

        self.assertTrue(first.enqueued)
        self.assertFalse(second.enqueued)
        self.assertEqual(
            first.job_key,
            "conversation-" + SOURCE_SHA + ":review-" + DECISION_ID + ":human-feedback-round-00",
        )
        self.assertEqual(second.job_key, first.job_key)
        row = self.queue.item(RUN_ID, first.job_key, STAGE)
        payload = row["payload"]
        self.assertEqual(payload["tool"], "golden.human_feedback_repair")
        self.assertEqual(payload["packet_locator"], fixture_decision()["packet_locator"])
        self.assertNotIn("immutable_user_turns", payload)
        self.assertNotIn("immutable_user_turns", payload["inputs"]["source_binding"])
        self.assertNotIn("haan, booking kal kar do", json.dumps(payload))
        self.assertEqual(
            payload["inputs"]["source_binding"]["user_turn_sha256"],
            fixture_packet()["user_turn_sha256"],
        )
        self.assertEqual(payload["user_turn_sha256"], fixture_packet()["user_turn_sha256"])
        self.assertEqual(self.queue.counts_by_stage(RUN_ID)[STAGE]["READY"], 1)

    def test_new_decisions_get_separate_rounds_and_round_cap_is_enforced(self):
        self.router.route(fixture_decision())
        second = fixture_decision()
        second["decision_id"] = "hfd_" + "d" * 64
        routed = self.router.route(second)
        self.assertEqual(routed.round_number, 1)
        self.assertTrue(routed.job_key.endswith("human-feedback-round-01"))

        third = fixture_decision()
        third["decision_id"] = "hfd_" + "e" * 64
        with self.assertRaisesRegex(FeedbackRoutingError, "rounds exhausted"):
            self.router.route(third)

    def test_decision_identity_cannot_be_reused_with_changed_feedback(self):
        self.router.route(fixture_decision())
        changed = fixture_decision()
        changed["feedback"]["targets"][0]["instruction"] = "Use a different correction."
        with self.assertRaisesRegex(FeedbackRoutingError, "reused with different content"):
            self.router.route(changed)

    def test_run_packet_source_and_user_turn_identity_are_checked(self):
        cases = []
        wrong_run = fixture_decision()
        wrong_run["run_id"] = "another-run"
        cases.append((wrong_run, "run identity mismatch"))
        wrong_packet = fixture_decision()
        wrong_packet["packet_id"] = "rp_" + "d" * 64
        cases.append((wrong_packet, "packet identity mismatch"))
        wrong_source = fixture_decision()
        wrong_source["source_content_sha256"] = "e" * 64
        cases.append((wrong_source, "source content identity mismatch"))
        for decision, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(FeedbackRoutingError, error):
                self.router.route(decision)

        batch = json.loads(self.batch_path.read_text(encoding="utf-8"))
        batch["result"]["packets"][0]["turns"][0]["text"] += "!"
        self.batch_path.write_text(json.dumps(batch), encoding="utf-8")
        with self.assertRaisesRegex(FeedbackRoutingError, "user-turn checksum mismatch"):
            self.router.route(fixture_decision())

    def test_only_approved_targeted_assistant_repairs_are_accepted(self):
        unapproved = fixture_decision()
        unapproved["approval"]["status"] = "DRAFT"
        with self.assertRaisesRegex(FeedbackRoutingError, "explicitly approved"):
            self.router.route(unapproved)

        user_target = fixture_decision()
        user_target["feedback"]["targets"][0]["turn_id"] = "turn_0000"
        with self.assertRaisesRegex(FeedbackRoutingError, "assistant turns only"):
            self.router.route(user_target)

        duplicate = fixture_decision()
        duplicate["feedback"]["targets"].append(
            deepcopy(duplicate["feedback"]["targets"][0])
        )
        with self.assertRaisesRegex(FeedbackRoutingError, "unique"):
            self.router.route(duplicate)

    def test_shared_policy_taxonomy_and_skill_mutations_are_rejected(self):
        for instruction in (
            "Modify the taxonomy to make this metric pass.",
            "Update the policy before repairing this turn.",
            "Edit the shared skills to allow this response.",
        ):
            decision = fixture_decision()
            decision["feedback"]["targets"][0]["instruction"] = instruction
            with self.subTest(instruction=instruction), self.assertRaises(FeedbackPolicyViolation):
                self.router.route(decision)

        structured = fixture_decision()
        structured["requested_mutations"] = {"taxonomy": "add an exception"}
        with self.assertRaises(FeedbackPolicyViolation):
            self.router.route(structured)

    def test_locator_cannot_escape_workspace(self):
        decision = fixture_decision()
        decision["packet_locator"]["packet_batch"] = "/etc/passwd"
        with self.assertRaisesRegex(FeedbackRoutingError, "escapes"):
            self.router.route(decision)


if __name__ == "__main__":
    unittest.main()
