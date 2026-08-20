"""Emit fine-tuning data from finished runs, as versioned datasets.

A run is a unit of work, not a unit of release. Work done in earlier runs is
still good data, and re-running the harness later produces a second, better
dataset rather than replacing the first. So releases are versioned explicitly:
each version names the runs it draws from, and the exporter writes a JSONL file
plus a manifest recording exactly what went in.

Message shape is the OpenAI chat format, including tool calls, because that is
what the fine-tune consumes:

    {"messages": [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "", "tool_calls": [...]},
        {"role": "tool", "tool_call_id": "...", "content": "..."},
        {"role": "assistant", "content": "..."}
    ]}

Two rules govern what is emitted, and both exist to keep the dataset honest:

* **A divergent turn truncates the conversation.** Its replacement no longer
  fits the reply that actually followed, so everything after it is fiction. The
  conversation is cut before it and the coherent prefix ships. Dropping it from
  the middle instead would splice together two utterances that never followed
  one another.
* **An unverified turn is masked, not cut.** When the refiner kept the source
  text but could not establish the evidence for it, the dialogue is still real —
  it is simply not exemplary. Cutting there discarded conversations wholesale
  (136 of them, plus the tail of many more). Instead the turn ships and its
  index is recorded in `mask_assistant_indices`, so training can exclude it from
  the loss while keeping the surrounding context intact.
* **Only source tool calls appear.** The refiner is forbidden from inventing a
  call, and the exporter does not reinstate one either.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import json
import re
from pathlib import Path
from typing import Any

from .factory_qualification import FactoryQualificationStore
from .factory_review import _packet, build_review


# Terminal states whose turns are eligible for release. A partial candidate is
# included because its surviving prefix is exactly as verified as a full one.
RELEASABLE = ("VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE")

# Replayed conversations carry simulated caller turns. They are a different
# product from byte-faithful data and are exported to their own file; mixing
# them would destroy the guarantee that every user turn is what a real person
# said, with no way to tell afterwards which rows were affected.
REPLAY_PROVENANCE = "PARTIALLY_SYNTHETIC_NOT_BYTE_FAITHFUL"

# Below this many assistant turns there is not enough dialogue to train on.
MIN_ASSISTANT_TURNS = 3

# In SFT every assistant turn is a training target: the model is being taught to
# say exactly this. A turn with no speech teaches it to answer with nothing, and
# a two-character grunt teaches it to stall. Measured before this gate: 1,368
# assistant turns were the empty string. Control tags are protocol, not speech,
# so they do not count toward the floor.
MIN_TARGET_SPEECH = 5
_CONTROL_TAG = re.compile(r"<\|[A-Z_]+\|>|\b(?:WAITING|ENDCALL)\s*\d*\b")


def spoken_length(text: str) -> int:
    """Characters actually addressed to the caller."""
    return len(_CONTROL_TAG.sub("", text or "").strip())


def _system_prompts(root: Path, run_id: str) -> dict[str, str]:
    """Map packet_id to system prompt, read from the immutable packet batches."""
    store = FactoryQualificationStore(root / ".zen" / "factory-qualification.db")
    try:
        cache: dict[str, list[dict]] = {}
        prompts = {}
        for sample in store.samples(run_id):
            try:
                packet = _packet(sample, cache)
            except (OSError, ValueError, KeyError, IndexError):
                continue
            prompt = packet.get("system_prompt")
            if prompt:
                prompts[packet["packet_id"]] = prompt
        return prompts
    finally:
        store.close()


def conversation_messages(
    conversation: Mapping[str, Any], system_prompt: str | None
) -> tuple[list[dict[str, Any]], list[int]] | None:
    """Render one conversation as chat messages, or None if nothing is usable.

    Stops at the first divergent turn, so the emitted dialogue is a real prefix
    of the recorded call rather than a splice. Returns the messages together with
    the assistant-turn indices to mask out of the training loss.
    """
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    assistant_turns = 0
    masked: list[int] = []
    for turn in conversation.get("turns", []):
        role = turn.get("role")
        if role == "user":
            messages.append({"role": "user", "content": turn["text"]})
            continue
        if role == "tool":
            messages.append({
                "role": "tool",
                "tool_call_id": turn.get("tool_call_id"),
                "content": turn.get("text", ""),
            })
            continue
        # Divergence breaks what follows; nothing after it can be trusted.
        if turn.get("downstream_coherence") == "DIVERGENT":
            break
        if turn.get("action") == "NOT_REFINED":
            break
        # KEEP turns carry no separate golden text; the source is the golden text.
        content = turn.get("golden_text")
        if content is None:
            content = turn.get("source_text", "")
        # A turn with a tool call is allowed to be brief, but not silent; one
        # without is a pure speech target and must actually say something.
        if spoken_length(content) < MIN_TARGET_SPEECH and not turn.get("tool_calls"):
            masked.append(assistant_turns)
            messages.append({"role": "assistant", "content": content})
            assistant_turns += 1
            continue
        message: dict[str, Any] = {"role": "assistant", "content": content}
        tool_calls = turn.get("golden_tool_calls")
        if tool_calls:
            message["tool_calls"] = tool_calls
        if turn.get("excluded_from_golden"):
            masked.append(assistant_turns)
        messages.append(message)
        assistant_turns += 1

    # A trailing user turn or an orphaned tool result teaches nothing.
    while messages and messages[-1]["role"] in {"user", "tool"}:
        messages.pop()
    # Masked turns carry no training signal, so they do not count towards the
    # minimum: a conversation of three unverified turns teaches nothing.
    if assistant_turns - len(masked) < MIN_ASSISTANT_TURNS:
        return None
    return messages, masked


def _record(
    conversation: Mapping[str, Any],
    messages: Sequence[dict[str, Any]],
    run_id: str,
    masked: Sequence[int] = (),
) -> dict[str, Any]:
    return {
        "messages": list(messages),
        "metadata": {
            "source_id": conversation["source_id_full"],
            "run_id": run_id,
            "terminal_status": conversation["terminal"]["status"],
            "assistant_turns": sum(m["role"] == "assistant" for m in messages),
            "tool_call_turns": sum("tool_calls" in m for m in messages),
            # Zero-based positions among assistant turns whose content is real
            # but unverified; exclude these from the training loss.
            "mask_assistant_indices": list(masked),
            "domain": (conversation.get("classification") or {}).get("domain"),
            "primary_language": (
                (conversation.get("classification") or {}).get("primary_language")
            ),
            # Truncated conversations are honest partials; say so in the data.
            "truncated": conversation["terminal"]["status"] == "PARTIAL_CANDIDATE",
        },
    }


def export_replays(root: Path, run_ids: Iterable[str], *, out_dir: Path) -> dict[str, Any]:
    """Write replayed trajectories to their own dataset, never the main one."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "v3-replay.jsonl"
    manifest: dict[str, Any] = {
        "version": "v3-replay", "conversations": 0, "synthetic_turns": 0,
        "tool_call_turns": 0, "abandoned": 0, "skipped": 0,
        "provenance": REPLAY_PROVENANCE,
    }
    with path.open("w", encoding="utf-8") as handle:
        for run_id in run_ids:
            jobs = root / ".zen" / "factory-jobs" / run_id
            for replay_file in sorted(jobs.glob("*/replay.json")):
                try:
                    record = json.loads(replay_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if record.get("status") == "ABANDONED":
                    manifest["abandoned"] += 1
                    continue
                if record.get("status") != "REPLAYED":
                    manifest["skipped"] += 1
                    continue
                messages = []
                synthetic_positions = []
                for turn in record.get("turns", []):
                    role = turn.get("role")
                    message: dict[str, Any] = {"role": role, "content": turn.get("text", "")}
                    call = turn.get("tool_call")
                    if role == "assistant" and isinstance(call, dict):
                        message["tool_calls"] = [{
                            "id": f"call_{turn['turn_id']}",
                            "type": "function",
                            "function": {
                                "name": call.get("name", ""),
                                "arguments": call.get("arguments", "{}"),
                            },
                        }]
                        manifest["tool_call_turns"] += 1
                    if turn.get("synthetic"):
                        synthetic_positions.append(len(messages))
                        manifest["synthetic_turns"] += 1
                    messages.append(message)
                handle.write(json.dumps({
                    "messages": messages,
                    "metadata": {
                        "source_id": record.get("packet_id"),
                        "run_id": run_id,
                        "provenance": REPLAY_PROVENANCE,
                        "synthetic_message_indices": synthetic_positions,
                        "real_turns": record.get("real_turns"),
                        "ended_naturally": record.get("ended_naturally"),
                        "persona": record.get("persona"),
                        "model_id": record.get("model_id"),
                    },
                }, ensure_ascii=False) + "\n")
                manifest["conversations"] += 1
    path.chmod(0o600)
    manifest["path"] = str(path)
    return manifest


def export_version(
    root: Path,
    run_ids: Iterable[str],
    *,
    version: str,
    out_dir: Path,
    exclude_source_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Write one dataset version drawn from the given runs.

    `exclude_source_ids` lets a later version omit conversations an earlier one
    already released, so versions can be concatenated without duplicates.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{version}.jsonl"
    seen: set[str] = set(exclude_source_ids or ())
    manifest: dict[str, Any] = {
        "version": version,
        "runs": [],
        "conversations": 0,
        "assistant_turns": 0,
        "tool_call_turns": 0,
        "skipped_too_short": 0,
        "skipped_duplicate": 0,
        "masked_turns": 0,
    }

    with path.open("w", encoding="utf-8") as handle:
        for run_id in run_ids:
            prompts = _system_prompts(root, run_id)
            review = build_review(root, run_id)
            written = 0
            for conversation in review["conversations"]:
                if conversation["terminal"]["status"] not in RELEASABLE:
                    continue
                source_id = conversation["source_id_full"]
                if source_id in seen:
                    manifest["skipped_duplicate"] += 1
                    continue
                rendered = conversation_messages(
                    conversation, prompts.get(conversation["packet_id"])
                )
                if rendered is None:
                    manifest["skipped_too_short"] += 1
                    continue
                messages, masked = rendered
                seen.add(source_id)
                record = _record(conversation, messages, run_id, masked)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1
                manifest["conversations"] += 1
                manifest["assistant_turns"] += record["metadata"]["assistant_turns"]
                manifest["tool_call_turns"] += record["metadata"]["tool_call_turns"]
                manifest["masked_turns"] += len(masked)
            manifest["runs"].append({"run_id": run_id, "conversations": written})

    path.chmod(0o600)
    manifest["path"] = str(path)
    manifest["source_ids"] = sorted(seen)
    return manifest
