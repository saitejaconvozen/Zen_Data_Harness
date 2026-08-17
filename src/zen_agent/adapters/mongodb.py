from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
import re
from typing import Any


WRITE_ACTIONS = frozenset(
    {
        "insert", "update", "remove", "createCollection", "dropCollection",
        "dropDatabase", "createIndex", "dropIndex", "collMod",
        "renameCollectionSameDB", "bypassDocumentValidation",
    }
)


class MongoConfigurationError(RuntimeError):
    pass


class UnsafeMongoPrivileges(RuntimeError):
    """Retained for compatibility with older callers; privilege breadth is advisory."""


@dataclass(frozen=True, slots=True)
class MongoSettings:
    uri: str = field(repr=False)
    database: str = "test"
    call_collection: str = "call_dispositions"
    agent_collection: str = "agent_base"
    server_selection_timeout_ms: int = 8_000
    query_timeout_ms: int = 60_000
    # Upper bound on the per-agent conversation count. Shortlisting needs a
    # floor, not an exact total, and the cap keeps each count index-bounded.
    inventory_count_cap: int = 1_000

    @classmethod
    def from_environment(cls) -> "MongoSettings":
        uri = os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI")
        if not uri or not uri.strip():
            raise MongoConfigurationError(
                "MONGODB_URI is required; inject it through the process environment"
            )
        database = os.environ.get("MONGODB_DATABASE", "test").strip()
        call_collection = os.environ.get(
            "MONGODB_CALL_COLLECTION", "call_dispositions"
        ).strip()
        agent_collection = os.environ.get(
            "MONGODB_AGENT_COLLECTION", "agent_base"
        ).strip()
        if (database, call_collection, agent_collection) != (
            "test", "call_dispositions", "agent_base"
        ):
            raise MongoConfigurationError(
                "this plugin is allowlisted only for test.call_dispositions and test.agent_base"
            )
        return cls(uri.strip(), database, call_collection, agent_collection)


@dataclass(frozen=True, slots=True)
class MongoPrivilegeAudit:
    roles: tuple[str, ...]
    write_actions: tuple[str, ...]

    @property
    def server_enforced_read_only(self) -> bool:
        return not self.write_actions

    def to_dict(self) -> dict[str, Any]:
        return {
            "roles": list(self.roles),
            "write_actions": list(self.write_actions),
            "server_enforced_read_only": self.server_enforced_read_only,
            "application_operations": "ALLOWLISTED_READ_ONLY",
            "warning": (
                None
                if self.server_enforced_read_only
                else "credential has broader privileges; harness tools still expose only reads"
            ),
        }


def evaluate_privileges(auth_info: dict[str, Any]) -> MongoPrivilegeAudit:
    roles = tuple(
        sorted(
            f"{item.get('role', 'unknown')}@{item.get('db', 'unknown')}"
            for item in auth_info.get("authenticatedUserRoles", [])
        )
    )
    actions = {
        action
        for privilege in auth_info.get("authenticatedUserPrivileges", [])
        for action in privilege.get("actions", [])
        if isinstance(action, str)
    }
    return MongoPrivilegeAudit(roles, tuple(sorted(actions & WRITE_ACTIONS)))


def _hash_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _mongo_id_type(value: Any) -> str:
    return "objectid" if type(value).__name__ == "ObjectId" else "string"


_RUNTIME_METADATA = re.compile(
    r"^\s*<session_metadata>.*?</session_metadata>\s*$", re.I | re.S
)


