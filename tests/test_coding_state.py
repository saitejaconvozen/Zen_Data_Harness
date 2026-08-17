from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from zen_agent.coding_state import CodingStateStore


class CodingStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = CodingStateStore(Path(self.temporary.name) / "coding.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_session_lifecycle_and_ordered_append_only_events(self):
        session_id = self.store.create_session(
            "Fix the regression",
            self.temporary.name,
            metadata={"ticket": "ZEN-1"},
            session_id="session-one",
        )
        updated = self.store.update_session_status(session_id, "running")
        custom = self.store.append_event(session_id, "planner.completed", {"steps": 3})

        self.assertEqual(updated["status"], "RUNNING")
        self.assertEqual(updated["metadata"], {"ticket": "ZEN-1"})
        events = self.store.list_events(session_id)
        self.assertEqual(
            [event["event_type"] for event in events],
            ["session.created", "session.status_changed", "planner.completed"],
        )
        self.assertEqual(custom["payload"], {"steps": 3})
        self.assertEqual(
            [event["id"] for event in self.store.list_events(session_id, after=events[0]["id"])],
            [events[1]["id"], events[2]["id"]],
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.store.db.execute(
                "UPDATE coding_events SET event_type='changed' WHERE id=?", (events[0]["id"],)
            )

    def test_turns_and_tool_calls_are_structured_and_audited(self):
        session_id = self.store.create_session("Inspect", self.temporary.name)
        turn = self.store.add_turn(
            session_id, "assistant", {"action": "tool"}, agent_name="executor"
        )
        call_id = self.store.start_tool_call(
            session_id, "fs.read", {"path": "README.md"}, turn_id=turn["id"]
        )
        call = self.store.finish_tool_call(call_id, "SUCCEEDED", result={"text": "hello"})

        self.assertEqual(turn["sequence"], 1)
        self.assertEqual(self.store.list_turns(session_id)[0]["content"]["action"], "tool")
        self.assertEqual(call["arguments"], {"path": "README.md"})
        self.assertEqual(call["result"], {"text": "hello"})
        self.assertEqual(call["status"], "SUCCEEDED")
        self.assertIn("tool.finished", [event["event_type"] for event in self.store.list_events(session_id)])
        with self.assertRaisesRegex(ValueError, "already finished"):
            self.store.finish_tool_call(call_id, "FAILED", error="late")

    def test_feedback_steering_and_cancel_are_runtime_consumable(self):
        session_id = self.store.create_session("Implement", self.temporary.name)
        feedback = self.store.add_feedback(session_id, "Add an edge-case test", author="reviewer")
        steering = self.store.add_steering(session_id, "Do not change the public API")
        pending = self.store.list_feedback(session_id, pending_only=True)

        self.assertEqual([item["kind"] for item in pending], ["feedback", "steering"])
        handled = self.store.mark_feedback_handled(feedback["id"])
        self.assertTrue(handled["handled"])
        self.assertEqual([item["id"] for item in self.store.list_feedback(session_id, pending_only=True)], [steering["id"]])
        cancelled = self.store.request_cancel(session_id, reason="operator request")
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(cancelled["status"], "PLANNED")
        self.assertEqual(self.store.clear_cancel_request(session_id)["cancel_requested"], False)

    def test_parent_and_filters_are_validated(self):
        parent = self.store.create_session("Parent", self.temporary.name)
        child = self.store.create_session(
            "Child", self.temporary.name, parent_session_id=parent, agent_name="verifier"
        )
        self.store.update_session_status(child, "VERIFYING")

        self.assertEqual(self.store.get_session(child)["parent_session_id"], parent)
        self.assertEqual([item["id"] for item in self.store.list_sessions(status="VERIFYING")], [child])
        with self.assertRaises(KeyError):
            self.store.create_session("Orphan", self.temporary.name, parent_session_id="missing")
        with self.assertRaises(ValueError):
            self.store.update_session_status(child, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
