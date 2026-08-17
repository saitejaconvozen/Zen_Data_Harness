from __future__ import annotations

import re
from typing import Any


class SchemaError(ValueError):
    pass


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the JSON Schema subset used by local tool contracts."""

    expected = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if expected:
        python_type = type_map.get(expected)
        if python_type is None:
            raise SchemaError(f"{path}: unsupported schema type {expected}")
        if expected in {"integer", "number"} and isinstance(value, bool):
            raise SchemaError(f"{path}: expected {expected}, got boolean")
        if not isinstance(value, python_type):
            raise SchemaError(f"{path}: expected {expected}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: value is not in enum")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = sorted(set(schema.get("required", [])) - set(value))
        if missing:
            raise SchemaError(f"{path}: missing required fields {missing}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise SchemaError(f"{path}: unknown fields {extra}")
        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                validate(item, child_schema, f"{path}.{key}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaError(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(f"{path}: more than maxItems")
        if "items" in schema:
            for index, item in enumerate(value):
                validate(item, schema["items"], f"{path}[{index}]")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaError(f"{path}: shorter than minLength")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise SchemaError(f"{path}: does not match pattern")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path}: above maximum")
