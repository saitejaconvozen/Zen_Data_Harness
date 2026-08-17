from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zen_agent.artifacts import ArtifactStore


class ArtifactTests(unittest.TestCase):
    def test_content_addressing_is_idempotent_and_tamper_evident(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            first = store.put_json({"value": 1})
            second = store.put_json({"value": 1})
            self.assertEqual(first, second)
            self.assertTrue(store.verify(first))
            (store.root / first.relative_path).write_bytes(b"changed")
            self.assertFalse(store.verify(first))
