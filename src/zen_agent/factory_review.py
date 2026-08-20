from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from .review_integration import sync_review_items


def _rows(path: Path, sql: str, run_id: str) -> list[dict[str, Any]]:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(sql, (run_id,)).fetchall()]
    finally:
        db.close()


def _read(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _taxonomy(root: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    registry = json.loads(
        (
            root
            / "plugins/golden-conversations/resources/taxonomy/"
            / "zen-eval-taxonomy-2026-q2-v1.json"
        ).read_text(encoding="utf-8")
    )
    names = {}
    for axis in registry["axes"]:
        for subaxis in axis["subaxes"]:
            for variant in subaxis["variants"]:
                names[(axis["id"], subaxis["id"], variant["id"])] = {
                    "axis_name": axis["name"],
                    "subaxis_name": subaxis["name"],
                    "variant_name": variant["name"],
                }
    return names


# Roles that are part of the conversation. Everything else — scaffolding the
# sanitiser demoted, future roles nobody has thought of — is excluded rather
# than falling through to the assistant branch. For SFT every assistant turn is
# a training target, so a fall-through does not produce a cosmetic glitch: it
# produces training examples that teach the model to answer with nothing.
DIALOGUE_ROLES = frozenset({"user", "assistant", "tool"})


def is_dialogue_turn(turn: dict[str, Any]) -> bool:
    """Whether this turn belongs in the conversation at all."""
    return turn.get("role") in DIALOGUE_ROLES


def _packet(sample: dict[str, Any], cache: dict[str, list[dict]]) -> dict:
    path = sample["packet_batch"]
    if path not in cache:
        cache[path] = json.loads(Path(path).read_text(encoding="utf-8"))["result"]["packets"]
    packet = cache[path][sample["packet_index"]]
    if packet["source"]["source_content_sha256"] != sample["source_content_sha256"]:
        raise ValueError("packet identity mismatch")
    return packet


def _iterations(zen: Path, run_id: str, packet_id: str) -> tuple[list[dict], dict | None]:
    history = []
    initial = zen / "jobs" / run_id / packet_id
    proposal = _read(initial / "refiner.json")
    verifier = _read(initial / "verifier.json")
    if proposal:
        history.append(
            {
                "round": 0,
                "kind": "INITIAL",
                "proposal_role": proposal["worker"]["role"],
                "replaced": sum(
                    row["action"] == "REPLACE"
                    for row in proposal["decision"]["assistant_turns"]
                ),
                "verifier_decision": (
                    verifier["decision"]["decision"] if verifier else "PENDING"
                ),
                "verifier_findings": (
                    verifier["decision"]["findings"] if verifier else []
                ),
                "trajectory": None,
            }
        )
    latest = proposal
    graph_root = zen / "graph-jobs" / run_id / packet_id
    for directory in sorted(graph_root.glob("round-*")) if graph_root.is_dir() else ():
        repair = _read(directory / "repair.json")
        graph_verifier = _read(directory / "verifier.json")
        trajectory = _read(directory / "trajectory.json")
        if not repair:
            continue
        latest = repair
        history.append(
            {
                "round": int(directory.name.rsplit("-", 1)[1]) + 1,
                "kind": "REPAIR",
                "proposal_role": repair["worker"]["role"],
                "replaced": sum(
                    row["action"] == "REPLACE"
                    for row in repair["decision"]["assistant_turns"]
                ),
                "verifier_decision": (
                    graph_verifier["decision"]["decision"]
                    if graph_verifier
                    else "PENDING"
                ),
                "verifier_findings": (
                    graph_verifier["decision"]["findings"]
                    if graph_verifier
                    else []
                ),
                "trajectory": trajectory,
            }
        )
    return history, latest


def _verdict_rollup(values: list[str | None]) -> str:
    normalized = {value for value in values if value in {"PASS", "FAIL"}}
    if "FAIL" in normalized:
        return "FAIL"
    if "PASS" in normalized:
        return "PASS"
    return "NOT_ASSESSED"


def _metric_hierarchy(coverage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    axes: dict[tuple[str, str], dict[str, Any]] = {}
    for row in coverage:
        axis_key = (row["axis_id"], row["axis_name"])
        axis = axes.setdefault(axis_key, {"rows": [], "subaxes": {}})
        axis["rows"].append(row)
        subaxis_key = (row["subaxis_id"], row["subaxis_name"])
        subaxis = axis["subaxes"].setdefault(
            subaxis_key, {"rows": [], "variants": {}}
        )
        subaxis["rows"].append(row)
        variant_key = (row["variant_id"], row["variant_name"])
        subaxis["variants"].setdefault(variant_key, []).append(row)

    def counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
        source = Counter(row.get("source_verdict", "NOT_ASSESSED") for row in rows)
        golden = Counter(row.get("golden_verdict", "NOT_ASSESSED") for row in rows)
        return {
            "citations": len(rows),
            "conversations": len({row["source_id"] for row in rows}),
            "turns": len({(row["source_id"], row["turn_id"]) for row in rows}),
            "source": dict(source),
            "golden": dict(golden),
        }

    output = []
    for (axis_id, axis_name), axis in sorted(axes.items()):
        subaxes = []
        for (subaxis_id, subaxis_name), subaxis in sorted(axis["subaxes"].items()):
            variants = [
                {
                    "variant_id": variant_id,
                    "variant_name": variant_name,
                    "counts": counts(rows),
                }
                for (variant_id, variant_name), rows
                in sorted(subaxis["variants"].items())
            ]
            subaxes.append(
                {
                    "subaxis_id": subaxis_id,
                    "subaxis_name": subaxis_name,
                    "counts": counts(subaxis["rows"]),
                    "variants": variants,
                }
            )
        output.append(
            {
                "axis_id": axis_id,
                "axis_name": axis_name,
                "counts": counts(axis["rows"]),
                "subaxes": subaxes,
            }
        )
    return output


def build_review(root: Path, run_id: str) -> dict[str, Any]:
    zen = root / ".zen"
    samples = _rows(
        zen / "factory-qualification.db",
        """SELECT sample.*, configuration.status AS configuration_status
        FROM factory_configuration_sample AS sample
        JOIN factory_configuration AS configuration
          ON configuration.run_id=sample.run_id
         AND configuration.configuration_key=sample.configuration_key
        WHERE sample.run_id=? ORDER BY sample.source_content_sha256""",
        run_id,
    )
    queue_rows = _rows(
        zen / "factory-queue.db",
        "SELECT stage,status,payload_json,updated_at FROM factory_work WHERE run_id=?",
        run_id,
    )
    terminals = {}
    for row in queue_rows:
        if row["stage"] == "terminal" and row["status"] == "SUCCEEDED":
            payload = json.loads(row["payload_json"])
            candidate = {
                "status": payload["inputs"]["terminal_status"],
                "reason": payload.get("terminal_reason"),
                "round": payload["inputs"]["round_number"],
                "review_decision_id": payload.get("review_decision_id"),
                "_updated_at": row["updated_at"],
            }
            current = terminals.get(payload["packet_id"])
            if current is None or candidate["_updated_at"] >= current["_updated_at"]:
                terminals[payload["packet_id"]] = candidate
    names = _taxonomy(root)
    cache: dict[str, list[dict]] = {}
    conversations = []
    coverage = []
    status_counts: Counter[str] = Counter()
    total_replaced = total_annotations = 0
    for number, sample in enumerate(samples, 1):
        packet = _packet(sample, cache)
        audit_wrapper = _read(
            zen / "factory-jobs" / run_id / packet["packet_id"] / "agent-audit.json"
        )
        audit = audit_wrapper["decision"] if audit_wrapper else {}
        history, latest = _iterations(zen, run_id, packet["packet_id"])
        decision = latest["decision"] if latest else None
        assistant_rows = (
            {row["turn_id"]: row for row in decision["assistant_turns"]}
            if decision
            else {}
        )
        output_turns = []
        for turn in packet["turns"]:
            # Scaffolding the sanitiser demoted out of the dialogue. Anything not
            # explicitly handled below falls through to the assistant branch, so
            # without this these render as assistant turns with empty text — and
            # for SFT every assistant turn is a training target. 1,368 of them
            # would have taught the model to answer with nothing.
            if not is_dialogue_turn(turn):
                continue
            if turn["role"] == "tool":
                output_turns.append({
                    "turn_id": turn["turn_id"], "role": "tool",
                    "text": turn["text"], "text_sha256": turn["text_sha256"],
                    "tool_call_id": turn.get("tool_call_id"),
                    "source_preserved": True,
                })
                continue
            if turn["role"] == "user":
                output_turns.append(
                    {
                        "turn_id": turn["turn_id"],
                        "role": "user",
                        "text": turn["text"],
                        "source_preserved": True,
                    }
                )
                continue
            row = assistant_rows.get(turn["turn_id"])
            annotations = []
            if row:
                for annotation in row["annotations"]:
                    key = (
                        annotation["axis_id"],
                        annotation["subaxis_id"],
                        annotation["variant_id"],
                    )
                    # A decision whose validation failed still leaves its raw
                    # output on disk, so a citation here may name a path that is
                    # not in the governed registry. Surface it as unresolved
                    # rather than crashing every review and the status UI.
                    resolved = names.get(key)
                    citation = {
                        **annotation,
                        **(resolved or {
                            "axis_name": annotation["axis_id"],
                            "subaxis_name": annotation["subaxis_id"],
                            "variant_name": annotation["variant_id"],
                        }),
                        "taxonomy_path_resolved": resolved is not None,
                    }
                    annotations.append(citation)
                    coverage.append(
                        {
                            "conversation_number": number,
                            "source_id": sample["source_content_sha256"][:12],
                            "turn_id": turn["turn_id"],
                            "action": row["action"],
                            **citation,
                        }
                    )
                total_replaced += row["action"] == "REPLACE"
                total_annotations += len(annotations)
            output_turns.append(
                {
                    "turn_id": turn["turn_id"],
                    "role": "assistant",
                    "source_text": turn["text"],
                    # golden_text_final carries the harness-applied language tag.
                    "golden_text": (
                        row.get("golden_text_final", row["golden_text"]) if row else None
                    ),
                    "action": row["action"] if row else "NOT_REFINED",
                    "semantic_delta": row["semantic_delta"] if row else None,
                    "source_quality": row.get("source_quality") if row else None,
                    "downstream_coherence": (
                        row.get("downstream_coherence") if row else None
                    ),
                    "divergence_reason": row.get("divergence_reason") if row else None,
                    "evidence_status": row.get("evidence_status") if row else None,
                    # Divergent or unassessable turns are dropped from the golden set.
                    "excluded_from_golden": bool(
                        row
                        and (
                            row.get("downstream_coherence") == "DIVERGENT"
                            or row.get("evidence_status") == "INSUFFICIENT"
                        )
                    ),
                    # The dataset trains tool use, so the calls ship with the turn.
                    "source_tool_calls": turn.get("tool_calls"),
                    "golden_tool_calls": (
                        row.get("golden_tool_calls") if row else None
                    ) or turn.get("tool_calls"),
                    "correction_reason": row["correction_reason"] if row else None,
                    "metric_citations": annotations,
                    "source_metric_result": _verdict_rollup(
                        [item.get("source_verdict") for item in annotations]
                    ),
                    "golden_metric_result": _verdict_rollup(
                        [item.get("golden_verdict") for item in annotations]
                    ),
                }
            )
        terminal = terminals.get(
            packet["packet_id"],
            {
                # An absent audit is not a rejection. Only an audit that ran and
                # said the conversation is unusable rejects it; a missing or
                # unreadable decision means it has yet to be judged.
                "status": (
                    "IN_PROGRESS" if latest
                    else "AWAITING_AUDIT" if not audit
                    else "REJECTED_SOURCE" if audit.get("conversation_usable") is False
                    else "QUEUED"
                ),
                "reason": None,
                "round": None,
            },
        )
        status_counts[terminal["status"]] += 1
        review_outcome = {
            "VERIFIED_CANDIDATE": "PASS",
            "PARTIAL_CANDIDATE": "PASS",
            "QUARANTINED": "FAIL",
            "REJECTED_SOURCE": "FAIL",
        }.get(terminal["status"], "PENDING")
        classification = (
            decision["classification"]
            if decision
            else {
                "domain": "Unclassified",
                "primary_language": "Unknown",
                "other_languages": [],
                "code_switching": False,
            }
        )
        conversations.append(
            {
                "number": number,
                "packet_id": packet["packet_id"],
                "source_id": sample["source_content_sha256"][:12],
                "configuration_id": sample["configuration_key"][:12],
                "agent_id": packet["source"].get("agent_id"),
                "agent_version": packet["source"].get("agent_version"),
                "call_id": packet["source"].get("call_id"),
                "source_id_full": sample["source_content_sha256"],
                "configuration_status": sample["configuration_status"],
                "terminal": terminal,
                "review_outcome": review_outcome,
                "audit": {
                    "verdict": audit.get("verdict"),
                    "prompt_coherent": audit.get("prompt_coherent"),
                    "workflow_obeyed": audit.get("workflow_obeyed"),
                    "conversation_usable": audit.get("conversation_usable"),
                    "findings": [
                        {
                            "turn_id": finding.get("turn_id"),
                            "severity": finding.get("severity"),
                            "category": finding.get("category"),
                        }
                        for finding in audit.get("findings", [])
                    ],
                },
                "classification": classification,
                "prompt_usable": decision.get("prompt_usable") if decision else None,
                "prompt_issues": decision.get("prompt_issues", []) if decision else [],
                "replay_required": decision.get("replay_required") if decision else None,
                "quarantine_reasons": (
                    decision.get("quarantine_reasons", []) if decision else []
                ),
                "iterations": history,
                "turns": output_turns,
            }
        )
    review_sync = sync_review_items(root, run_id, conversations)
    for conversation in conversations:
        conversation["terminal"].pop("_updated_at", None)
        conversation["human_review"] = review_sync["items"][conversation["packet_id"]]
    stage_counts = Counter((row["stage"], row["status"]) for row in queue_rows)
    return {
        "schema_version": "zen.factory-golden-review/1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "model_policy": "gpt-5.6-sol-only",
        "disclaimer": (
            "Source conversations and model-generated proposals are restricted. "
            "Only VERIFIED_CANDIDATE items passed independent verification; "
            "all outputs still require human release review."
        ),
        "counts": {
            "conversations": len(conversations),
            "replaced_assistant_turns": total_replaced,
            "metric_citations": total_annotations,
            "terminal": dict(status_counts),
            "human_review": review_sync["states"],
            "queue": [
                {"stage": stage, "status": status, "count": count}
                for (stage, status), count in sorted(stage_counts.items())
            ],
        },
        "conversations": conversations,
        "metric_coverage": coverage,
        "metric_hierarchy": _metric_hierarchy(coverage),
    }


def _write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    temporary.replace(path)


def publish(root: Path, run_id: str, site: Path) -> dict[str, Any]:
    review = build_review(root, run_id)
    site.mkdir(parents=True, exist_ok=True, mode=0o700)
    assets = {
        "index.html": INDEX.encode(),
        "styles.css": STYLES.encode(),
        "app.js": APP.encode(),
        "review.json": json.dumps(
            review, ensure_ascii=False, separators=(",", ":")
        ).encode(),
    }
    for name, data in assets.items():
        _write(site / name, data)
    manifest = {
        "schema_version": "zen.review-site/2",
        "site_run_id": run_id,
        "security": {
            "contains_restricted_conversations": True,
            "system_prompts_included": False,
            "authentication": "bearer-or-cookie-token",
        },
        "counts": review["counts"],
        "files": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in assets.items()
        },
    }
    _write(
        site / "site-manifest.json",
        json.dumps(manifest, indent=2).encode(),
    )
    return {
        "site": str(site),
        "run_id": run_id,
        **review["counts"],
    }


INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>Zen Refinement Studio</title><link rel="stylesheet" href="/styles.css"></head><body><header><div><p class="eyebrow">ZEN DATA FACTORY</p><h1>Refinement studio</h1><p id="runline">Loading governed run…</p></div><nav><button class="tab active" data-view="conversations">Conversations</button><button class="tab" data-view="metrics">Pass / fail metrics</button></nav></header><section id="notice"></section><section id="stats"></section><main id="conversationView"><aside><div class="controls"><input id="search" type="search" placeholder="Search domain, language, ID"><select id="outcome"><option value="ALL">All review outcomes</option><option value="PASS">PASS — independently verified</option><option value="FAIL">FAIL — quarantined/rejected</option><option value="PENDING">PENDING — still processing</option></select><select id="status"><option value="ALL">All pipeline statuses</option></select><select id="change"><option value="ALL">All conversations</option><option value="CHANGED">Has assistant changes</option><option value="UNCHANGED">No assistant changes</option></select></div><div id="conversationList"></div></aside><section id="detail"><div class="empty">Select a conversation.</div></section></main><main id="metricView" hidden><section class="metric-controls"><input id="metricSearch" type="search" placeholder="Search axis, sub-axis, variant, conversation or turn"><select id="metricResult"><option value="ALL">All metric verdicts</option><option value="SOURCE_FAIL">Source FAIL</option><option value="SOURCE_PASS">Source PASS</option><option value="GOLDEN_FAIL">Golden FAIL</option><option value="GOLDEN_PASS">Golden PASS</option></select><select id="axis"><option value="ALL">All axes</option></select><select id="subaxis"><option value="ALL">All sub-axes</option></select><select id="variant"><option value="ALL">All variants</option></select></section><section id="metricSummary"></section><section class="table-wrap"><table><thead><tr><th>Conversation</th><th>Turn</th><th>Action</th><th>Axis</th><th>Sub-axis</th><th>Variant</th><th>Source</th><th>Golden</th><th>Severity</th><th>Confidence</th></tr></thead><tbody id="metricRows"></tbody></table></section></main><script src="/app.js" defer></script></body></html>"""

STYLES = """:root{color-scheme:dark;--bg:#071016;--panel:#0d1a22;--panel2:#122630;--line:#28434d;--text:#edf8f7;--muted:#8ea6ad;--mint:#50e2c5;--blue:#70adff;--amber:#ffc26a;--red:#ff7c84;--green:#66dfa5}*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#061015,#091a22);color:var(--text);font:14px/1.5 Inter,system-ui,sans-serif}header{min-height:145px;padding:28px 4vw 20px;display:flex;align-items:end;justify-content:space-between;border-bottom:1px solid var(--line);background:radial-gradient(circle at 78% 0,#174a4c,transparent 38%)}h1{font-size:clamp(32px,5vw,56px);letter-spacing:-.045em;margin:0}.eyebrow{font:800 11px monospace;color:var(--mint);letter-spacing:.18em}p{color:var(--muted)}nav{display:flex;gap:7px}.tab,.read{border:1px solid var(--line);border-radius:9px;padding:9px 13px;background:#0b1720;color:var(--text);cursor:pointer}.tab.active,.read:hover{border-color:var(--mint);color:var(--mint);background:#12302f}#notice{margin:20px 4vw 0;padding:14px 17px;border:1px solid #795932;border-radius:12px;background:#261e13;color:#ffdda3}#stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:15px 4vw}.stat{padding:14px 16px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}.stat strong{display:block;font-size:27px}.stat span{color:var(--muted)}#conversationView{display:grid;grid-template-columns:330px minmax(0,1fr);min-height:700px;border-top:1px solid var(--line)}aside{border-right:1px solid var(--line);padding:16px;background:#09151c}.controls{display:grid;gap:8px;position:sticky;top:0;background:#09151c;padding-bottom:12px;z-index:2}input,select{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:#08131a;color:var(--text)}#conversationList{display:grid;gap:7px}.conversation-item{width:100%;text-align:left;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--text);cursor:pointer}.conversation-item.selected,.conversation-item:hover{border-color:var(--mint);background:#102a2c}.conversation-item strong,.conversation-item span{display:block}.conversation-item small{color:var(--muted)}#detail{padding:26px clamp(18px,4vw,60px) 70px;max-width:1200px}.detail-head{display:flex;justify-content:space-between;gap:20px;border-bottom:1px solid var(--line);padding-bottom:16px}.badges{display:flex;gap:6px;flex-wrap:wrap}.badge{display:inline-block;padding:3px 7px;border:1px solid var(--line);border-radius:999px;font:700 10px monospace;color:var(--muted)}.pass,.verified,.keep{color:var(--green)}.fail,.quarantined,.replace{color:var(--red)}.pending{color:var(--amber)}.panel{margin:15px 0;padding:15px;border:1px solid var(--line);border-radius:11px;background:var(--panel)}.iterations{display:flex;gap:8px;flex-wrap:wrap}.iteration{padding:9px;border:1px solid var(--line);border-radius:8px;background:#0b161d}.turns{display:grid;gap:13px}.turn{border:1px solid var(--line);border-radius:13px;padding:16px;background:var(--panel)}.turn.user{margin-right:9%;border-left:3px solid #7791a0}.turn.assistant{margin-left:9%;border-left:3px solid var(--mint)}.turn-head{display:flex;align-items:center;gap:7px;margin-bottom:10px}.turn-head code{margin-right:auto;color:var(--muted)}.utterance{white-space:pre-wrap;color:var(--text);font-size:15px}.compare{display:grid;grid-template-columns:1fr 1fr;gap:10px}.pane{padding:12px;border-radius:9px;background:#08151c}.pane.source{border-top:2px solid var(--red)}.pane.golden{border-top:2px solid var(--green)}.pane h4{margin:0 0 8px;color:var(--muted);font-size:10px;text-transform:uppercase}.reason{padding:9px;background:#152732;border-radius:7px}.metrics summary{cursor:pointer;color:var(--mint)}.citation{margin-top:9px;padding:11px;border:1px solid var(--line);border-radius:9px;background:#09151c}.path{display:flex;gap:5px;flex-wrap:wrap}.axis{color:var(--mint)}.subaxis{color:var(--blue)}.variant{color:var(--amber)}#metricView{display:block;margin:20px 4vw 70px}#metricView[hidden]{display:none}.metric-controls{display:grid;grid-template-columns:2fr 1fr;gap:10px;margin-bottom:12px}.table-wrap{overflow:auto;max-height:720px;border:1px solid var(--line);border-radius:12px}table{border-collapse:collapse;width:100%;min-width:1150px}th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{position:sticky;top:0;background:#10222c;color:var(--muted);font-size:10px;text-transform:uppercase}.empty{color:var(--muted)}@media(max-width:850px){header{align-items:start;flex-direction:column}#stats{grid-template-columns:repeat(2,1fr)}#conversationView{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid var(--line)}.compare{grid-template-columns:1fr}.turn.user,.turn.assistant{margin:0}.metric-controls{grid-template-columns:1fr}}"""

APP = r'''"use strict";
const state={data:null,selected:null};
function node(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined&&text!==null)n.textContent=String(text);return n}
function badge(text,cls){return node("span","badge "+(cls||""),text)}
function changed(c){return c.turns.some(t=>t.role==="assistant"&&t.action==="REPLACE")}
function metricCard(m){const card=node("article","citation"),path=node("div","path");path.append(badge(m.axis_name,"axis"),node("span","","›"),badge(m.subaxis_name,"subaxis"),node("span","","›"),badge(m.variant_name,"variant"));card.append(path);const verdicts=node("div","badges");verdicts.append(badge("source "+(m.source_verdict||"N/A"),m.source_verdict==="FAIL"?"fail":"pass"),badge("golden "+(m.golden_verdict||"N/A"),m.golden_verdict==="PASS"?"pass":"pending"),badge(Math.round((m.confidence||0)*100)+"%",""));card.append(verdicts);card.append(node("p","",m.expected_behavior));return card}
function renderConversation(c){state.selected=c.source_id;const host=document.querySelector("#detail");host.replaceChildren();const head=node("div","detail-head"),copy=node("div");copy.append(node("p","eyebrow","CONVERSATION "+c.number),node("h2","",c.classification.domain+" · "+c.classification.primary_language),node("p","",c.source_id+" · config "+c.configuration_id));head.append(copy,badge(c.terminal.status,c.terminal.status.toLowerCase()));host.append(head);const audit=node("section","panel");audit.append(node("h3","","Source qualification"),badge(c.audit.verdict,c.audit.verdict==="PASS"?"pass":"fail"),badge("config "+c.configuration_status,c.configuration_status==="QUALIFIED"?"pass":"pending"));audit.append(node("p","",c.terminal.reason||"Terminal decision pending."));host.append(audit);const history=node("section","panel");history.append(node("h3","","Iteration history"));const strip=node("div","iterations");if(!c.iterations.length)strip.append(node("span","empty","No refinement was attempted."));c.iterations.forEach(i=>{const d=node("div","iteration");d.append(node("strong","",i.kind+" "+i.round),node("div","",i.replaced+" assistant changes"),badge(i.verifier_decision,i.verifier_decision==="PASS"?"pass":i.verifier_decision==="PENDING"?"pending":"fail"));strip.append(d)});history.append(strip);host.append(history);const turns=node("section","turns");c.turns.forEach(t=>{const card=node("article","turn "+t.role),meta=node("div","turn-head");meta.append(badge(t.role,t.role),node("code","",t.turn_id));if(t.role==="user"){meta.append(badge("source preserved","keep"));card.append(meta,node("p","utterance",t.text));turns.append(card);return}meta.append(badge(t.action,t.action==="REPLACE"?"replace":t.action==="KEEP"?"keep":"pending"));card.append(meta);if(t.action==="REPLACE"){const compare=node("div","compare"),before=node("div","pane source"),after=node("div","pane golden");before.append(node("h4","","Source assistant"),node("p","utterance",t.source_text));after.append(node("h4","","Refined assistant"),node("p","utterance",t.golden_text));compare.append(before,after);card.append(compare)}else{card.append(node("p","utterance",t.golden_text||t.source_text))}if(t.correction_reason)card.append(node("p","reason",t.correction_reason));const metrics=node("details","metrics");metrics.append(node("summary","",t.metric_citations.length+" observed metric path(s)"));t.metric_citations.forEach(m=>metrics.append(metricCard(m)));card.append(metrics);turns.append(card)});host.append(turns);renderList()}
function filtered(){const q=document.querySelector("#search").value.toLowerCase(),status=document.querySelector("#status").value,change=document.querySelector("#change").value;return state.data.conversations.filter(c=>(status==="ALL"||c.terminal.status===status)&&(change==="ALL"||(change==="CHANGED")===changed(c))&&JSON.stringify({id:c.source_id,classification:c.classification}).toLowerCase().includes(q))}
function renderList(){const host=document.querySelector("#conversationList");host.replaceChildren();filtered().forEach(c=>{const b=node("button","conversation-item "+(state.selected===c.source_id?"selected":""));b.type="button";b.append(node("strong","",c.classification.domain+" · "+c.classification.primary_language),node("span","",c.terminal.status+" · "+c.turns.filter(t=>t.action==="REPLACE").length+" changed"),node("small","",c.source_id));b.addEventListener("click",()=>renderConversation(c));host.append(b)})}
function renderMetrics(){const host=document.querySelector("#metricRows"),q=document.querySelector("#metricSearch").value.toLowerCase(),axis=document.querySelector("#axis").value;host.replaceChildren();state.data.metric_coverage.filter(m=>(axis==="ALL"||m.axis_name===axis)&&JSON.stringify(m).toLowerCase().includes(q)).forEach(m=>{const r=node("tr");[m.conversation_number+" · "+m.source_id,m.turn_id,m.action,m.axis_name,m.subaxis_name,m.variant_name,m.source_verdict||"N/A",m.golden_verdict||"N/A",m.severity||"N/A",Math.round((m.confidence||0)*100)+"%"].forEach(x=>r.append(node("td","",x)));host.append(r)})}
function start(data){state.data=data;document.querySelector("#runline").textContent=data.run_id+" · "+data.model_policy;document.querySelector("#notice").textContent=data.disclaimer;const terminal=data.counts.terminal;[[data.counts.conversations,"Conversations"],[data.counts.replaced_assistant_turns,"Changed assistant turns"],[data.counts.metric_citations,"Metric citations"],[terminal.VERIFIED_CANDIDATE||0,"Verified candidates"],[terminal.QUARANTINED||0,"Quarantined"]].forEach(([n,l])=>{const c=node("div","stat");c.append(node("strong","",n),node("span","",l));document.querySelector("#stats").append(c)});const statuses=[...new Set(data.conversations.map(c=>c.terminal.status))].sort();statuses.forEach(s=>document.querySelector("#status").append(node("option","",s)));const axes=[...new Set(data.metric_coverage.map(m=>m.axis_name))].sort();axes.forEach(a=>document.querySelector("#axis").append(node("option","",a)));["search","status","change"].forEach(id=>document.querySelector("#"+id).addEventListener(id==="search"?"input":"change",renderList));document.querySelector("#metricSearch").addEventListener("input",renderMetrics);document.querySelector("#axis").addEventListener("change",renderMetrics);document.querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));b.classList.add("active");const metric=b.dataset.view==="metrics";document.querySelector("#conversationView").hidden=metric;document.querySelector("#metricView").hidden=!metric;if(metric)renderMetrics()}));renderList();renderMetrics();if(data.conversations.length)renderConversation(data.conversations[0])}
function verdictClass(value){return value==="PASS"?"pass":value==="FAIL"?"fail":"pending"}
function resultBadges(counts){const wrap=node("span","badges");wrap.append(badge("source PASS "+(counts.source.PASS||0),"pass"),badge("source FAIL "+(counts.source.FAIL||0),"fail"),badge("golden PASS "+(counts.golden.PASS||0),"pass"),badge("golden FAIL "+(counts.golden.FAIL||0),"fail"));return wrap}
function metricCard(m){const card=node("article","citation"),path=node("div","path");path.append(badge(m.axis_id+" · "+m.axis_name,"axis"),node("span","","›"),badge(m.subaxis_id+" · "+m.subaxis_name,"subaxis"),node("span","","›"),badge(m.variant_id+" · "+m.variant_name,"variant"));card.append(path);const verdicts=node("div","badges");verdicts.append(badge("source "+(m.source_verdict||"NOT ASSESSED"),verdictClass(m.source_verdict)),badge("golden "+(m.golden_verdict||"NOT ASSESSED"),verdictClass(m.golden_verdict)),badge(Math.round((m.confidence||0)*100)+"% confidence",""));card.append(verdicts);card.append(node("p","",m.expected_behavior));return card}
function renderConversation(c){state.selected=c.source_id;const host=document.querySelector("#detail");host.replaceChildren();const head=node("div","detail-head"),copy=node("div");copy.append(node("p","eyebrow","CONVERSATION "+c.number),node("h2","",c.classification.domain+" · "+c.classification.primary_language),node("p","",c.source_id+" · config "+c.configuration_id));head.append(copy,badge(c.review_outcome+" · "+c.terminal.status,verdictClass(c.review_outcome)));host.append(head);const audit=node("section","panel");audit.append(node("h3","","Conversation classification"));const auditBadges=node("div","badges");auditBadges.append(badge("final "+c.review_outcome,verdictClass(c.review_outcome)),badge("source audit "+(c.audit.verdict||"PENDING"),verdictClass(c.audit.verdict)),badge("configuration "+c.configuration_status,c.configuration_status==="QUALIFIED"?"pass":"pending"));audit.append(auditBadges,node("p","",c.terminal.reason||"Independent verification is still in progress."));host.append(audit);const history=node("section","panel");history.append(node("h3","","Refinement and verification iterations"));const strip=node("div","iterations");if(!c.iterations.length)strip.append(node("span","empty","No refinement was attempted."));c.iterations.forEach(i=>{const d=node("div","iteration");d.append(node("strong","",i.kind+" "+i.round),node("div","",i.replaced+" assistant changes"),badge(i.verifier_decision,verdictClass(i.verifier_decision)));strip.append(d)});history.append(strip);host.append(history);const turns=node("section","turns");c.turns.forEach(t=>{const card=node("article","turn "+t.role),meta=node("div","turn-head");meta.append(badge(t.role,t.role),node("code","",t.turn_id));if(t.role==="user"){meta.append(badge("source preserved","keep"));card.append(meta,node("p","utterance",t.text));turns.append(card);return}meta.append(badge(t.action,t.action==="REPLACE"?"replace":t.action==="KEEP"?"keep":"pending"));if(t.source_metric_result!=="NOT_ASSESSED")meta.append(badge("source "+t.source_metric_result,verdictClass(t.source_metric_result)));if(t.golden_metric_result!=="NOT_ASSESSED")meta.append(badge("golden "+t.golden_metric_result,verdictClass(t.golden_metric_result)));card.append(meta);if(t.action==="REPLACE"){const compare=node("div","compare"),before=node("div","pane source"),after=node("div","pane golden");before.append(node("h4","","Source assistant"),node("p","utterance",t.source_text));after.append(node("h4","","Refined assistant"),node("p","utterance",t.golden_text));compare.append(before,after);card.append(compare)}else{card.append(node("p","utterance",t.golden_text||t.source_text))}if(t.correction_reason)card.append(node("p","reason",t.correction_reason));const metrics=node("details","metrics");metrics.append(node("summary","",t.metric_citations.length+" metric citation(s): Axis → Sub-axis → Variant"));t.metric_citations.forEach(m=>metrics.append(metricCard(m)));card.append(metrics);turns.append(card)});host.append(turns);renderList()}
function filtered(){const q=document.querySelector("#search").value.toLowerCase(),outcome=document.querySelector("#outcome").value,status=document.querySelector("#status").value,change=document.querySelector("#change").value;return state.data.conversations.filter(c=>(outcome==="ALL"||c.review_outcome===outcome)&&(status==="ALL"||c.terminal.status===status)&&(change==="ALL"||(change==="CHANGED")===changed(c))&&JSON.stringify({id:c.source_id,classification:c.classification}).toLowerCase().includes(q))}
function renderList(){const host=document.querySelector("#conversationList");host.replaceChildren();const rows=filtered();rows.forEach(c=>{const b=node("button","conversation-item "+(state.selected===c.source_id?"selected":""));b.type="button";b.append(node("strong","",c.classification.domain+" · "+c.classification.primary_language));const labels=node("span","badges");labels.append(badge(c.review_outcome,verdictClass(c.review_outcome)),badge(c.turns.filter(t=>t.action==="REPLACE").length+" changed",""));b.append(labels,node("small","",c.source_id+" · "+c.terminal.status));b.addEventListener("click",()=>renderConversation(c));host.append(b)});if(!rows.length)host.append(node("p","empty","No conversations match these filters."))}
function metricMatches(m){const q=document.querySelector("#metricSearch").value.toLowerCase(),result=document.querySelector("#metricResult").value,axis=document.querySelector("#axis").value,subaxis=document.querySelector("#subaxis").value,variant=document.querySelector("#variant").value;const resultOk=result==="ALL"||(result==="SOURCE_FAIL"&&m.source_verdict==="FAIL")||(result==="SOURCE_PASS"&&m.source_verdict==="PASS")||(result==="GOLDEN_FAIL"&&m.golden_verdict==="FAIL")||(result==="GOLDEN_PASS"&&m.golden_verdict==="PASS");return resultOk&&(axis==="ALL"||m.axis_id===axis)&&(subaxis==="ALL"||m.subaxis_id===subaxis)&&(variant==="ALL"||m.variant_id===variant)&&JSON.stringify(m).toLowerCase().includes(q)}
function renderMetricHierarchy(){const host=document.querySelector("#metricSummary");host.replaceChildren(node("h2","","Metric taxonomy"),node("p","","Rollup of all citations. Expand an axis, then a sub-axis, to inspect its independent variants."));state.data.metric_hierarchy.forEach(a=>{const axis=node("details","panel"),summary=node("summary","path");summary.append(badge(a.axis_id+" · "+a.axis_name,"axis"),node("span","",a.counts.citations+" citations · "+a.counts.conversations+" conversations"),resultBadges(a.counts));axis.append(summary);a.subaxes.forEach(s=>{const sub=node("details","citation"),subSummary=node("summary","path");subSummary.append(badge(s.subaxis_id+" · "+s.subaxis_name,"subaxis"),node("span","",s.counts.citations+" citations"),resultBadges(s.counts));sub.append(subSummary);s.variants.forEach(v=>{const item=node("article","citation"),title=node("div","path");title.append(badge(v.variant_id+" · "+v.variant_name,"variant"),node("span","",v.counts.turns+" turns · "+v.counts.conversations+" conversations"));item.append(title,resultBadges(v.counts));sub.append(item)});axis.append(sub)});host.append(axis)})}
function renderMetrics(){const host=document.querySelector("#metricRows");host.replaceChildren();const rows=state.data.metric_coverage.filter(metricMatches);rows.forEach(m=>{const r=node("tr");[m.conversation_number+" · "+m.source_id,m.turn_id,m.action,m.axis_id+" · "+m.axis_name,m.subaxis_id+" · "+m.subaxis_name,m.variant_id+" · "+m.variant_name,m.source_verdict||"NOT ASSESSED",m.golden_verdict||"NOT ASSESSED",m.severity||"N/A",Math.round((m.confidence||0)*100)+"%"].forEach((x,index)=>{const cell=node("td","",x);if(index===6||index===7)cell.className=verdictClass(x);r.append(cell)});host.append(r)});if(!rows.length){const r=node("tr"),cell=node("td","empty","No metric citations match these filters.");cell.colSpan=10;r.append(cell);host.append(r)}}
function fillTaxonomySelect(id,idKey,nameKey){const select=document.querySelector("#"+id),values=new Map(state.data.metric_coverage.map(m=>[m[idKey],m[nameKey]]));[...values].sort((a,b)=>a[0].localeCompare(b[0])).forEach(([value,label])=>{const option=node("option","",value+" · "+label);option.value=value;select.append(option)})}
function start(data){state.data=data;document.querySelector("#runline").textContent=data.run_id+" · "+data.model_policy;document.querySelector("#notice").textContent=data.disclaimer;const outcomes=data.conversations.reduce((c,x)=>(c[x.review_outcome]=(c[x.review_outcome]||0)+1,c),{}),stats=document.querySelector("#stats");stats.replaceChildren();[[data.counts.conversations,"Conversations"],[outcomes.PASS||0,"PASS — verified"],[outcomes.FAIL||0,"FAIL — quarantined"],[data.counts.replaced_assistant_turns,"Changed assistant turns"],[data.counts.metric_citations,"Metric citations"]].forEach(([n,l])=>{const c=node("div","stat");c.append(node("strong","",n),node("span","",l));stats.append(c)});[...new Set(data.conversations.map(c=>c.terminal.status))].sort().forEach(s=>{const option=node("option","",s);option.value=s;document.querySelector("#status").append(option)});fillTaxonomySelect("axis","axis_id","axis_name");fillTaxonomySelect("subaxis","subaxis_id","subaxis_name");fillTaxonomySelect("variant","variant_id","variant_name");["search","outcome","status","change"].forEach(id=>document.querySelector("#"+id).addEventListener(id==="search"?"input":"change",renderList));document.querySelector("#metricSearch").addEventListener("input",renderMetrics);["metricResult","axis","subaxis","variant"].forEach(id=>document.querySelector("#"+id).addEventListener("change",renderMetrics));document.querySelectorAll(".tab").forEach(b=>b.addEventListener("click",()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));b.classList.add("active");const metric=b.dataset.view==="metrics";document.querySelector("#conversationView").hidden=metric;document.querySelector("#metricView").hidden=!metric;if(metric)renderMetrics()}));renderMetricHierarchy();renderList();renderMetrics();if(data.conversations.length)renderConversation(data.conversations[0])}
fetch("/review.json",{credentials:"same-origin"}).then(r=>{if(!r.ok)throw Error("Load failed "+r.status);return r.json()}).then(start).catch(e=>document.body.replaceChildren(node("div","empty",e.message)));'''


# Keep the generated site assets independently editable and testable.
_FACTORY_ASSETS = Path(__file__).resolve().parents[2] / "plugins" / "review-website" / "assets"
INDEX = (_FACTORY_ASSETS / "factory-index.html").read_text(encoding="utf-8")
STYLES = (_FACTORY_ASSETS / "factory-styles.css").read_text(encoding="utf-8")
APP = (_FACTORY_ASSETS / "factory-app.js").read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the protected factory refinement and metric-coverage UI"
    )
    parser.add_argument("run_id")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--site", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.root.resolve(), args.run_id, args.site.resolve()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
