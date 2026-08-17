from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zen_agent.adapters.mongodb import bind_conversation
from zen_agent.artifacts import ArtifactStore


class LiveSourceContractTests(unittest.TestCase):
    def test_artifact_directories_are_owner_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            store = ArtifactStore(root)
            record = store.put_json({"protected": True})
            self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(root / "blobs").st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(root / record.relative_path).st_mode & 0o777, 0o600)

    def test_binding_preserves_exact_user_turn(self):
        user_text = "haan ji mera accnt no galat aa raha..."
        document = {
            "_id": "source-1",
            "agent_id": "agent-1",
            "call_id": "call-1",
            "duration": 30,
            "chat_history": [
                {"role": "system", "content": "Follow the workflow."},
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": "I can help."},
                {"role": "user", "content": "haan"},
                {"role": "assistant", "content": "Please continue."},
                {"role": "user", "content": "okay ji"},
                {"role": "assistant", "content": "Thank you."},
            ],
        }
        bound = bind_conversation(document)
        observed = [turn["text"] for turn in bound["turns"] if turn["role"] == "user"]
        self.assertEqual(observed[0], user_text)
        self.assertEqual(bound["user_turn_count"], 3)
