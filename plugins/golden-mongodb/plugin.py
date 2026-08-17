from __future__ import annotations

from zen_agent.adapters.mongodb import MongoSettings, ReadOnlyMongoSource
from zen_agent.models import Plan, TaskSpec, ToolRisk
from zen_agent.plugins import WorkflowSpec
from zen_agent.tools import ToolSpec


def _with_source(operation):
    source = ReadOnlyMongoSource(MongoSettings.from_environment())
    try:
        return operation(source)
    finally:
        source.close()


def _audit(_context, _inputs):
    return _with_source(lambda source: source.audit().to_dict())


def _inventory(_context, inputs):
    return _with_source(
        lambda source: source.inventory_agents(int(inputs["max_agents"]))
    )


def _sample(_context, inputs):
    return _with_source(
        lambda source: source.sample_conversations(
            list(inputs["agent_ids"]),
            int(inputs["per_agent"]),
            int(inputs["scan_per_agent"]),
            int(inputs["seed"]),
        )
    )


def _audit_plan(objective, inputs, _max_attempts):
    if inputs:
        raise ValueError("golden-mongo-audit takes no inputs; use environment secrets")
    return Plan(
        "golden-mongo-audit",
        objective,
        (TaskSpec("audit", "Audit MongoDB access", "golden.mongodb_audit", {}, max_attempts=1),),
        "Verify connectivity and record effective privileges; harness tools expose only allowlisted reads.",
        inputs,
    )


def _inventory_plan(objective, inputs, _max_attempts):
    max_agents = inputs.get("max_agents", 100)
    if not isinstance(max_agents, int) or isinstance(max_agents, bool):
        raise ValueError("max_agents must be an integer")
    return Plan(
        "golden-agent-inventory",
        objective,
        (
            TaskSpec(
                "inventory",
                "Inventory agents and bounded conversation counts",
                "golden.inventory_agents",
                {"max_agents": max_agents},
                max_attempts=1,
            ),
        ),
        "Inventory agent_base metadata and indexed call_dispositions counts without fetching transcripts.",
        {"max_agents": max_agents},
    )


def _sample_plan(objective, inputs, _max_attempts):
    agent_ids = inputs.get("agent_ids")
    if not isinstance(agent_ids, list) or not all(
        isinstance(item, str) and item for item in agent_ids
    ):
        raise ValueError("agent_ids must be a JSON array of non-empty strings")
    task_inputs = {
        "agent_ids": agent_ids,
        "per_agent": inputs.get("per_agent", 3),
        "scan_per_agent": inputs.get("scan_per_agent", 100),
        "seed": inputs.get("seed", 20260401),
    }
    return Plan(
        "golden-conversation-sample",
        objective,
        (
            TaskSpec(
                "sample",
                "Fetch and source-bind bounded conversations",
                "golden.sample_conversations",
                task_inputs,
                max_attempts=1,
            ),
        ),
        "Read bounded per-agent pools, preserve exact turns, and bind immutable prompt/content hashes without a model call.",
        task_inputs,
    )


AUDIT_SCHEMA = {
    "type": "object",
    "required": [
        "roles", "write_actions", "server_enforced_read_only",
        "application_operations", "warning",
    ],
    "additionalProperties": False,
    "properties": {
        "roles": {"type": "array", "items": {"type": "string"}},
        "write_actions": {"type": "array", "items": {"type": "string"}},
        "server_enforced_read_only": {"type": "boolean"},
        "application_operations": {"type": "string"},
        "warning": {},
    },
}


def register(registry):
    registry.tools.register(
        ToolSpec(
            "golden.mongodb_audit", "0.2.0",
            "Audit connectivity and record effective MongoDB privileges",
            ToolRisk.READ_ONLY,
            {"type": "object", "additionalProperties": False, "properties": {}},
            AUDIT_SCHEMA,
            _audit,
        )
    )
    registry.tools.register(
        ToolSpec(
            "golden.inventory_agents", "0.2.0",
            "Inventory bounded agent metadata and conversation counts",
            ToolRisk.READ_ONLY,
            {
                "type": "object", "required": ["max_agents"],
                "additionalProperties": False,
                "properties": {"max_agents": {"type": "integer", "minimum": 1, "maximum": 10000}},
            },
            {
                "type": "object",
                "required": ["source", "privilege_audit", "limit", "agents_returned", "agents"],
                "additionalProperties": False,
                "properties": {
                    "source": {"type": "object"}, "privilege_audit": {"type": "object"},
                    "limit": {"type": "integer"}, "agents_returned": {"type": "integer"},
                    "conversation_count_capped_at": {"type": "integer"},
                    "agents": {"type": "array", "items": {"type": "object"}},
                },
            },
            _inventory,
        )
    )
    registry.tools.register(
        ToolSpec(
            "golden.sample_conversations", "0.1.0",
            "Fetch a deterministic bounded sample and bind exact sources",
            ToolRisk.READ_ONLY,
            {
                "type": "object",
                "required": ["agent_ids", "per_agent", "scan_per_agent", "seed"],
                "additionalProperties": False,
                "properties": {
                    "agent_ids": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "string", "minLength": 1}},
                    "per_agent": {"type": "integer", "minimum": 1, "maximum": 10},
                    "scan_per_agent": {"type": "integer", "minimum": 1, "maximum": 500},
                    "seed": {"type": "integer"},
                },
            },
            {
                "type": "object",
                "required": [
                    "source", "privilege_audit", "seed", "requested_agent_ids",
                    "per_agent", "scan_per_agent", "selected_count",
                    "rejection_counts", "conversations",
                ],
                "additionalProperties": False,
                "properties": {
                    "source": {"type": "object"}, "privilege_audit": {"type": "object"},
                    "seed": {"type": "integer"},
                    "requested_agent_ids": {"type": "array", "items": {"type": "string"}},
                    "per_agent": {"type": "integer"}, "scan_per_agent": {"type": "integer"},
                    "selected_count": {"type": "integer"}, "rejection_counts": {"type": "object"},
                    "conversations": {"type": "array", "items": {"type": "object"}},
                },
            },
            _sample,
        )
    )
    registry.register_workflow(
        WorkflowSpec(
            "golden-mongo-audit", "Audit MongoDB access",
            ("mongodb", "mongo", "credential", "access"), _audit_plan,
        )
    )
    registry.register_workflow(
        WorkflowSpec(
            "golden-agent-inventory", "Inventory MongoDB agents",
            ("inventory agents", "shortlist agents", "mongo agents"), _inventory_plan,
        )
    )
    registry.register_workflow(
        WorkflowSpec(
            "golden-conversation-sample", "Source-bind a bounded conversation sample",
            ("sample conversations", "fetch conversations", "source bind"), _sample_plan,
        )
    )
