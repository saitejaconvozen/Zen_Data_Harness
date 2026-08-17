from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FactoryDrainCliTests(unittest.TestCase):
    def test_drain_source_has_no_planner_or_control_cycle_dependency(self):
        source = (ROOT / "src" / "zen_agent" / "factory_drain_cli.py").read_text(encoding="utf-8")
        self.assertNotIn("FactoryOperator", source)
        self.assertNotIn("compile_plan", source)
        self.assertIn('"planned_new_work": False', source)


if __name__ == "__main__":
    unittest.main()
