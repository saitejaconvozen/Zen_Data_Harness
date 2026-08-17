from __future__ import annotations

import json
from pathlib import Path
import unittest


SCHEMAS = Path(__file__).resolve().parents[1] / "plugins" / "golden-conversations" / "schemas"


def _load(name):
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def _turn_items(schema):
    return schema["properties"]["decision"]["properties"]["assistant_turns"]["items"]


class RefinerSchemaParityTests(unittest.TestCase):
    """The response schema drives generation; the decision schema drives validation.

    They are separate files, so a change to one silently breaks the other. This
    guard failed in practice: turn-level fields were added to the decision schema
    while Codex kept generating against the untouched response schema.
    """

    def setUp(self) -> None:
        self.response = _load("refiner-response-v1")
        self.decision = _load("refiner-decision-v1")

    def test_turn_required_fields_match(self) -> None:
        self.assertEqual(
            sorted(_turn_items(self.response)["required"]),
            sorted(_turn_items(self.decision)["required"]),
        )

    def test_turn_enums_match(self) -> None:
        response = _turn_items(self.response)["properties"]
        decision = _turn_items(self.decision)["properties"]
        for field in ("action", "semantic_delta", "source_quality",
                      "downstream_coherence", "evidence_status"):
            self.assertEqual(
                response[field].get("enum"), decision[field].get("enum"), field
            )

    def test_decision_required_fields_match(self) -> None:
        self.assertEqual(
            sorted(self.response["properties"]["decision"]["required"]),
            sorted(self.decision["properties"]["decision"]["required"]),
        )

    def test_perfect_turns_may_carry_no_annotation_in_both(self) -> None:
        for schema in (self.response, self.decision):
            self.assertEqual(
                _turn_items(schema)["properties"]["annotations"].get("minItems", 0), 0
            )


class ContractParityTests(unittest.TestCase):
    """The repairer generates against the refiner's schema, so both prompts
    must explain every field the schema demands."""

    def _assert_contract_covers_schema(self, prompt_name: str) -> None:
        contract = (SCHEMAS.parent / "prompts" / prompt_name).read_text(encoding="utf-8")
        turn = _turn_items(_load("refiner-response-v1"))["properties"]
        vocabulary = (
            turn["source_quality"]["enum"]
            + turn["downstream_coherence"]["enum"]
            + turn["evidence_status"]["enum"]
        )
        for value in vocabulary:
            self.assertIn(value, contract, f"{value} is unexplained in {prompt_name}")

    def test_refiner_contract_matches_the_schema_vocabulary(self) -> None:
        self._assert_contract_covers_schema("conversation-refiner.md")

    def test_repairer_contract_matches_the_schema_vocabulary(self) -> None:
        self._assert_contract_covers_schema("conversation-repairer.md")


if __name__ == "__main__":
    unittest.main()
