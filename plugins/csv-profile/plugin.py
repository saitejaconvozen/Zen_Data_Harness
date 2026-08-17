from __future__ import annotations

import csv
from pathlib import Path

from zen_agent.models import Plan, TaskSpec, ToolRisk
from zen_agent.plugins import WorkflowSpec
from zen_agent.tools import ToolSpec


def _profile(context, inputs):
    path = Path(inputs["path"]).resolve()
    if context.workspace != path and context.workspace not in path.parents:
        raise PermissionError("csv-profile reads only inside the harness workspace")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header")
        columns = list(reader.fieldnames)
        rows = list(reader)
    nulls = {column: sum(not (row.get(column) or "").strip() for row in rows) for column in columns}
    distinct = {column: len({row.get(column, "") for row in rows}) for column in columns}
    return {"path": str(path), "rows": len(rows), "columns": columns, "nulls": nulls, "distinct": distinct}


def _plan(objective, inputs, max_attempts):
    if "path" not in inputs:
        raise ValueError("csv-profile requires --input path=<workspace CSV>")
    return Plan(
        workflow="csv-profile",
        objective=objective,
        explanation="Profile one workspace-local CSV and commit the validated result.",
        inputs=inputs,
        tasks=(TaskSpec("profile", "Profile CSV", "csv.profile", {"path": inputs["path"]}, max_attempts=max_attempts),),
    )


def register(registry):
    registry.tools.register(
        ToolSpec(
            name="csv.profile",
            version="1.0.0",
            description="Read and profile a workspace-local CSV",
            risk=ToolRisk.READ_ONLY,
            input_schema={"type": "object", "required": ["path"], "additionalProperties": False, "properties": {"path": {"type": "string", "minLength": 1}}},
            output_schema={
                "type": "object",
                "required": ["path", "rows", "columns", "nulls", "distinct"],
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"}, "rows": {"type": "integer", "minimum": 0},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "nulls": {"type": "object"}, "distinct": {"type": "object"}
                },
            },
            handler=_profile,
        )
    )
    registry.register_workflow(WorkflowSpec("csv-profile", "Profile a CSV", ("csv", "tabular", "profile"), _plan))
