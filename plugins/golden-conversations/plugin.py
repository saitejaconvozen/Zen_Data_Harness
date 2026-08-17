from __future__ import annotations

import csv
from hashlib import sha256
from pathlib import Path
import re

from zen_agent.models import Plan, TaskSpec, ToolRisk
from zen_agent.plugins import WorkflowSpec
from zen_agent.tools import ToolSpec


def _within_legacy_boundary(context, value):
    path = Path(value).resolve()
    allowed = context.workspace.parent.resolve()
    if allowed != path and allowed not in path.parents:
        raise PermissionError("golden bootstrap reads only inside the parent Sai_Teja workspace")
    return path


def _inspect_legacy(context, inputs):
    root = _within_legacy_boundary(context, inputs["legacy_root"])
    expected = {
        "package": root / "src" / "zen_data_engine",
        "readme": root / "README_GOLDEN_HARNESS.md",
        "taxonomy": root / "DSE OKR 2026 Q2 - Zen Eval Axes.csv",
        "tests": root / "tests",
    }
    present = {name: path.exists() for name, path in expected.items()}
    modules = sorted(path.name for path in expected["package"].glob("*.py")) if expected["package"].is_dir() else []
    tests = sorted(path.name for path in expected["tests"].glob("test_*.py")) if expected["tests"].is_dir() else []
    return {"legacy_root": str(root), "present": present, "module_count": len(modules), "test_count": len(tests), "modules": modules}


def _validate_taxonomy(context, inputs):
    path = _within_legacy_boundary(context, inputs["taxonomy_csv"])
    if not path.is_file():
        raise FileNotFoundError(path)
    axes = []
    subaxes = []
    variants = []
    current_axis = None
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.reader(handle), start=1):
            row = row + [""] * (4 - len(row))
            axis, subaxis, _, variant_cell = (item.strip() for item in row[:4])
            if axis:
                current_axis = axis
                axes.append(axis)
            if subaxis:
                if current_axis is None:
                    raise ValueError(f"subaxis before axis at row {row_number}")
                subaxes.append({"axis": current_axis, "subaxis": subaxis})
            numbered = re.findall(r"(?m)^\s*\d+\.\s+", variant_cell)
            if numbered:
                if not subaxis:
                    raise ValueError(f"variants without subaxis at row {row_number}")
                variants.extend({"axis": current_axis, "subaxis": subaxis} for _ in numbered)
    if not axes or not subaxes or not variants:
        raise ValueError("taxonomy must contain non-empty independent axis, subaxis, and variant columns")
    return {
        "path": str(path),
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "axis_count": len(axes),
        "subaxis_count": len(subaxes),
        "variant_count": len(variants),
        "independent_columns_validated": True,
    }


def _plan(objective, inputs, max_attempts):
    missing = sorted({"legacy_root", "taxonomy_csv"} - set(inputs))
    if missing:
        raise ValueError(f"golden-bootstrap missing inputs: {missing}")
    return Plan(
        workflow="golden-bootstrap",
        objective=objective,
        explanation="Inventory the existing deterministic foundation, then validate the governed taxonomy without reading MongoDB.",
        inputs=inputs,
        tasks=(
            TaskSpec("inventory", "Inventory legacy golden package", "golden.inspect_legacy", {"legacy_root": inputs["legacy_root"]}, max_attempts=max_attempts),
            TaskSpec("taxonomy", "Validate axes taxonomy", "golden.validate_taxonomy", {"taxonomy_csv": inputs["taxonomy_csv"]}, depends_on=("inventory",), max_attempts=max_attempts),
        ),
    )


def register(registry):
    registry.tools.register(
        ToolSpec(
            "golden.inspect_legacy", "0.1.0", "Inventory existing deterministic conversation components", ToolRisk.READ_ONLY,
            {"type": "object", "required": ["legacy_root"], "additionalProperties": False, "properties": {"legacy_root": {"type": "string", "minLength": 1}}},
            {"type": "object", "required": ["legacy_root", "present", "module_count", "test_count", "modules"], "additionalProperties": False, "properties": {"legacy_root": {"type": "string"}, "present": {"type": "object"}, "module_count": {"type": "integer", "minimum": 0}, "test_count": {"type": "integer", "minimum": 0}, "modules": {"type": "array", "items": {"type": "string"}}}},
            _inspect_legacy,
        )
    )
    registry.tools.register(
        ToolSpec(
            "golden.validate_taxonomy", "0.1.0", "Validate independent taxonomy columns and parentage", ToolRisk.READ_ONLY,
            {"type": "object", "required": ["taxonomy_csv"], "additionalProperties": False, "properties": {"taxonomy_csv": {"type": "string", "minLength": 1}}},
            {"type": "object", "required": ["path", "sha256", "axis_count", "subaxis_count", "variant_count", "independent_columns_validated"], "additionalProperties": False, "properties": {"path": {"type": "string"}, "sha256": {"type": "string", "pattern": "[0-9a-f]{64}"}, "axis_count": {"type": "integer", "minimum": 1}, "subaxis_count": {"type": "integer", "minimum": 1}, "variant_count": {"type": "integer", "minimum": 1}, "independent_columns_validated": {"type": "boolean"}}},
            _validate_taxonomy,
        )
    )
    registry.register_workflow(
        WorkflowSpec("golden-bootstrap", "Inspect the golden conversation foundation", ("golden", "conversation", "taxonomy", "axes"), _plan)
    )
