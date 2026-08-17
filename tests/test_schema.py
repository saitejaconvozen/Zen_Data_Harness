from __future__ import annotations

import unittest

from zen_agent.schema import SchemaError, validate


class SchemaTests(unittest.TestCase):
    def test_rejects_unknown_and_missing_fields(self):
        schema = {
            "type": "object",
            "required": ["name"],
            "additionalProperties": False,
            "properties": {"name": {"type": "string"}},
        }
        with self.assertRaises(SchemaError):
            validate({}, schema)
        with self.assertRaises(SchemaError):
            validate({"name": "x", "extra": True}, schema)

    def test_boolean_is_not_integer(self):
        with self.assertRaises(SchemaError):
            validate(True, {"type": "integer"})
