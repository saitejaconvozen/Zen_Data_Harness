from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from zen_agent.factory_qualification import FactoryQualificationStore, wilson_lower


def packet(index: int) -> dict:
    digest = f"{index + 1:064x}"
    return {
        "packet_id": "rp_" + f"{100 + index:064x}",
        "source": {
            "agent_id": "agent-a",
            "agent_version": "v1",
            "system_prompt_sha256": "a" * 64,
            "source_content_sha256": digest,
        },
    }


class FactoryQualificationTests(unittest.TestCase):
    def test_configuration_waits_for_complete_sample_then_qualifies(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FactoryQualificationStore(Path(directory) / "qualification.db")
            try:
                keys = [store.register_packet("run", packet(i), "batch.json", i) for i in range(3)]
                self.assertEqual(len(set(keys)), 1)
                key = keys[0]
                for index in range(2):
                    store.record_audit(
                        "run", packet(index)["source"]["source_content_sha256"],
                        verdict="PASS", critical_failures=0,
                        decision_sha256=f"{index + 20:064x}",
                    )
                self.assertEqual(store.decide("run", key).status, "PENDING")
                store.record_audit(
                    "run", packet(2)["source"]["source_content_sha256"],
                    verdict="PASS", critical_failures=0, decision_sha256="f" * 64,
                )
                decision = store.decide("run", key)
                self.assertEqual(decision.status, "QUALIFIED")
                self.assertEqual(len(store.promotable("run", key)), 3)
            finally:
                store.close()

    def test_critical_failure_rejects_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FactoryQualificationStore(Path(directory) / "qualification.db")
            try:
                key = ""
                for index in range(3):
                    key = store.register_packet("run", packet(index), "batch.json", index)
                    store.record_audit(
                        "run", packet(index)["source"]["source_content_sha256"],
                        verdict="PASS" if index else "FAIL",
                        critical_failures=1 if index == 0 else 0,
                        decision_sha256=f"{index + 30:064x}",
                    )
                self.assertEqual(store.decide("run", key).status, "REJECTED")
                self.assertEqual(store.promotable("run", key), [])
            finally:
                store.close()

    def test_wilson_lower_is_conservative_for_small_samples(self):
        self.assertLess(wilson_lower(3, 3), 0.50)
        self.assertGreater(wilson_lower(100, 100), 0.95)


if __name__ == "__main__":
    unittest.main()
