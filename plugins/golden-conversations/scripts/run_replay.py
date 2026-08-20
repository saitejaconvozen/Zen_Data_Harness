"""Complete a conversation whose corrected turn broke the recorded reply.

Correcting assistant turn N invalidates everything after it: the caller's turn
N+1 was a reply to the *original* turn N, not the corrected one. The harness
normally truncates there, which is honest but costly — 303 conversations shipped
only a prefix.

Replay finishes those trajectories by simulating the rest of the call. It is a
last resort, not a default, because it trades away this system's strongest
guarantee: that every user turn is byte-identical to what a real person said.
Replayed turns are synthetic. They are flagged individually, kept in a separate
dataset, and never merged with byte-faithful data.

Three constraints govern it:

* **Only when there is no alternative.** Truncation is preferred whenever the
  surviving prefix is usable on its own. Replay runs only when divergence lands
  early enough that truncating would waste a conversation worth keeping.
* **Only on high-quality conversations.** Simulating the remainder of a call
  that was mediocre to begin with manufactures mediocre data at real cost.
* **The persona never drifts.** The caller is a specific person with a goal, a
  language, a register, and facts they have already stated. A simulator that
  re-derives them each turn slides towards a generic cooperative user, and the
  dialogue stops resembling a real call. So the persona is extracted **once**
  from the real prefix and held fixed for every simulated turn.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _transport import active_model, run_model  # noqa: E402


# A replayed tail longer than this stops being a completion and becomes an
# invention: the further from real dialogue, the weaker the training signal.
MAX_SYNTHETIC_EXCHANGES = 6

# Below this many real assistant turns, truncation already yields nothing, and
# replaying would produce a conversation that is mostly synthetic.
MIN_REAL_PREFIX_TURNS = 3


PERSONA_SCHEMA = {
    "type": "object",
    "required": ["goal", "language", "register", "stated_facts", "constraints"],
    "additionalProperties": False,
    "properties": {
        "goal": {"type": "string", "minLength": 10,
                 "description": "What this caller is trying to achieve."},
        "language": {"type": "string",
                     "description": "The language and script they actually speak in, "
                                    "including romanised or mixed forms."},
        "register": {"type": "string",
                     "description": "How they speak: terse or discursive, formal or "
                                    "casual, patient or frustrated."},
        "stated_facts": {
            "type": "array", "maxItems": 30, "items": {"type": "string"},
            "description": "Everything the caller has already said about themselves "
                           "or their situation. A later turn must never contradict "
                           "these.",
        },
        "constraints": {
            "type": "array", "maxItems": 20, "items": {"type": "string"},
            "description": "What this caller would not plausibly say or do.",
        },
    },
}

USER_TURN_SCHEMA = {
    "type": "object",
    "required": ["text", "conversation_should_end", "reason"],
    "additionalProperties": False,
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        "conversation_should_end": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 5},
    },
}

ASSISTANT_TURN_SCHEMA = {
    "type": "object",
    "required": ["text", "follows_system_prompt", "reason"],
    "additionalProperties": False,
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
        "follows_system_prompt": {"type": "boolean"},
        "reason": {"type": "string", "minLength": 5},
        # A replay may invent a backend action and its outcome. Recording the
        # call rather than only its effect keeps the trajectory usable for
        # tool-call training instead of teaching the model to assert results
        # out of nowhere.
        "tool_call": {
            "type": "object",
            "required": ["name", "arguments", "invented_result"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "arguments": {"type": "string"},
                "invented_result": {"type": "string"},
            },
        },
    },
}


def render(turns: list[dict]) -> str:
    lines = []
    for turn in turns:
        marker = " [SIMULATED]" if turn.get("synthetic") else ""
        lines.append(f"{turn['role']}{marker}: {turn['text']}")
    return "\n".join(lines)


def persona_prompt(turns: list[dict]) -> str:
    return (
        "Below is the real, recorded part of a phone call. Describe the CALLER "
        "as a person, from evidence in their own words only.\n\n"
        "Do not describe the assistant. Do not invent details the caller never "
        "gave. If they never stated something, leave it out — a persona that "
        "asserts more than the transcript supports will drift.\n\n"
        + render(turns)
    )


def user_turn_prompt(persona: dict, turns: list[dict]) -> str:
    return (
        "You are continuing a real phone call by writing the CALLER's next turn.\n\n"
        "# The caller (fixed; do not drift from this)\n"
        f"{json.dumps(persona, ensure_ascii=False, indent=2)}\n\n"
        "Rules:\n"
        "- Write only what this caller would say next, in their own language and "
        "register. Match how they have been speaking, including mixed or "
        "romanised language.\n"
        "- Never contradict anything in stated_facts.\n"
        "- Real callers are brief, sometimes vague, sometimes repeat themselves. "
        "Do not write a polished or unusually cooperative customer.\n"
        "- Set conversation_should_end true when this caller's goal is met or "
        "they would plausibly hang up.\n\n"
        "# Conversation so far\n" + render(turns)
    )


def assistant_turn_prompt(system_prompt: str, turns: list[dict]) -> str:
    return (
        "You are the voice agent. Write your next turn, obeying the "
        "configuration below exactly. This turn becomes fine-tuning data, so it "
        "must be what the agent *should* say, not merely what is acceptable.\n\n"
        "This conversation is synthetic, so there is no real backend. When an "
        "action is required, take it: emit a tool_call with plausible arguments "
        "and a plausible invented_result, then continue as though it succeeded. "
        "Do not stall, do not put the caller on hold indefinitely, and do not "
        "repeat a previous turn — drive the call to its natural conclusion.\n\n"
        "# Agent configuration\n" + system_prompt + "\n\n"
        "# Conversation so far\n" + render(turns)
    )


def call(role: str, prompt: str, schema: dict, job: Path, tag: str) -> dict:
    schema_path = job / f"{tag}.schema.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    return run_model(
        prompt=prompt,
        schema_path=schema_path,
        output_path=job / f"{tag}.json",
        log_path=job / f"{tag}.log",
        role=role,
        timeout=600,
    )


def replay(packet: dict, golden_turns: dict, divergent_turn_id: str, job: Path) -> dict:
    """Rebuild the tail of a conversation after its first divergent turn."""
    ordered = packet["turns"]
    index = next(
        (i for i, t in enumerate(ordered) if t.get("turn_id") == divergent_turn_id),
        None,
    )
    if index is None:
        raise ValueError(f"divergent turn {divergent_turn_id} not in packet")

    # Everything before the divergence is real; the divergent turn itself keeps
    # its corrected text, since that correction is the reason we are here.
    prefix: list[dict] = []
    for turn in ordered[:index]:
        prefix.append({
            "turn_id": turn["turn_id"], "role": turn["role"],
            "text": golden_turns.get(turn["turn_id"], turn["text"]),
            "synthetic": False,
        })
    corrected = golden_turns.get(divergent_turn_id, ordered[index]["text"])
    prefix.append({
        "turn_id": divergent_turn_id, "role": ordered[index]["role"],
        "text": corrected, "synthetic": False, "corrected": True,
    })

    real_assistant = sum(1 for t in prefix if t["role"] == "assistant")
    if real_assistant < MIN_REAL_PREFIX_TURNS:
        return {
            "status": "SKIPPED",
            "reason": (
                f"only {real_assistant} real assistant turns precede the divergence; "
                "the result would be mostly synthetic"
            ),
        }

    persona = call("PERSONA_EXTRACTOR", persona_prompt(prefix), PERSONA_SCHEMA,
                   job, "persona")
    persona = {k: v for k, v in persona.items() if not k.startswith("zen_")}

    conversation = list(prefix)
    synthetic = 0
    ended = False
    stalled = False
    for exchange in range(1, MAX_SYNTHETIC_EXCHANGES + 1):
        user = call("USER_SIMULATOR", user_turn_prompt(persona, conversation),
                    USER_TURN_SCHEMA, job, f"user-{exchange}")
        conversation.append({
            "turn_id": f"sim_user_{exchange:03d}", "role": "user",
            "text": user["text"], "synthetic": True,
        })
        if user.get("conversation_should_end"):
            ended = True
            break
        assistant = call("ASSISTANT_ROLLOUT",
                         assistant_turn_prompt(packet["system_prompt"], conversation),
                         ASSISTANT_TURN_SCHEMA, job, f"assistant-{exchange}")
        turn = {
            "turn_id": f"sim_assistant_{exchange:03d}", "role": "assistant",
            "text": assistant["text"], "synthetic": True,
            "follows_system_prompt": assistant.get("follows_system_prompt"),
        }
        if isinstance(assistant.get("tool_call"), dict):
            turn["tool_call"] = assistant["tool_call"]
        conversation.append(turn)
        synthetic += 1

        # Stall guard. Without a real backend a grounded agent can hold forever,
        # and a corpus of "please continue holding" is worse than no corpus.
        recent = [
            item["text"].strip().lower()
            for item in conversation if item["role"] == "assistant"
        ][-3:]
        if len(recent) == 3 and len(set(recent)) < 3:
            stalled = True
            break

    # A caller turn with no reply teaches nothing; drop a dangling tail.
    while conversation and conversation[-1]["role"] == "user":
        conversation.pop()

    if stalled:
        return {
            "status": "ABANDONED",
            "reason": "the assistant repeated itself; a stalled tail is not usable data",
            "packet_id": packet["packet_id"],
        }
    return {
        "status": "REPLAYED",
        "packet_id": packet["packet_id"],
        "divergent_turn_id": divergent_turn_id,
        "persona": persona,
        "real_turns": len(prefix),
        "synthetic_exchanges": synthetic,
        "ended_naturally": ended,
        "stalled": stalled,
        "model_id": active_model(),
        "replayed_at": datetime.now(timezone.utc).isoformat(),
        "turns": conversation,
        # Stated on the record, not inferred by a reader: this conversation is
        # not byte-faithful and must never join the byte-faithful dataset.
        "provenance": "PARTIALLY_SYNTHETIC_NOT_BYTE_FAITHFUL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a divergent conversation tail")
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--refiner-decision", type=Path, required=True)
    parser.add_argument("--divergent-turn-id", required=True)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    decision = json.loads(args.refiner_decision.read_text(encoding="utf-8"))
    rows = decision.get("decision", decision).get("assistant_turns", [])
    golden = {
        row["turn_id"]: row.get("golden_text_final") or row.get("golden_text") or ""
        for row in rows
        if row.get("golden_text") or row.get("golden_text_final")
    }

    result = replay(packet, golden, args.divergent_turn_id, args.job)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(json.dumps({k: v for k, v in result.items() if k != "turns"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
