from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from zen_agent.factory import default_factory_manifest
from zen_agent.factory_queue import LocalFactoryQueue


class FactoryTests(unittest.TestCase):
    def test_default_manifest_sizes_for_five_thousand_accepts(self):
        manifest = default_factory_manifest()
        self.assertEqual(manifest.target_accepted, 5000)
        self.assertEqual(manifest.candidate_floor, 20000)
        self.assertEqual(manifest.model_policy, "gpt-5.6-sol-only")
        self.assertTrue(all(stage.model in {None, "gpt-5.6-sol"} for stage in manifest.stages))

    def test_claim_is_exclusive_and_completion_is_idempotency_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = LocalFactoryQueue(Path(directory) / "queue.db")
            try:
                self.assertTrue(queue.enqueue("run", "conversation-1", "refine", {"source_ref": "mongo://opaque"}))
                self.assertFalse(queue.enqueue("run", "conversation-1", "refine", {"source_ref": "duplicate"}))
                first = queue.claim("run", "worker-a", ("refine",), lease_seconds=30)
                self.assertIsNotNone(first)
                self.assertIsNone(queue.claim("run", "worker-b", ("refine",), lease_seconds=30))
                queue.complete(first.id, first.lease_token, "sha256:abc")
                self.assertEqual(queue.counts("run"), {"SUCCEEDED": 1})
                with self.assertRaisesRegex(ValueError, "stale"):
                    queue.complete(first.id, first.lease_token, "sha256:def")
            finally:
                queue.close()

    def test_fail_retries_then_dead_letters(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = LocalFactoryQueue(Path(directory) / "queue.db")
            try:
                queue.enqueue("run", "conversation-1", "verify", {}, max_attempts=2)
                first = queue.claim("run", "verifier-1", ("verify",))
                self.assertEqual(queue.fail(first.id, first.lease_token, "transient"), "READY")
                second = queue.claim("run", "verifier-2", ("verify",))
                self.assertEqual(second.attempt, 2)
                self.assertEqual(queue.fail(second.id, second.lease_token, "final"), "DEAD")
                self.assertEqual(queue.counts("run"), {"DEAD": 1})
            finally:
                queue.close()


if __name__ == "__main__":
    unittest.main()
