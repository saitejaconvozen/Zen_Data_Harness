from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .factory_queue import LocalFactoryQueue


SCHEMA_VERSION = "zen.human-feedback/1"
STAGE = "human_feedback_repair"
TOOL = "golden.human_feedback_repair"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DECISION_ID_RE = re.compile(r"^hfd_[0-9a-f]{64}$")
_PACKET_ID_RE = re.compile(r"^rp_[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FORBIDDEN_KEYS = {
    "policy_mutation",
    "policy_mutations",
    "taxonomy_mutation",
    "taxonomy_mutations",
    "skill_mutation",
    "skill_mutations",
    "shared_mutation",
    "shared_mutations",
    "requested_mutation",
    "requested_mutations",
}
_MUTATION_TEXT_RE = re.compile(
    r"\b(?:change|edit|modify|rewrite|update|replace|disable|delete|remove)\b"
    r".{0,48}\b(?:zen\.md|policy|policies|taxonomy|taxonomies|skill|skills|"
    r"system prompt|release criteria)\b",
    re.IGNORECASE,
)


class FeedbackRoutingError(ValueError):
    """An approved review decision cannot safely enter the repair queue."""


class FeedbackPolicyViolation(FeedbackRoutingError):
    """Human feedback attempted to mutate shared governed resources."""


@dataclass(frozen=True, slots=True)
class FeedbackRoute:
    run_id: str
    packet_id: str
    review_decision_id: str
    round_number: int
    job_key: str
    enqueued: bool


