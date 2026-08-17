from __future__ import annotations

import unittest

from zen_agent.factory_inventory import normalize_agent_inventory


class FactoryInventoryTests(unittest.TestCase):
    def test_duplicate_metadata_rows_collapse_without_double_counting_traces(self):
        rows = normalize_agent_inventory([
            {"agent_id": "a", "conversation_count": 100, "languages": ["en"], "project_name": "one"},
            {"agent_id": "a", "conversation_count": 100, "languages": ["te"], "project_name": "two"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["conversation_count"], 100)
        self.assertEqual(rows[0]["languages"], ["en", "te"])
        self.assertEqual(rows[0]["metadata_rows"], 2)


if __name__ == "__main__":
    unittest.main()