def _normalise_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Keep the OpenAI tool-call shape, dropping nothing the model must learn."""

    calls = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        calls.append({
            "id": str(item.get("id") or ""),
            "type": str(item.get("type") or "function"),
            "function": {
                "name": str(function.get("name") or ""),
                "arguments": function.get("arguments")
                if isinstance(function.get("arguments"), str)
                else json.dumps(function.get("arguments") or {}, sort_keys=True),
            },
        })
    return calls


def bind_conversation(document: dict[str, Any], max_chars: int = 100_000) -> dict[str, Any]:
    history = document.get("chat_history")
    if not isinstance(history, list):
        raise ValueError("chat_history is not an array")
    system_prompt = ""
    turns = []
    canonical_history = []
    total_chars = 0
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            raise ValueError(f"chat_history[{index}] is not a turn object")
        # An assistant turn that calls a tool carries its action in `tool_calls`
        # and often has null content. Rejecting those dropped every tool-using
        # conversation, and keeping only `content` silently discarded the call —
        # both fatal for training a model to use tools.
        tool_calls = item.get("tool_calls")
        tool_call_id = item.get("tool_call_id")
        raw_content = item.get("content")
        if not isinstance(raw_content, str):
            if tool_calls:
                raw_content = ""
            else:
                raise ValueError(f"chat_history[{index}] is not a textual turn")
        text = raw_content
        total_chars += len(text)
        if total_chars > max_chars:
            raise ValueError("conversation exceeds maximum character budget")
        raw_role = str(item.get("role") or "unknown").strip().lower()
        role = "assistant" if raw_role in {"assistant", "model", "agent"} else raw_role
        canonical = {"source_index": index, "role": role, "content": text}
        if tool_calls:
            canonical["tool_calls"] = tool_calls
        if tool_call_id:
            canonical["tool_call_id"] = tool_call_id
        canonical_history.append(canonical)
        if role == "system" and not system_prompt:
            system_prompt = text
            continue
        if text and _RUNTIME_METADATA.match(text):
            role = "runtime_metadata"
        turn = {
            "source_index": index,
            "role": role,
            "text": text,
            "text_sha256": _hash_text(text),
        }
        if tool_calls:
            turn["tool_calls"] = _normalise_tool_calls(tool_calls)
            turn["tool_calls_sha256"] = _hash_text(
                json.dumps(turn["tool_calls"], sort_keys=True, separators=(",", ":"))
            )
        if tool_call_id:
            turn["tool_call_id"] = str(tool_call_id)
        turns.append(turn)
    if not system_prompt:
        raise ValueError("conversation has no system prompt")
    agent_id = str(document.get("agent_id") or "")
    call_id = str(document.get("call_id") or document.get("_id") or "")
    if not agent_id or not call_id:
        raise ValueError("conversation is missing agent_id or call_id")
    user_turns = sum(item["role"] == "user" for item in turns)
    assistant_turns = sum(item["role"] == "assistant" for item in turns)
    if user_turns < 3 or assistant_turns < 3:
        raise ValueError("conversation has fewer than three user/assistant turns")
    source_payload = json.dumps(
        canonical_history, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    source_id = str(document.get("_id") or "")
    return {
        "source_mongo_id": source_id,
        "source_mongo_id_type": _mongo_id_type(document.get("_id")),
        "call_id": call_id,
        "agent_id": agent_id,
        "agent_version": (
            str(document["agent_version"])
            if document.get("agent_version") is not None
            else None
        ),
        "created_at": str(document.get("creation_date") or ""),
        "duration_seconds": float(document.get("duration") or 0.0),
        "system_prompt": system_prompt,
        "system_prompt_sha256": _hash_text(system_prompt),
        "source_content_sha256": _hash_text(source_payload),
        "user_turn_count": user_turns,
        "assistant_turn_count": assistant_turns,
        "turns": turns,
    }


class ReadOnlyMongoSource:
    """Expose only bounded reads over two allowlisted MongoDB collections."""

    def __init__(self, settings: MongoSettings):
        try:
            from pymongo import MongoClient
        except ImportError as exc:
            raise RuntimeError(
                "pymongo is required; install requirements-production.txt"
            ) from exc
        self.settings = settings
        self.client = MongoClient(
            settings.uri,
            serverSelectionTimeoutMS=settings.server_selection_timeout_ms,
            retryWrites=False,
            appname="zen-agent-harness-read-operations-only",
        )

    def close(self) -> None:
        self.client.close()

    def audit(self) -> MongoPrivilegeAudit:
        self.client.admin.command("ping")
        status = self.client.admin.command(
            {"connectionStatus": 1, "showPrivileges": True}
        )
        return evaluate_privileges(status.get("authInfo", {}))

    def inventory_agents(self, max_agents: int) -> dict[str, Any]:
        if not 1 <= max_agents <= 10_000:
            raise ValueError("max_agents must be between 1 and 10,000")
        audit = self.audit()
        database = self.client[self.settings.database]
        agents = list(
            database[self.settings.agent_collection]
            .find(
                {"agent_id": {"$type": "string", "$ne": ""}},
                {
                    "_id": 0, "agent_id": 1, "agent_name": 1, "project_name": 1,
                    "languages": 1, "channel_type": 1, "is_inbound": 1,
                    "is_outbound": 1, "allow_language_switch": 1, "created_at": 1,
                },
                max_time_ms=self.settings.query_timeout_ms,
            )
            .sort("agent_id", 1)
            .limit(max_agents)
        )
        agent_ids = [item["agent_id"] for item in agents]
        counts: dict[str, int] = {}
        calls = database[self.settings.call_collection]
        # A grouped count walks every matching document, which does not finish
        # against a call collection of tens of millions. Shortlisting only needs
        # to know an agent has enough conversations, so count with an early exit:
        # index-backed and bounded at roughly 3ms per agent.
        cap = self.settings.inventory_count_cap
        for agent_id in agent_ids:
            counts[str(agent_id)] = int(
                calls.count_documents(
                    {"agent_id": agent_id},
                    limit=cap,
                    hint="agent_id_-1",
                    maxTimeMS=self.settings.query_timeout_ms,
                )
            )
        normalized = []
        for item in agents:
            agent_id = str(item["agent_id"])
            normalized.append(
                {
                    "agent_id": agent_id,
                    "agent_name": str(item.get("agent_name") or ""),
                    "project_name": str(item.get("project_name") or ""),
                    "languages": [str(value) for value in item.get("languages") or []],
                    "channel_type": str(item.get("channel_type") or ""),
                    "is_inbound": bool(item.get("is_inbound", False)),
                    "is_outbound": bool(item.get("is_outbound", False)),
                    "allow_language_switch": bool(item.get("allow_language_switch", False)),
                    "created_at": str(item.get("created_at") or ""),
                    "conversation_count": counts.get(agent_id, 0),
                }
            )
        return {
            "source": self._source_manifest(),
            "privilege_audit": audit.to_dict(),
            "limit": max_agents,
            "conversation_count_capped_at": self.settings.inventory_count_cap,
            "agents_returned": len(normalized),
            "agents": normalized,
        }

    def sample_conversations(
        self,
        agent_ids: list[str],
        per_agent: int,
        scan_per_agent: int,
        seed: int,
    ) -> dict[str, Any]:
        if not agent_ids or len(agent_ids) > 50:
            raise ValueError("agent_ids must contain between 1 and 50 agents")
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("agent_ids contains duplicates")
        if not 1 <= per_agent <= 10:
            raise ValueError("per_agent must be between 1 and 10")
        if not per_agent <= scan_per_agent <= 500:
            raise ValueError("scan_per_agent must be between per_agent and 500")
        audit = self.audit()
        calls = self.client[self.settings.database][self.settings.call_collection]
        selected = []
        rejection_counts: dict[str, int] = {}
        for agent_id in agent_ids:
            documents = list(
                calls.find(
                    {"agent_id": agent_id, "chat_history.5": {"$exists": True}},
                    {
                        "_id": 1, "agent_id": 1, "agent_version": 1, "call_id": 1,
                        "creation_date": 1, "duration": 1, "chat_history": 1,
                    },
                    max_time_ms=self.settings.query_timeout_ms,
                )
                .hint("agent_id_-1")
                .limit(scan_per_agent)
            )
            ranked = sorted(
                documents,
                key=lambda item: _hash_text(
                    f"{seed}\x1f{agent_id}\x1f{str(item.get('_id') or '')}"
                ),
            )
            accepted_for_agent = 0
            for document in ranked:
                try:
                    bound = bind_conversation(document)
                    if bound["duration_seconds"] < 20.0:
                        raise ValueError("conversation duration is below 20 seconds")
                except Exception as exc:
                    key = str(exc)
                    rejection_counts[key] = rejection_counts.get(key, 0) + 1
                    continue
                selected.append(bound)
                accepted_for_agent += 1
                if accepted_for_agent >= per_agent:
                    break
        return {
            "source": self._source_manifest(),
            "privilege_audit": audit.to_dict(),
            "seed": seed,
            "requested_agent_ids": agent_ids,
            "per_agent": per_agent,
            "scan_per_agent": scan_per_agent,
            "selected_count": len(selected),
            "rejection_counts": rejection_counts,
            "conversations": selected,
        }

    def _source_manifest(self) -> dict[str, Any]:
        return {
            "database": self.settings.database,
            "agent_collection": self.settings.agent_collection,
            "call_collection": self.settings.call_collection,
            "application_mode": "ALLOWLISTED_READ_ONLY_OPERATIONS",
        }
