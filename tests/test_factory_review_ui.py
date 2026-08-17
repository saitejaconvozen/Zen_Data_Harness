import unittest

from zen_agent.factory_review import APP, INDEX, _metric_hierarchy, _verdict_rollup


class FactoryReviewUiTests(unittest.TestCase):
    def test_ui_has_turn_comparison_and_metric_coverage(self):
        self.assertIn("Pass / fail metrics", INDEX)
        self.assertIn("Source assistant", APP)
        self.assertIn("Refined assistant", APP)
        self.assertIn("axis_name", APP)
        self.assertIn("subaxis_name", APP)
        self.assertIn("variant_name", APP)
        self.assertIn('id="metricResult"', INDEX)
        self.assertIn('id="subaxis"', INDEX)
        self.assertIn('id="variant"', INDEX)
        self.assertIn("renderMetricHierarchy", APP)

    def test_fail_dominates_turn_verdict_rollup(self):
        self.assertEqual(_verdict_rollup(["PASS", "FAIL"]), "FAIL")
        self.assertEqual(_verdict_rollup(["PASS"]), "PASS")
        self.assertEqual(_verdict_rollup([]), "NOT_ASSESSED")

    def test_metric_hierarchy_is_axis_subaxis_variant(self):
        row = {
            "axis_id": "AX001",
            "axis_name": "Conversation",
            "subaxis_id": "AX001-SA001",
            "subaxis_name": "Opening",
            "variant_id": "AX001-SA001-V001",
            "variant_name": "Greeting",
            "source_id": "conversation-1",
            "turn_id": "turn-2",
            "source_verdict": "FAIL",
            "golden_verdict": "PASS",
        }
        hierarchy = _metric_hierarchy([row])
        self.assertEqual(hierarchy[0]["axis_id"], "AX001")
        self.assertEqual(hierarchy[0]["subaxes"][0]["subaxis_id"], "AX001-SA001")
        variant = hierarchy[0]["subaxes"][0]["variants"][0]
        self.assertEqual(variant["variant_id"], "AX001-SA001-V001")
        self.assertEqual(variant["counts"]["source"]["FAIL"], 1)
        self.assertEqual(variant["counts"]["golden"]["PASS"], 1)

    def test_transcripts_are_rendered_as_text_not_html(self):
        self.assertIn("textContent", APP)
        self.assertNotIn("innerHTML", APP)


if __name__ == "__main__":
    unittest.main()
