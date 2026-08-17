from __future__ import annotations

import json
from typing import Any


def normalize_agent_inventory(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Collapse metadata rows to fetchable agent identities without losing variants.

    Mongo trace fetching is keyed by agent_id; effective configuration identity is
    established later from agent version plus the source-bound system-prompt digest.
    Conversation counts on duplicate metadata rows are therefore maxed, not summed.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        agent_id = row.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("inventory row has no agent_id")
        groups.setdefault(agent_id, []).append(row)
    normalized = []
    for agent_id in sorted(groups):
        variants = groups[agent_id]
        languages = sorted({str(value) for row in variants for value in row.get("languages", []) if value})
        projects = sorted({str(row.get("project_name")) for row in variants if row.get("project_name")})
        channels = sorted({str(row.get("channel_type")) for row in variants if row.get("channel_type")})
        normalized.append({
            "agent_id": agent_id,
            "metadata_rows": len(variants),
            "metadata_digest": _metadata_digest(variants),
            "project_names": projects,
            "languages": languages,
            "channel_types": channels,
            "is_inbound": any(bool(row.get("is_inbound")) for row in variants),
            "is_outbound": any(bool(row.get("is_outbound")) for row in variants),
            "allow_language_switch": any(bool(row.get("allow_language_switch")) for row in variants),
            "conversation_count": max(int(row.get("conversation_count", 0)) for row in variants),
        })
    return tuple(normalized)


def _metadata_digest(rows: list[dict[str, Any]]) -> str:
    from hashlib import sha256
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()
