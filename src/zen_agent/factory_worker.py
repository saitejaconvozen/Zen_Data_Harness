from __future__ import annotations

from typing import Any

from .artifacts import ArtifactStore
from .config import HarnessConfig
from .factory_qualification import FactoryQualificationStore
from .factory_queue import ClaimedWork, LocalFactoryQueue
from .policy import PolicyEngine
from .tools import ToolContext, ToolRegistry


STAGE_TOOLS = {
    "trace_fetch": "golden.sample_conversations",
    "prepare_packets": "golden.prepare_refinement_packets",
    "agent_audit": "factory.audit_conversation",
    "refine": "golden.refine_one",
    "verify": "golden.verify_one",
    "human_feedback_repair": "golden.human_feedback_repair",
    "repair": "golden.graph_repair",
    "trajectory_gate": "golden.graph_trajectory_gate",
    "verify_repair": "golden.graph_verify",
    "terminal": "golden.graph_terminal",
}


class FactoryWorker:
    def __init__(
        self,
        config: HarnessConfig,
        tools: ToolRegistry,
        queue: LocalFactoryQueue,
        artifacts: ArtifactStore,
        worker_id: str,
        qualification: FactoryQualificationStore | None = None,
    ):
        self.config = config
        self.tools = tools
        self.queue = queue
        self.artifacts = artifacts
        self.worker_id = worker_id
        self.qualification = qualification
        self.policy = PolicyEngine(config)

    def run_one(self, run_id: str, stages: tuple[str, ...]) -> dict[str, Any] | None:
        unknown = set(stages) - set(STAGE_TOOLS)
        if unknown:
            raise ValueError(f"unknown factory worker stages: {sorted(unknown)}")
        # Model subprocesses are capped at 15 minutes; keep the queue lease
        # longer so timeout handling can commit a retry without duplicate claims.
        work = self.queue.claim(
            run_id, self.worker_id, stages, lease_seconds=1200
        )
        if work is None:
            return None
        try:
            expected_tool = STAGE_TOOLS[work.stage]
            tool_name = work.payload.get("tool")
            if tool_name != expected_tool:
                raise ValueError(f"stage {work.stage} requires tool {expected_tool}")
            tool = self.tools.get(tool_name)
            policy = self.policy.evaluate(tool.name, tool.risk)
            if policy.effect != "allow":
                raise PermissionError(f"factory tool denied: {policy.reason}")
            inputs = work.payload.get("inputs")
            if not isinstance(inputs, dict):
                raise ValueError("factory work payload requires inputs object")
            output = self.tools.invoke(
                tool.name, ToolContext(run_id, work.id, self.config.root), inputs
            )
            record = self.artifacts.put_json(
                {"tool": tool.name, "version": tool.version, "result": output}
            )
            artifact_path = self.artifacts.root / record.relative_path
            workspace_path = str(artifact_path.relative_to(self.config.root))
            self._transition(run_id, work, output, workspace_path)
            self.queue.complete(work.id, work.lease_token, f"sha256:{record.sha256}")
            return {
                "work_id": work.id,
                "job_key": work.job_key,
                "stage": work.stage,
                "status": "SUCCEEDED",
                "output_sha256": record.sha256,
            }
        except Exception as exc:
            status = self.queue.fail(work.id, work.lease_token, f"{type(exc).__name__}: {exc}")
            return {
                "work_id": work.id,
                "job_key": work.job_key,
                "stage": work.stage,
                "status": status,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def run_until_idle(
        self, run_id: str, stages: tuple[str, ...], *, max_items: int
    ) -> list[dict[str, Any]]:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        results = []
        while len(results) < max_items:
            result = self.run_one(run_id, stages)
            if result is None:
                break
            results.append(result)
        return results

    def _transition(
        self,
        run_id: str,
        work: ClaimedWork,
        output: dict[str, Any],
        artifact_path: str,
    ) -> None:
        if work.stage == "trace_fetch":
            if output.get("selected_count", 0) < 1:
                return
            self.queue.enqueue(
                run_id,
                work.job_key + ":packets",
                "prepare_packets",
                {
                    "tool": "golden.prepare_refinement_packets",
                    "inputs": {"sample_artifact": artifact_path},
                    "parent_job_key": work.job_key,
                },
                max_attempts=2,
                priority=90,
            )
            return
        if work.stage == "prepare_packets":
            packets = output.get("packets")
            if not isinstance(packets, list):
                raise ValueError("packet preparation returned no packets")
            for index, packet in enumerate(packets):
                source_sha = packet.get("source", {}).get("source_content_sha256")
                if not isinstance(source_sha, str) or len(source_sha) != 64:
                    raise ValueError("prepared packet lacks source digest")
                if self.qualification is not None:
                    self.qualification.register_packet(run_id, packet, artifact_path, index)
                self.queue.enqueue(
                    run_id,
                    f"conversation-{source_sha}",
                    "agent_audit",
                    {
                        "tool": "factory.audit_conversation",
                        "inputs": {"packet_batch": artifact_path, "packet_index": index},
                        "source_content_sha256": source_sha,
                        "packet_id": packet["packet_id"],
                    },
                    max_attempts=2,
                    priority=80,
                )
            return
        if work.stage == "agent_audit":
            summary = output.get("summary", {})
            if self.qualification is None:
                if summary.get("conversation_usable", summary.get("verdict") == "PASS"):
                    self._enqueue_refinement(run_id, work, output)
                return
            key = self.qualification.record_audit(
                run_id,
                work.payload["source_content_sha256"],
                verdict=summary["verdict"],
                critical_failures=int(summary.get("critical_failures", 0)),
                decision_sha256=output["decision_sha256"],
            )
            decision = self.qualification.decide(run_id, key)
            if not summary.get("conversation_usable", False):
                self._enqueue_terminal(
                    run_id, work, "QUARANTINED", 0,
                    "source conversation is not usable for corrective refinement",
                )
                return
            self._enqueue_refinement(
                run_id, work, output, configuration_key=key,
                qualification_status=decision.status,
            )
            return
        if work.stage == "refine":
            self.queue.enqueue(
                run_id, work.job_key, "verify",
                {
                    **self._common_payload(work),
                    "tool": "golden.verify_one",
                    "inputs": dict(work.payload["inputs"]),
                    "refiner_summary": output.get("summary", {}),
                },
                max_attempts=2, priority=60,
            )
            return
        if work.stage == "verify":
            summary = output.get("summary", {})
            refiner = work.payload.get("refiner_summary", {})
            decision = summary.get("decision")
            status, reason = self._classify_proposal(decision, refiner)
            if status == "REPAIR":
                self._enqueue_repair(run_id, work, 0)
            else:
                self._enqueue_terminal(run_id, work, status, 0, reason)
            return
        if work.stage in {"repair", "human_feedback_repair"}:
            self.queue.enqueue(
                run_id, work.job_key, "trajectory_gate",
                {
                    **self._common_payload(work),
                    "tool": "golden.graph_trajectory_gate",
                    "inputs": dict(work.payload["inputs"]),
                    "proposal_summary": output.get("summary", {}),
                },
                max_attempts=1, priority=45,
            )
            return
        if work.stage == "trajectory_gate":
            round_number = int(work.payload["inputs"]["round_number"])
            if output.get("route") == "SAFE":
                inputs = dict(work.payload["inputs"])
                inputs["max_rounds"] = int(work.payload.get("max_repair_rounds", 5))
                payload = {**self._common_payload(work)}
                # Turns the gate excluded join the proposal's own exclusions so
                # the terminal stage drops exactly those and keeps the rest.
                excluded = output.get("excluded_turn_ids") or []
                if excluded:
                    proposal = dict(payload.get("proposal_summary") or {})
                    merged = set(proposal.get("divergent_turns") or []) | set(excluded)
                    proposal["divergent_turns"] = sorted(merged)
                    payload["proposal_summary"] = proposal
                self.queue.enqueue(
                    run_id, work.job_key, "verify_repair",
                    {
                        **payload,
                        "tool": "golden.graph_verify",
                        "inputs": inputs,
                    },
                    max_attempts=2, priority=40,
                )
            else:
                self._enqueue_terminal(
                    run_id, work, "QUARANTINED", round_number,
                    "every repaired turn would invalidate an immutable user turn",
                )
            return
        if work.stage == "verify_repair":
            route = output.get("route")
            round_number = int(work.payload["inputs"]["round_number"])
            proposal = work.payload.get("proposal_summary", {})
            if route == "EXHAUSTED":
                # Repair rounds are spent. Rather than discard a conversation
                # whose remaining turns are sound, drop only the turns the
                # verifier still objects to and keep the rest.
                self._enqueue_exhausted(run_id, work, output, round_number)
                return
            status, reason = self._classify_proposal(route, proposal, repaired=True)
            if status == "REPAIR":
                self._enqueue_repair(run_id, work, round_number + 1)
            else:
                self._enqueue_terminal(run_id, work, status, round_number, reason)
            return

    @staticmethod
    def _classify_proposal(
        decision: str | None,
        proposal: dict[str, Any],
        *,
        repaired: bool = False,
    ) -> tuple[str, str]:
        """Decide a proposal's terminal status.

        Every gate here is scoped to the narrowest level that actually holds. A
        turn that diverges from the recorded user reply, or that lacks the
        evidence to assess, is excluded on its own. The conversation is discarded
        only when a genuine whole-conversation blocker applies or nothing usable
        survives.
        """

        stage = "repair" if repaired else "initial"
        # Genuine whole-conversation blockers.
        if proposal.get("prompt_usable") is not True:
            return "QUARANTINED", "system prompt is too incoherent to judge adherence"
        if proposal.get("conversation_assessable") is False:
            return "QUARANTINED", "no turn in this conversation is assessable"
        if decision == "FAIL":
            return "REPAIR", f"{stage} verifier rejected the proposal"
        if decision != "PASS":
            return "QUARANTINED", f"{stage} verification ended with {decision}"

        excluded = set(proposal.get("divergent_turns") or [])
        excluded.update(proposal.get("unassessable_turns") or [])
        total = int(proposal.get("assistant_turns", 0) or 0)
        if not excluded:
            return "VERIFIED_CANDIDATE", f"{stage} independent verifier passed"
        if total and len(excluded) >= total:
            return "QUARANTINED", "no assistant turn survived turn-level exclusion"
        return (
            "PARTIAL_CANDIDATE",
            f"{stage} verifier passed; {len(excluded)} of {total} turns excluded",
        )

    def _enqueue_refinement(
        self,
        run_id: str,
        work: ClaimedWork,
        output: dict[str, Any],
        *,
        configuration_key: str | None = None,
        qualification_status: str | None = None,
    ) -> None:
        self.queue.enqueue(
            run_id,
            work.job_key,
            "refine",
            {
                "tool": "golden.refine_one",
                "inputs": dict(work.payload["inputs"]),
                "source_content_sha256": work.payload["source_content_sha256"],
                "packet_id": work.payload["packet_id"],
                "agent_audit_sha256": output["decision_sha256"],
                "configuration_key": configuration_key,
                "qualification_status": qualification_status,
                "conversation_job_key": work.job_key,
                "max_repair_rounds": 5,
            },
            max_attempts=2,
            priority=70,
        )

    @staticmethod
    def _common_payload(work: ClaimedWork) -> dict[str, Any]:
        return {
            key: work.payload[key]
            for key in (
                "source_content_sha256", "packet_id", "configuration_key",
                "qualification_status", "agent_audit_sha256", "max_repair_rounds",
                "proposal_summary", "refiner_summary",
                "conversation_job_key", "review_decision_id",
                "review_decision_sha256", "feedback_round_number",
                "max_feedback_rounds", "graph_round_number",
            )
            if key in work.payload
        }

    @staticmethod
    def _conversation_job_key(work: ClaimedWork) -> str:
        return work.payload.get(
            "conversation_job_key",
            work.job_key.split(":repair-round-", 1)[0],
        )

    def _enqueue_repair(self, run_id: str, work: ClaimedWork, round_number: int) -> None:
        max_rounds = int(work.payload.get("max_repair_rounds", 5))
        conversation_job_key = self._conversation_job_key(work)
        if round_number >= max_rounds:
            self._enqueue_terminal(
                run_id, work, "QUARANTINED", max(0, round_number - 1),
                "maximum repair rounds exhausted",
            )
            return
        base_inputs = work.payload["inputs"]
        inputs = {
            "packet_batch": base_inputs["packet_batch"],
            "packet_index": base_inputs["packet_index"],
            "packet_id": work.payload["packet_id"],
            "source_decision_run_id": run_id,
            "round_number": round_number,
        }
        self.queue.enqueue(
            run_id,
            f"{conversation_job_key}:repair-round-{round_number:02d}",
            "repair",
            {
                **self._common_payload(work),
                "conversation_job_key": conversation_job_key,
                "tool": "golden.graph_repair",
                "inputs": inputs,
            },
            max_attempts=2, priority=50,
        )

    def _enqueue_exhausted(
        self, run_id: str, work: ClaimedWork, output: dict[str, Any], round_number: int
    ) -> None:
        """Salvage a conversation whose repair budget ran out.

        A single unresolved finding used to discard every turn in the packet.
        Only turns the verifier still objects to are excluded now; the packet is
        quarantined solely when nothing usable survives.
        """

        proposal = dict(work.payload.get("proposal_summary") or {})
        blocking = list(output.get("blocking_turn_ids") or output.get("finding_turn_ids") or [])
        excluded = set(proposal.get("divergent_turns") or []) | set(blocking)
        total = int(proposal.get("assistant_turns", 0) or 0)
        if not blocking or (total and len(excluded) >= total):
            self._enqueue_terminal(
                run_id, work, "QUARANTINED", round_number,
                "repair rounds exhausted with no turn left acceptable",
            )
            return
        proposal["divergent_turns"] = sorted(excluded)
        work.payload["proposal_summary"] = proposal
        self._enqueue_terminal(
            run_id, work, "PARTIAL_CANDIDATE", round_number,
            f"repair rounds exhausted; {len(excluded)} of {total} turns excluded, "
            "the remainder passed verification",
        )

    def _enqueue_terminal(
        self, run_id: str, work: ClaimedWork, status: str,
        round_number: int, reason: str,
    ) -> None:
        base_inputs = work.payload["inputs"]
        proposal = work.payload.get("proposal_summary") or work.payload.get(
            "refiner_summary", {}
        )
        self.queue.enqueue(
            run_id, self._conversation_job_key(work), "terminal",
            {
                **self._common_payload(work),
                "tool": "golden.graph_terminal",
                "inputs": {
                    "packet_batch": base_inputs["packet_batch"],
                    "packet_index": base_inputs["packet_index"],
                    "packet_id": work.payload["packet_id"],
                    "source_decision_run_id": run_id,
                    "round_number": round_number,
                    "terminal_status": status,
                },
                "terminal_reason": reason,
                # Downstream assembly excludes these turns from the golden set.
                "excluded_turn_ids": list(proposal.get("divergent_turns") or []),
            },
            max_attempts=1, priority=30,
        )


def factory_artifact_store(config: HarnessConfig) -> ArtifactStore:
    return ArtifactStore(config.state_directory / "factory-artifacts")
