from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zen_agent.models import Plan, TaskSpec
from zen_agent.state import EventStore


class StateTests(unittest.TestCase):
    def test_running_task_becomes_pending_on_resume(self):
        with TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "state.db")
            try:
                plan = Plan("fixture", "test resume", (TaskSpec("one", "One", "fixture.tool", {}),), "fixture")
                run_id = store.create_run(plan, "fingerprint")
                task = store.list_tasks(run_id)[0]
                store.start_task(task["id"])
                store.prepare_resume(run_id)
                self.assertEqual(store.list_tasks(run_id)[0]["status"], "PENDING")
                self.assertIn("run.resumed", [event["event_type"] for event in store.trace(run_id)])
            finally:
                store.close()