class FeedbackRouter:
    """Validate approved turn feedback and route one source-bound repair.

    This class deliberately has a narrow responsibility. It neither interprets
    feedback nor edits a conversation. It binds a human decision to the original
    packet and immutable user turns, then places an idempotent, round-scoped item
    on the durable queue for a separately implemented repair tool.
    """

    def __init__(
        self,
        workspace: Path,
        queue: LocalFactoryQueue,
        run_id: str,
        *,
        max_feedback_rounds: int = 3,
    ) -> None:
        self.workspace = workspace.resolve()
        self.queue = queue
        self.run_id = _required_string(run_id, "run_id")
        if not _RUN_ID_RE.fullmatch(self.run_id):
            raise FeedbackRoutingError("run_id has an invalid format")
        if not 1 <= max_feedback_rounds <= 10:
            raise FeedbackRoutingError("max_feedback_rounds must be between 1 and 10")
        self.max_feedback_rounds = max_feedback_rounds

    def route(self, decision: dict[str, Any]) -> FeedbackRoute:
        _reject_shared_mutations(decision)
        normalized = self._validate_shape(decision)
        locator = normalized["packet_locator"]
        packet = self._load_packet(locator)
        immutable_user_turns = self._validate_identity(normalized, packet)
        self._validate_targets(normalized["feedback"]["targets"], packet)

        decision_sha256 = sha256(
            json.dumps(
                normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()

        all_prior = self._all_feedback_items()
        for row in all_prior:
            if row["payload"].get("review_decision_id") == normalized["decision_id"]:
                if row["payload"].get("review_decision_sha256") != decision_sha256:
                    raise FeedbackRoutingError(
                        "review decision identity was reused with different content"
                    )
                if row["payload"].get("packet_id") != normalized["packet_id"]:
                    raise FeedbackRoutingError(
                        "review decision identity was reused for another packet"
                    )
                return FeedbackRoute(
                    self.run_id,
                    normalized["packet_id"],
                    normalized["decision_id"],
                    int(row["payload"]["feedback_round_number"]),
                    row["job_key"],
                    False,
                )

        prior = [
            row for row in all_prior
            if row["payload"].get("packet_id") == normalized["packet_id"]
        ]
        round_number = 0 if not prior else max(
            int(row["payload"]["feedback_round_number"]) for row in prior
        ) + 1
        if round_number >= self.max_feedback_rounds:
            raise FeedbackRoutingError(
                f"maximum human-feedback rounds exhausted for {normalized['packet_id']}"
            )

        graph_root = (
            self.workspace / ".zen" / "graph-jobs" / self.run_id
            / normalized["packet_id"]
        )
        used_graph_rounds = []
        if graph_root.is_dir():
            for path in graph_root.glob("round-*"):
                try:
                    used_graph_rounds.append(int(path.name.removeprefix("round-")))
                except ValueError:
                    continue
        graph_round_number = max(used_graph_rounds, default=-1) + 1
        # Give each immutable review decision an isolated terminal lineage. A
        # prior conversation terminal must never suppress a feedback revision.
        conversation_job_key = (
            f"conversation-{normalized['source_content_sha256']}:"
            f"review-{normalized['decision_id']}"
        )
        job_key = f"{conversation_job_key}:human-feedback-round-{round_number:02d}"
        feedback = {
            "schema_version": SCHEMA_VERSION,
            "decision_id": normalized["decision_id"],
            "reviewer_id": normalized["approval"]["reviewer_id"],
            "approved_at": normalized["approval"]["approved_at"],
            "targets": normalized["feedback"]["targets"],
        }
        # Keep the source bindings both beside the tool input (for queue/control
        # checks) and inside it (for the tool boundary). Do not replace the
        # locator with a generated copy or a later review-site artifact.
        payload = {
            "tool": TOOL,
            "inputs": {
                "packet_batch": locator["packet_batch"],
                "packet_index": locator["packet_index"],
                "packet_id": normalized["packet_id"],
                "source_decision_run_id": self.run_id,
                "round_number": graph_round_number,
                "human_feedback": feedback,
                "source_binding": {
                    "source_content_sha256": normalized["source_content_sha256"],
                    "user_turn_sha256": [row["text_sha256"] for row in immutable_user_turns],
                },
            },
            "source_content_sha256": normalized["source_content_sha256"],
            "packet_id": normalized["packet_id"],
            "conversation_job_key": conversation_job_key,
            "review_decision_id": normalized["decision_id"],
            "review_decision_sha256": decision_sha256,
            "feedback_round_number": round_number,
            "graph_round_number": graph_round_number,
            "max_feedback_rounds": self.max_feedback_rounds,
            "max_repair_rounds": graph_round_number + self.max_feedback_rounds,
            "packet_locator": dict(locator),
            "user_turn_sha256": [row["text_sha256"] for row in immutable_user_turns],
        }
        inserted = self.queue.enqueue(
            self.run_id,
            job_key,
            STAGE,
            payload,
            max_attempts=2,
            priority=75,
        )
        if not inserted:
            existing = self.queue.item(self.run_id, job_key, STAGE)
            if existing["payload"].get("review_decision_id") != normalized["decision_id"]:
                raise FeedbackRoutingError(
                    "feedback round was concurrently occupied by another review decision"
                )
        return FeedbackRoute(
            self.run_id,
            normalized["packet_id"],
            normalized["decision_id"],
            round_number,
            job_key,
            inserted,
        )

    def _validate_shape(self, decision: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(decision, dict):
            raise FeedbackRoutingError("review decision must be an object")
        required = {
            "schema_version", "decision_id", "run_id", "packet_id",
            "source_content_sha256", "packet_locator", "approval", "feedback",
        }
        extra = set(decision) - required
        missing = required - set(decision)
        if missing:
            raise FeedbackRoutingError(f"review decision is missing: {sorted(missing)}")
        if extra:
            raise FeedbackRoutingError(f"review decision has unsupported fields: {sorted(extra)}")
        if decision["schema_version"] != SCHEMA_VERSION:
            raise FeedbackRoutingError("unsupported review decision schema")
        if not _DECISION_ID_RE.fullmatch(_required_string(decision["decision_id"], "decision_id")):
            raise FeedbackRoutingError("invalid review decision identity")
        if decision["run_id"] != self.run_id:
            raise FeedbackRoutingError("review decision run identity mismatch")
        if not _PACKET_ID_RE.fullmatch(_required_string(decision["packet_id"], "packet_id")):
            raise FeedbackRoutingError("invalid packet identity")
        if not _DIGEST_RE.fullmatch(
            _required_string(decision["source_content_sha256"], "source_content_sha256")
        ):
            raise FeedbackRoutingError("invalid source content identity")

        locator = decision["packet_locator"]
        if not isinstance(locator, dict) or set(locator) != {"packet_batch", "packet_index"}:
            raise FeedbackRoutingError("packet_locator must contain packet_batch and packet_index only")
        _required_string(locator["packet_batch"], "packet_locator.packet_batch")
        if not isinstance(locator["packet_index"], int) or isinstance(locator["packet_index"], bool) or locator["packet_index"] < 0:
            raise FeedbackRoutingError("packet_locator.packet_index must be a non-negative integer")

        approval = decision["approval"]
        if not isinstance(approval, dict) or set(approval) != {"status", "reviewer_id", "approved_at"}:
            raise FeedbackRoutingError("approval has an invalid shape")
        if approval["status"] != "APPROVED":
            raise FeedbackRoutingError("only explicitly approved human feedback may be routed")
        _required_string(approval["reviewer_id"], "approval.reviewer_id")
        _required_string(approval["approved_at"], "approval.approved_at")

        feedback = decision["feedback"]
        if not isinstance(feedback, dict) or set(feedback) != {"action", "targets"}:
            raise FeedbackRoutingError("feedback has an invalid shape")
        if feedback["action"] != "REQUEST_REPAIR":
            raise FeedbackRoutingError("feedback router only accepts REQUEST_REPAIR")
        if not isinstance(feedback["targets"], list) or not feedback["targets"]:
            raise FeedbackRoutingError("feedback requires at least one targeted assistant turn")
        for target in feedback["targets"]:
            if not isinstance(target, dict) or set(target) != {"turn_id", "instruction"}:
                raise FeedbackRoutingError("each feedback target requires turn_id and instruction only")
            _required_string(target["turn_id"], "feedback.targets.turn_id")
            instruction = _required_string(target["instruction"], "feedback.targets.instruction")
            if len(instruction) > 4000:
                raise FeedbackRoutingError("feedback instruction exceeds 4000 characters")
        return decision

    def _load_packet(self, locator: dict[str, Any]) -> dict[str, Any]:
        path = Path(locator["packet_batch"])
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        if self.workspace != path and self.workspace not in path.parents:
            raise FeedbackRoutingError("packet locator escapes the harness workspace")
        if not path.is_file():
            raise FeedbackRoutingError("packet batch does not exist")
        if path.stat().st_size > 100_000_000:
            raise FeedbackRoutingError("packet batch exceeds 100 MB")
        try:
            wrapper = json.loads(path.read_text(encoding="utf-8"))
            packets = wrapper["result"]["packets"]
            packet = packets[locator["packet_index"]]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise FeedbackRoutingError("packet locator does not resolve to a refinement packet") from exc
        if not isinstance(packet, dict):
            raise FeedbackRoutingError("packet locator resolved to a non-object")
        return packet

    def _validate_identity(
        self, decision: dict[str, Any], packet: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if packet.get("packet_id") != decision["packet_id"]:
            raise FeedbackRoutingError("packet identity mismatch")
        if packet.get("source", {}).get("source_content_sha256") != decision["source_content_sha256"]:
            raise FeedbackRoutingError("source content identity mismatch")
        declared_hashes = packet.get("user_turn_sha256")
        if not isinstance(declared_hashes, list) or not declared_hashes:
            raise FeedbackRoutingError("packet has no immutable user-turn checksum binding")
        user_turns: list[dict[str, Any]] = []
        observed_hashes: list[str] = []
        for turn in packet.get("turns", []):
            if turn.get("role") != "user":
                continue
            text = turn.get("text")
            digest = turn.get("text_sha256")
            if not isinstance(text, str) or not isinstance(digest, str):
                raise FeedbackRoutingError("user turn is malformed")
            observed = sha256(text.encode("utf-8")).hexdigest()
            if observed != digest:
                raise FeedbackRoutingError("immutable user-turn checksum mismatch")
            observed_hashes.append(observed)
            user_turns.append(
                {
                    "turn_id": turn.get("turn_id"),
                    "source_index": turn.get("source_index"),
                    "role": "user",
                    "text": text,
                    "text_sha256": digest,
                }
            )
        if observed_hashes != declared_hashes:
            raise FeedbackRoutingError("packet user-turn checksum sequence mismatch")
        return user_turns

    @staticmethod
    def _validate_targets(targets: list[dict[str, Any]], packet: dict[str, Any]) -> None:
        assistant_ids = {
            turn.get("turn_id") for turn in packet.get("turns", [])
            if turn.get("role") == "assistant"
        }
        target_ids = [target["turn_id"] for target in targets]
        if len(target_ids) != len(set(target_ids)):
            raise FeedbackRoutingError("feedback targets must be unique")
        unknown = set(target_ids) - assistant_ids
        if unknown:
            raise FeedbackRoutingError(
                f"feedback may target assistant turns only: {sorted(unknown)}"
            )

    def _all_feedback_items(self) -> list[dict[str, Any]]:
        return [
            row for row in self.queue.items_for_run(self.run_id)
            if row["stage"] == STAGE
        ]


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeedbackRoutingError(f"{field} must be a non-empty string")
    return value


def _reject_shared_mutations(value: Any) -> None:
    """Reject structured or textual attempts to alter governed shared assets."""
    if isinstance(value, dict):
        forbidden = {str(key).casefold() for key in value} & _FORBIDDEN_KEYS
        if forbidden:
            raise FeedbackPolicyViolation(
                f"human feedback cannot request shared mutations: {sorted(forbidden)}"
            )
        for nested in value.values():
            _reject_shared_mutations(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_shared_mutations(nested)
    elif isinstance(value, str) and _MUTATION_TEXT_RE.search(value):
        raise FeedbackPolicyViolation(
            "human feedback cannot mutate shared policy, taxonomy, skills, or release criteria"
        )
