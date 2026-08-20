"""Live operational status for a governed factory run.

Reads the durable stores directly, so it reports what the pipeline actually did
rather than what a worker last announced. Read-only: it opens every SQLite file
in immutable mode and exposes no mutation endpoint.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from http.cookies import SimpleCookie
from http import HTTPStatus
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import unquote

from .config import load_config
from .review_feedback import ReviewFeedbackError, ReviewFeedbackStore


ACQUISITION = ("trace_fetch", "prepare_packets", "agent_audit")
REFINEMENT = ("refine", "verify", "repair", "trajectory_gate", "verify_repair", "terminal")
STAGE_ORDER = ACQUISITION + REFINEMENT
CANDIDATE_STATUSES = ("VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE")


def _query(path: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    if not path.is_file():
        return []
    # mode=ro, not immutable=1: an immutable connection caches the file and its
    # schema, so it never sees rows or columns added after it opened — which is
    # exactly wrong for a live dashboard. Read-only still cannot write, and WAL
    # keeps it from blocking the writer.
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()


def _concurrent_model_calls() -> int:
    """Count live `codex exec` processes pinned to the harness model."""

    count = 0
    proc = Path("/proc")
    if not proc.is_dir():
        return 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            )
        except OSError:
            continue
        if "codex" in cmdline and "gpt-5.6-sol" in cmdline and " exec " in cmdline:
            count += 1
    return count


def _run_process() -> dict | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            )
        except OSError:
            continue
        if "zen-factory-run" in cmdline and "bin/" in cmdline:
            try:
                started = entry.stat().st_mtime
            except OSError:
                started = time.time()
            match = re.search(r"zen-factory-run\s+(\S+)", cmdline)
            return {
                "pid": int(entry.name),
                "run_id": match.group(1) if match else None,
                "uptime_seconds": max(0, round(time.time() - started)),
            }
    return None


def _turn_quality(root: Path, run_id: str) -> dict:
    """Aggregate per-turn signals from the refiner decisions written so far."""

    quality = Counter()
    kept = replaced = divergent = unassessable = turns = conversations = 0
    for path in sorted((root / ".zen" / "jobs" / run_id).glob("rp_*/refiner.json")):
        try:
            decision = json.loads(path.read_text(encoding="utf-8"))["decision"]
        except (OSError, ValueError, KeyError):
            continue
        rows = decision.get("assistant_turns") or []
        if not rows:
            continue
        conversations += 1
        turns += len(rows)
        for row in rows:
            quality[row.get("source_quality") or "UNKNOWN"] += 1
            if row.get("action") == "KEEP":
                kept += 1
            else:
                replaced += 1
            if row.get("downstream_coherence") == "DIVERGENT":
                divergent += 1
            if row.get("evidence_status") == "INSUFFICIENT":
                unassessable += 1
    return {
        "conversations": conversations,
        "turns": turns,
        "kept": kept,
        "replaced": replaced,
        "divergent": divergent,
        "unassessable": unassessable,
        "quality": {
            key: quality.get(key, 0)
            for key in ("PERFECT", "MINOR_GAP", "MAJOR_GAP", "CRITICAL_GAP")
        },
    }


def snapshot(root: Path, run_id: str) -> dict:
    zen = root / ".zen"
    work = _query(
        zen / "factory-queue.db",
        "SELECT stage,status,payload_json,error,updated_at FROM factory_work WHERE run_id=?",
        (run_id,),
    )
    stages: dict[str, Counter] = {stage: Counter() for stage in STAGE_ORDER}
    terminal = Counter()
    errors: Counter = Counter()
    completion_times: list[float] = []
    for row in work:
        stages.setdefault(row["stage"], Counter())[row["status"]] += 1
        if row["stage"] == "terminal" and row["status"] == "SUCCEEDED":
            try:
                terminal[json.loads(row["payload_json"])["inputs"]["terminal_status"]] += 1
            except (ValueError, KeyError):
                pass
            completion_times.append(row["updated_at"])
        if row["status"] == "DEAD" and row["error"]:
            errors[row["error"].strip().splitlines()[-1][:180]] += 1

    control = _query(
        zen / "factory-control.db",
        "SELECT status,reason FROM factory_runs WHERE id=?",
        (run_id,),
    )
    selected = len(_query(
        zen / "factory-qualification.db",
        "SELECT 1 FROM factory_configuration_sample WHERE run_id=?",
        (run_id,),
    ))
    terminal_total = sum(terminal.values())
    candidates = sum(terminal.get(name, 0) for name in CANDIDATE_STATUSES)

    # Throughput from the last hour of committed terminals.
    now = time.time()
    recent = [value for value in completion_times if now - value <= 3600]
    per_hour = len(recent)
    remaining = max(0, selected - terminal_total)
    eta_seconds = round(remaining / per_hour * 3600) if per_hour and remaining else None

    return {
        "run_id": run_id,
        "generated_at": now,
        "control_status": control[0]["status"] if control else "UNKNOWN",
        "control_reason": (control[0]["reason"] if control else "") or "",
        "process": _run_process(),
        "model_calls": _concurrent_model_calls(),
        "selected": selected,
        "terminal_total": terminal_total,
        "remaining": remaining,
        "candidates": candidates,
        "yield_pct": round(100 * candidates / terminal_total, 1) if terminal_total else 0.0,
        "terminal": dict(terminal),
        "stages": {
            stage: {
                "succeeded": counts.get("SUCCEEDED", 0),
                "active": counts.get("LEASED", 0),
                "queued": counts.get("READY", 0),
                "dead": counts.get("DEAD", 0),
            }
            for stage, counts in stages.items()
        },
        "acquisition_stages": list(ACQUISITION),
        "refinement_stages": list(REFINEMENT),
        "throughput_per_hour": per_hour,
        "eta_seconds": eta_seconds,
        "turn_quality": _turn_quality(root, run_id),
        "errors": [{"message": key, "count": value} for key, value in errors.most_common(5)],
    }


# One entry per run: several dataset versions are served side by side.
_REVIEW_CACHE: dict[str, dict] = {}


def _review(root: Path, run_id: str, ttl: float = 120.0) -> dict:
    """Cache the assembled review; it re-reads every packet on every miss.

    At ~1,800 conversations a rebuild takes tens of seconds, so a short TTL made
    the page appear to hang. Two minutes is well inside how fast a run's terminal
    outcomes actually change.
    """

    from .factory_review import build_review

    now = time.time()
    cached = _REVIEW_CACHE.get(run_id)
    if cached and now - cached["at"] < ttl:
        return cached["value"]
    value = build_review(root, run_id)
    _REVIEW_CACHE[run_id] = {"at": now, "value": value}
    return value


def _turn_stats(conversation: dict) -> dict:
    quality = Counter()
    kept = replaced = excluded = assistant = 0
    for turn in conversation.get("turns", []):
        if turn.get("role") != "assistant":
            continue
        assistant += 1
        if turn.get("source_quality"):
            quality[turn["source_quality"]] += 1
        if turn.get("action") == "KEEP":
            kept += 1
        elif turn.get("action") == "REPLACE":
            replaced += 1
        if turn.get("excluded_from_golden"):
            excluded += 1
    return {
        "assistant_turns": assistant,
        "kept": kept,
        "replaced": replaced,
        "excluded": excluded,
        "quality": {
            key: quality.get(key, 0)
            for key in ("PERFECT", "MINOR_GAP", "MAJOR_GAP", "CRITICAL_GAP")
        },
    }



def _metric_paths(conversation: dict) -> list[dict]:
    """Compact, transcript-free metric paths for list filtering."""

    paths: dict[tuple[str, str, str], dict] = {}
    for turn in conversation.get("turns", []):
        for citation in turn.get("metric_citations") or []:
            key = tuple(str(citation.get(name) or "") for name in (
                "axis_id", "subaxis_id", "variant_id"
            ))
            if not all(key):
                continue
            item = paths.setdefault(key, {
                "axis_id": key[0], "axis_name": citation.get("axis_name") or key[0],
                "subaxis_id": key[1],
                "subaxis_name": citation.get("subaxis_name") or key[1],
                "variant_id": key[2],
                "variant_name": citation.get("variant_name") or key[2],
                "citations": 0,
            })
            item["citations"] += 1
    return sorted(paths.values(), key=lambda item: (
        item["axis_id"], item["subaxis_id"], item["variant_id"]
    ))


def _metric_hierarchy(items: Sequence[dict]) -> list[dict]:
    """Aggregate compact conversation paths as Axis -> Subaxis -> Variant."""

    axes: dict[str, dict] = {}
    for conversation in items:
        for path in conversation.get("metrics") or []:
            axis = axes.setdefault(path["axis_id"], {
                "axis_id": path["axis_id"], "axis_name": path["axis_name"],
                "conversations": set(), "subaxes": {},
            })
            axis["conversations"].add(conversation["source_id"])
            subaxis = axis["subaxes"].setdefault(path["subaxis_id"], {
                "subaxis_id": path["subaxis_id"],
                "subaxis_name": path["subaxis_name"],
                "conversations": set(), "variants": {},
            })
            subaxis["conversations"].add(conversation["source_id"])
            variant = subaxis["variants"].setdefault(path["variant_id"], {
                "variant_id": path["variant_id"],
                "variant_name": path["variant_name"],
                "conversations": set(), "citations": 0,
            })
            variant["conversations"].add(conversation["source_id"])
            variant["citations"] += int(path.get("citations") or 0)
    output = []
    for axis in axes.values():
        subaxes = []
        for subaxis in axis["subaxes"].values():
            variants = [
                {**variant, "conversations": len(variant["conversations"])}
                for variant in subaxis["variants"].values()
            ]
            subaxes.append({**subaxis, "conversations": len(subaxis["conversations"]),
                            "variants": variants})
        output.append({**axis, "conversations": len(axis["conversations"]),
                       "subaxes": subaxes})
    return output


def _facet(values: Sequence[str]) -> list[dict]:
    return [
        {"value": value, "count": count}
        for value, count in sorted(Counter(values).items(), key=lambda row: (-row[1], row[0]))
    ]


def _packet_context(root: Path, run_id: str, packet_id: str) -> dict:
    """Load restricted prompt/agent detail only for the selected conversation."""

    rows = _query(
        root / ".zen" / "factory-qualification.db",
        """SELECT sample.packet_batch,sample.packet_index,configuration.agent_id,
                  configuration.agent_version,configuration.system_prompt_sha256
           FROM factory_configuration_sample AS sample
           JOIN factory_configuration AS configuration
             ON configuration.run_id=sample.run_id
            AND configuration.configuration_key=sample.configuration_key
           WHERE sample.run_id=? AND sample.packet_id=?""",
        (run_id, packet_id),
    )
    if not rows:
        return {}
    row = rows[0]
    batch = Path(row["packet_batch"])
    if not batch.is_absolute():
        batch = root / batch
    batch = batch.resolve()
    if root.resolve() not in batch.parents or not batch.is_file():
        return {}
    try:
        envelope = json.loads(batch.read_text(encoding="utf-8"))
        packet = envelope["result"]["packets"][int(row["packet_index"])]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return {}
    source = packet.get("source") or {}
    return {
        "agent_id": source.get("agent_id") or row["agent_id"],
        "agent_version": source.get("agent_version") or row["agent_version"],
        "call_id": source.get("call_id"),
        "system_prompt": packet.get("system_prompt") or "",
        "system_prompt_sha256": source.get("system_prompt_sha256")
            or row["system_prompt_sha256"],
    }


def _judge_verdicts(root: Path, run_id: str) -> dict[str, dict]:
    """Judge verdicts keyed by source_id, for display alongside each candidate."""

    rows = _query(
        root / ".zen" / "qa-audit.db",
        "SELECT source_id, judge_verdict, judge_summary, findings_json "
        "FROM qa_audits WHERE run_id=?",
        (run_id,),
    )
    out = {}
    for row in rows:
        try:
            findings = json.loads(row["findings_json"] or "[]")
        except ValueError:
            findings = []
        out[row["source_id"]] = {
            "verdict": row["judge_verdict"],
            "summary": row["judge_summary"],
            "harmful": sum(1 for f in findings if f.get("kind") == "judge-harmful"),
            "unnecessary": sum(1 for f in findings if f.get("kind") == "judge-unnecessary"),
            "findings": findings,
        }
    return out


def conversation_index(root: Path, run_id: str, previous: Sequence[str] = ()) -> dict:
    """Light list payload: summaries only, so 1000 conversations stay loadable.

    Earlier runs are shown alongside the live one. Work done before a policy
    change is still work, and hiding it makes the corpus look smaller than it is.
    Each conversation carries the dataset version it belongs to; when the same
    source appears in both, the newer refinement wins.
    """

    items = []
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    disclaimer = ""
    versions = [("v2", run_id)] + [("v1", earlier) for earlier in previous]
    for version, current_run in versions:
        try:
            review = _review(root, current_run)
        except (OSError, ValueError, KeyError):
            continue
        judged = _judge_verdicts(root, current_run)
        disclaimer = disclaimer or review.get("disclaimer", "")
        for key, value in review["counts"].items():
            if isinstance(value, int):
                counts[key] += value
        for conversation in review["conversations"]:
            if conversation["source_id"] in seen:
                continue
            seen.add(conversation["source_id"])
            classification = conversation.get("classification") or {}
            metrics = _metric_paths(conversation)
            review_state = (conversation.get("human_review") or {}).get("state") or "UNAVAILABLE"
            items.append({
                "dataset": version,
                "run_id": current_run,
                "number": conversation["number"],
                "source_id": conversation["source_id"],
                "status": conversation["terminal"]["status"],
                "reason": conversation["terminal"].get("reason") or "",
                "domain": classification.get("domain") or "unclassified",
                "language": classification.get("primary_language") or "—",
                "other_languages": classification.get("other_languages") or [],
                "code_switching": bool(classification.get("code_switching")),
                "agent_id": conversation.get("agent_id") or "unknown",
                "agent_version": conversation.get("agent_version"),
                "configuration_id": conversation.get("configuration_id") or "unknown",
                "configuration_status": conversation.get("configuration_status"),
                "review_state": review_state,
                "audit": (conversation.get("audit") or {}).get("verdict"),
                "metrics": metrics,
                "metric_keys": [path["variant_id"] for path in metrics],
                "prompt_usable": conversation.get("prompt_usable"),
                "turns": len(conversation.get("turns", [])),
                "iterations": len(conversation.get("iterations", [])),
                "judge": (judged.get(conversation["source_id"]) or {}).get("verdict"),
                "judge_harmful": (
                    judged.get(conversation["source_id"]) or {}
                ).get("harmful", 0),
                **_turn_stats(conversation),
            })
    # Numbering spans the merged corpus so every row has a distinct label.
    for position, item in enumerate(items, start=1):
        item["number"] = position
    return {
        "run_id": run_id,
        "datasets": [{"version": v, "run_id": r} for v, r in versions],
        "generated_at": time.time(),
        "counts": dict(counts),
        "disclaimer": disclaimer,
        "facets": {
            "domains": _facet([item["domain"] for item in items]),
            "agents": _facet([item["agent_id"] for item in items]),
            "languages": _facet([item["language"] for item in items]),
            "review_states": _facet([item["review_state"] for item in items]),
        },
        "metric_hierarchy": _metric_hierarchy(items),
        "conversations": items,
    }


def conversation_detail(root: Path, run_id: str, source_id: str) -> dict | None:
    for conversation in _review(root, run_id)["conversations"]:
        if conversation["source_id"] == source_id:
            return {
                **conversation,
                "source_context": _packet_context(root, run_id, conversation["packet_id"]),
                "stats": _turn_stats(conversation),
                "judge": _judge_verdicts(root, run_id).get(source_id),
            }
    return None


THEME = """
:root{
  color-scheme:light;
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --ring:rgba(11,11,11,.10);
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --q1:#86b6ef; --q2:#5598e7; --q3:#2a78d6; --q4:#1c5cab;
  --track:#e1e0d9; --add:rgba(12,163,12,.16); --del:rgba(208,59,59,.16);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --ring:rgba(255,255,255,.10); --track:#2c2c2a;
  --add:rgba(12,163,12,.26); --del:rgba(208,59,59,.26);
}}
:root[data-theme="dark"]{
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --ring:rgba(255,255,255,.10); --track:#2c2c2a;
  --add:rgba(12,163,12,.26); --del:rgba(208,59,59,.26);
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
nav.top{display:flex;gap:6px;align-items:center;padding:10px 20px;
  border-bottom:1px solid var(--ring);background:var(--surface);position:sticky;top:0;z-index:5}
nav.top a{color:var(--ink2);text-decoration:none;font-size:13px;font-weight:600;
  padding:5px 11px;border-radius:7px}
nav.top a.on{background:var(--plane);color:var(--ink);box-shadow:0 0 0 1px var(--ring)}
nav.top .sp{flex:1}
nav.top .run{font-family:ui-monospace,monospace;font-size:11px;color:var(--muted)}
.pill{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:999px;
  font-size:11px;font-weight:650;border:1px solid var(--ring);white-space:nowrap}
.sw{width:9px;height:9px;border-radius:3px;flex:none}
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zen Factory Status</title>
<style>__THEME__
:root{
  color-scheme:light;
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --ring:rgba(11,11,11,.10);
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --q1:#86b6ef; --q2:#5598e7; --q3:#2a78d6; --q4:#1c5cab;
  --track:#e1e0d9;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --ring:rgba(255,255,255,.10); --track:#2c2c2a;
}}
:root[data-theme="dark"]{
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --ring:rgba(255,255,255,.10); --track:#2c2c2a;
}
*{box-sizing:border-box}
.wrap{padding:24px}
.wrap{max-width:1080px;margin:0 auto}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;margin-bottom:4px}
h1{font-size:20px;margin:0;font-weight:650}
.run{font-family:ui-monospace,monospace;font-size:12px;color:var(--muted)}
.sub{color:var(--ink2);font-size:13px;margin:0 0 20px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;
  font-size:12px;font-weight:600;border:1px solid var(--ring)}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
section{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
  padding:18px;margin-bottom:16px}
h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  margin:0 0 14px;font-weight:650}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px}
.tile .v{font-size:28px;font-weight:660;line-height:1.1}
.tile .l{font-size:12px;color:var(--ink2);margin-top:2px}
.tile .n{font-size:11px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:11px;
  text-transform:uppercase;letter-spacing:.06em;padding:0 8px 8px 0}
td{padding:7px 8px 7px 0;border-top:1px solid var(--grid);vertical-align:middle}
td.n{text-align:right;font-variant-numeric:tabular-nums;width:52px}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--track);min-width:120px}
.bar i{display:block;height:100%;box-shadow:0 0 0 1px var(--surface) inset}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:14px;font-size:12px;color:var(--ink2)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:10px;height:10px;border-radius:3px;flex:none}
.err{font-family:ui-monospace,monospace;font-size:12px;color:var(--ink2);
  border-top:1px solid var(--grid);padding:8px 0}
.scroll{overflow-x:auto}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:8px}
</style></head><body>
<nav class="top"><a href="./" class="on">Status</a><a href="conversations">Conversations</a>
<span class="sp"></span></nav>
<div class="wrap" id="app">Loading…</div>
<script>
const STATUS={VERIFIED_CANDIDATE:{c:"var(--good)",i:"\\u2713",l:"Verified"},
 PARTIAL_CANDIDATE:{c:"var(--warning)",i:"\\u25D0",l:"Partial"},
 QUARANTINED:{c:"var(--serious)",i:"\\u2298",l:"Quarantined"}};
const QUAL=[["PERFECT","var(--q1)"],["MINOR_GAP","var(--q2)"],
 ["MAJOR_GAP","var(--q3)"],["CRITICAL_GAP","var(--q4)"]];
const esc=s=>String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const dur=s=>s==null?"—":s<60?s+"s":s<3600?Math.round(s/60)+"m":(s/3600).toFixed(1)+"h";
function bar(parts,total){
  if(!total)return '<div class="bar"></div>';
  return '<div class="bar">'+parts.filter(p=>p[0]>0).map(p=>
    `<i style="width:${(100*p[0]/total).toFixed(2)}%;background:${p[1]}"></i>`).join("")+'</div>';
}
function render(d){
  const running=!!d.process, t=d.terminal||{};
  const tiles=[
    ["Selected",d.selected,"conversations sourced"],
    ["Terminal",d.terminal_total,`${d.remaining} remaining`],
    ["Candidates",d.candidates,`${d.yield_pct}% of terminal`],
    ["Model calls",d.model_calls,"concurrent gpt-5.6-sol"],
    ["Throughput",d.throughput_per_hour,"terminal / last hour"],
    ["ETA",dur(d.eta_seconds),"at current rate"]];
  const stageRow=(s)=>{const v=d.stages[s]||{succeeded:0,active:0,queued:0,dead:0};
    const tot=v.succeeded+v.active+v.queued+v.dead;
    return `<tr><td>${s.replace(/_/g," ")}</td><td class="scroll">${bar([
      [v.succeeded,"var(--good)"],[v.active,"var(--q3)"],
      [v.queued,"var(--track)"],[v.dead,"var(--critical)"]],tot)}</td>
      <td class="n">${v.succeeded}</td><td class="n">${v.active||"·"}</td>
      <td class="n">${v.queued||"·"}</td>
      <td class="n" style="color:${v.dead?"var(--critical)":"inherit"}">${v.dead||"·"}</td></tr>`};
  const q=d.turn_quality, qtot=Object.values(q.quality||{}).reduce((a,b)=>a+b,0);
  document.getElementById("app").innerHTML=`
  <header><h1>Factory status</h1>
    <span class="pill"><span class="dot" style="background:${running?"var(--good)":"var(--muted)"}"></span>
      ${running?"Running":"Not running"}</span>
    <span class="pill">${esc(d.control_status)}</span>
    <span class="run">${esc(d.run_id)}</span></header>
  <p class="sub">${running?`pid ${d.process.pid}, up ${dur(d.process.uptime_seconds)}`:
     (esc(d.control_reason)||"no worker process attached to this run")}</p>
  <section><h2>Headline</h2><div class="tiles">${tiles.map(([l,v,n])=>
    `<div class="tile"><div class="v">${v}</div><div class="l">${l}</div>
     <div class="n">${n}</div></div>`).join("")}</div></section>
  <section><h2>Outcomes</h2>
    ${d.terminal_total?bar(Object.keys(STATUS).map(k=>[t[k]||0,STATUS[k].c]),d.terminal_total)
      :'<p class="sub">No conversation has reached a terminal state yet.</p>'}
    <div class="legend">${Object.entries(STATUS).map(([k,s])=>
      `<span><span class="sw" style="background:${s.c}"></span>${s.i} ${s.l}
       <b style="font-variant-numeric:tabular-nums">${t[k]||0}</b></span>`).join("")}</div>
  </section>
  <section><h2>Pipeline</h2><div class="scroll"><table>
    <thead><tr><th>Stage</th><th>Progress</th><th class="n">Done</th>
      <th class="n">Active</th><th class="n">Queued</th><th class="n">Dead</th></tr></thead>
    <tbody>${d.acquisition_stages.concat(d.refinement_stages).map(stageRow).join("")}</tbody>
  </table></div>
  <div class="legend">
    <span><span class="sw" style="background:var(--good)"></span>Done</span>
    <span><span class="sw" style="background:var(--q3)"></span>Active</span>
    <span><span class="sw" style="background:var(--track)"></span>Queued</span>
    <span><span class="sw" style="background:var(--critical)"></span>Dead</span>
  </div></section>
  <section><h2>Turn quality &middot; ${q.conversations} conversations, ${q.turns} turns</h2>
    ${qtot?bar(QUAL.map(([k,c])=>[q.quality[k]||0,c]),qtot)
      :'<p class="sub">No refiner decisions written yet.</p>'}
    <div class="legend">${QUAL.map(([k,c])=>
      `<span><span class="sw" style="background:${c}"></span>${k.replace("_"," ").toLowerCase()}
       <b style="font-variant-numeric:tabular-nums">${q.quality[k]||0}</b></span>`).join("")}</div>
    <div class="legend" style="border-top:1px solid var(--grid);padding-top:12px">
      <span>kept <b>${q.kept}</b></span><span>replaced <b>${q.replaced}</b></span>
      <span>divergent turns <b>${q.divergent}</b></span>
      <span>unassessable turns <b>${q.unassessable}</b></span></div>
  </section>
  ${d.errors.length?`<section><h2>Dead work</h2>${d.errors.map(e=>
    `<div class="err">${e.count}&times; ${esc(e.message)}</div>`).join("")}</section>`:""}
  <footer>Refreshed ${new Date(d.generated_at*1000).toLocaleTimeString()} &middot; auto every 10s</footer>`;
}
async function tick(){
  try{const r=await fetch("api/status",{cache:"no-store"});
    if(r.ok)render(await r.json());}catch(e){}
}
tick();setInterval(tick,10000);
</script></body></html>"""


CONVERSATIONS = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zen Conversations</title>
<style>__THEME__
.shell{display:grid;grid-template-columns:390px 1fr;height:calc(100vh - 45px)}
@media(max-width:900px){.shell{grid-template-columns:1fr}.detail{display:none}
  .shell.show-detail .list{display:none}.shell.show-detail .detail{display:block}}
.list{border-right:1px solid var(--ring);overflow-y:auto;background:var(--surface)}
.filters{position:sticky;top:0;background:var(--surface);padding:12px;
  border-bottom:1px solid var(--ring);z-index:2}
.filters input,.filters select{width:100%;padding:7px 9px;border-radius:8px;
  border:1px solid var(--ring);background:var(--plane);color:var(--ink);font:inherit;font-size:12px}
.filter-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:8px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
.chip,.metric-button{cursor:pointer;border:1px solid var(--ring);background:transparent;
  color:var(--ink2);border-radius:999px;padding:3px 8px;font:inherit;font-size:10.5px;
  font-weight:650;display:inline-flex;align-items:center;gap:5px}
.chip[aria-pressed=true],.metric-button[aria-pressed=true]{background:var(--ink);color:var(--plane)}
.metric-browser{margin-top:9px;border-top:1px solid var(--grid);padding-top:8px;max-height:220px;overflow:auto}
.metric-browser>summary{cursor:pointer;font-size:11px;font-weight:700;color:var(--ink2)}
.metric-axis,.metric-sub{margin:6px 0 0 8px}.metric-axis>summary,.metric-sub>summary{cursor:pointer;font-size:11px}
.metric-variants{display:flex;flex-wrap:wrap;gap:4px;margin:5px 0 8px 10px}
.result-count{font-size:11px;color:var(--muted);margin-top:7px}
.row{display:block;width:100%;text-align:left;border:0;border-bottom:1px solid var(--grid);
  background:transparent;color:var(--ink);font:inherit;padding:11px 13px;cursor:pointer}
.row:hover{background:var(--plane)}.row[aria-current=true]{background:var(--plane);box-shadow:inset 3px 0 0 var(--q3)}
.row .l1{display:flex;justify-content:space-between;gap:8px;align-items:center}
.row .id{font-family:ui-monospace,monospace;font-size:11px;color:var(--ink2)}
.row .l2{font-size:11.5px;color:var(--muted);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ds{margin-left:5px;padding:1px 5px;border-radius:5px;font-size:9px;text-transform:uppercase;border:1px solid var(--ring)}
.qbar{display:flex;height:4px;border-radius:3px;overflow:hidden;background:var(--track);margin-top:6px}.qbar i{display:block;height:100%}
.more{width:calc(100% - 24px);margin:12px;padding:8px;border:1px solid var(--ring);border-radius:8px;background:var(--plane);color:var(--ink);cursor:pointer}
.detail{overflow-y:auto;padding:22px 26px 60px}.detail h1{font-size:19px;margin:0 0 4px;font-weight:650}
.meta{color:var(--ink2);font-size:13px;margin:0 0 14px}.badges{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:16px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:11px;padding:14px 16px;margin-bottom:14px}
.card h2{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 9px;font-weight:650}
.prompt{max-height:360px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 ui-monospace,monospace;background:var(--plane);padding:11px;border-radius:8px}
.turn{background:var(--surface);border:1px solid var(--ring);border-radius:11px;padding:13px 15px;margin-bottom:11px}
.turn.user{background:transparent;border-style:dashed}.turn.tool{border-color:var(--q2)}.turn.excluded{opacity:.72;border-color:var(--serious)}
.thead{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-bottom:9px}.tid{font-family:ui-monospace,monospace;font-size:11px;color:var(--muted)}
.utt{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}.cmp{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:760px){.cmp{grid-template-columns:1fr}}.pane h3{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);margin:0 0 5px}
.pane{background:var(--plane);border:1px solid var(--ring);border-radius:9px;padding:10px 11px}ins{background:var(--add);text-decoration:none}del{background:var(--del)}
.why{font-size:13px;color:var(--ink2);margin:10px 0 0;padding-left:11px;border-left:2px solid var(--grid)}
details.cites,details.prompt-box{margin-top:10px}details.cites summary,details.prompt-box summary{cursor:pointer;font-size:12px;color:var(--ink2);font-weight:650}
.cite{border-top:1px solid var(--grid);padding:9px 0;font-size:12px}.cite .path{font-weight:700}.cite .ids{font-family:ui-monospace,monospace;color:var(--muted);font-size:10.5px}.cite .ev{color:var(--ink2);margin-top:3px}.cite .q{color:var(--muted);font-style:italic;margin-top:3px}
.iteration{border-top:1px solid var(--grid);padding:9px 0}.empty{color:var(--muted);padding:40px 10px;text-align:center}.warn{border:1px solid var(--serious);border-radius:9px;padding:9px 12px;font-size:12px;margin-bottom:14px}
.back{display:none}@media(max-width:900px){.back{display:inline-flex;margin-bottom:12px}}
</style></head><body>
<nav class="top"><a href="./">Status</a><a href="conversations" class="on">Conversations</a><span class="sp"></span><span class="run" id="run"></span></nav>
<div class="shell" id="shell"><div class="list"><div class="filters">
<input id="q" type="search" placeholder="Search conversation, agent, domain, language, metric…" aria-label="Search conversations">
<div class="filter-grid"><select id="dataset"></select><select id="status"></select><select id="domain"></select><select id="agent"></select><select id="language"></select><select id="review"></select></div>
<div class="chips" id="chips"></div><details class="metric-browser" id="metric-browser"><summary>Metric hierarchy · Axis → Subaxis → Variant</summary><div id="metrics"></div></details>
<div class="result-count" id="result-count"></div></div><div id="rows"></div></div>
<div class="detail" id="detail"><p class="empty">Select a conversation to inspect the prompt, turns, changes, metrics, and verification history.</p></div></div>
<script>
const ST={AWAITING_AUDIT:{c:"var(--muted)",l:"Awaiting audit"},QUEUED:{c:"var(--muted)",l:"Queued"},IN_PROGRESS:{c:"var(--q3)",l:"In progress"},VERIFIED_CANDIDATE:{c:"var(--good)",l:"Verified"},PARTIAL_CANDIDATE:{c:"var(--warning)",l:"Partial"},QUARANTINED:{c:"var(--serious)",l:"Quarantined"},REJECTED_SOURCE:{c:"var(--critical)",l:"Rejected source"}};
const QC={PERFECT:"var(--q1)",MINOR_GAP:"var(--q2)",MAJOR_GAP:"var(--q3)",CRITICAL_GAP:"var(--q4)"};
const esc=s=>String(s==null?"":s).replace(/[&<>\"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
let DATA=[],SEL=null,Q="",STATUS=new Set(),METRIC="",VISIBLE=250;
function pill(text,color){return `<span class="pill">${color?`<span class="sw" style="background:${color}"></span>`:""}${esc(text)}</span>`}
function diff(a,b){const A=String(a||"").split(/(\s+)/),B=String(b||"").split(/(\s+)/),n=A.length,m=B.length,dp=Array.from({length:n+1},()=>new Uint32Array(m+1));for(let i=n-1;i>=0;i--)for(let j=m-1;j>=0;j--)dp[i][j]=A[i]===B[j]?dp[i+1][j+1]+1:Math.max(dp[i+1][j],dp[i][j+1]);let i=0,j=0,L="",R="";while(i<n&&j<m){if(A[i]===B[j]){L+=esc(A[i]);R+=esc(B[j]);i++;j++}else if(dp[i+1][j]>=dp[i][j+1])L+=`<del>${esc(A[i++])}</del>`;else R+=`<ins>${esc(B[j++])}</ins>`}while(i<n)L+=`<del>${esc(A[i++])}</del>`;while(j<m)R+=`<ins>${esc(B[j++])}</ins>`;return[L,R]}
function qbar(q){const t=Object.values(q||{}).reduce((a,b)=>a+b,0);return t?'<div class="qbar">'+Object.entries(QC).filter(([k])=>q[k]>0).map(([k,c])=>`<i style="width:${100*q[k]/t}%;background:${c}"></i>`).join("")+'</div>':""}
function option(id,label,rows){const el=document.getElementById(id),old=el.value;el.innerHTML=`<option value="">All ${esc(label)}</option>`+(rows||[]).map(x=>`<option value="${esc(x.value)}">${esc(x.value)} (${x.count})</option>`).join("");el.value=old}
function choice(id){return(document.getElementById(id)||{}).value||""}
function metricMatch(c){if(!METRIC)return true;return(c.metrics||[]).some(m=>m.axis_id===METRIC||m.subaxis_id===METRIC||m.variant_id===METRIC)}
function filtered(){const ds=choice("dataset"),st=choice("status"),domain=choice("domain"),agent=choice("agent"),lang=choice("language"),review=choice("review");return DATA.filter(c=>(!ds||c.dataset===ds)&&(!st||c.status===st)&&(!domain||c.domain===domain)&&(!agent||c.agent_id===agent)&&(!lang||c.language===lang)&&(!review||c.review_state===review)&&(!STATUS.size||STATUS.has(c.status))&&metricMatch(c)&&(!Q||[c.source_id,c.agent_id,c.configuration_id,c.domain,c.language,...(c.other_languages||[]),c.reason,c.review_state,...(c.metrics||[]).flatMap(m=>[m.axis_id,m.axis_name,m.subaxis_id,m.subaxis_name,m.variant_id,m.variant_name])].join(" ").toLowerCase().includes(Q)))}
function renderList(){const all=filtered(),items=all.slice(0,VISIBLE),rows=document.getElementById("rows");document.getElementById("result-count").textContent=`${all.length} matching · ${items.length} rendered${METRIC?" · metric "+METRIC:""}`;rows.innerHTML=items.map(c=>{const s=ST[c.status]||{c:"var(--muted)",l:c.status};return `<button class="row" data-id="${esc(c.source_id)}" aria-current="${c.source_id===SEL}"><span class="l1"><span class="id">#${c.number} · ${esc(c.source_id)} <span class="ds">${esc(c.dataset)}</span></span><span>${pill(s.l,s.c)}</span></span><span class="l2">${esc(c.domain)} · ${esc(c.language)}${c.code_switching?" · code-switching":""}</span><span class="l2">agent ${esc(c.agent_id)} · config ${esc(c.configuration_id)} · review ${esc(c.review_state)} · ${c.replaced} changed / ${c.kept} kept</span>${qbar(c.quality)}</button>`}).join("")+(items.length<all.length?`<button class="more" id="more">Load ${Math.min(250,all.length-items.length)} more</button>`:"");rows.querySelectorAll(".row").forEach(b=>b.onclick=()=>select(b.dataset.id));const more=document.getElementById("more");if(more)more.onclick=()=>{VISIBLE+=250;renderList()}}
function renderMetrics(tree){document.getElementById("metrics").innerHTML=(tree||[]).map(a=>`<details class="metric-axis"><summary>${esc(a.axis_id)} · ${esc(a.axis_name)} (${a.conversations})</summary><button class="metric-button" data-metric="${esc(a.axis_id)}">filter axis</button>${a.subaxes.map(s=>`<details class="metric-sub"><summary>${esc(s.subaxis_id)} · ${esc(s.subaxis_name)} (${s.conversations})</summary><button class="metric-button" data-metric="${esc(s.subaxis_id)}">filter subaxis</button><div class="metric-variants">${s.variants.map(v=>`<button class="metric-button" data-metric="${esc(v.variant_id)}">${esc(v.variant_id)} · ${esc(v.variant_name)} (${v.conversations})</button>`).join("")}</div></details>`).join("")}</details>`).join("")||'<p class="result-count">Metrics appear as refinement completes.</p>';document.querySelectorAll(".metric-button").forEach(b=>b.onclick=()=>{METRIC=METRIC===b.dataset.metric?"":b.dataset.metric;document.querySelectorAll(".metric-button").forEach(x=>x.setAttribute("aria-pressed",String(x.dataset.metric===METRIC)));VISIBLE=250;renderList()})}
function citation(m){return `<div class="cite"><div class="path">${esc(m.axis_name||m.axis_id)} → ${esc(m.subaxis_name||m.subaxis_id)} → ${esc(m.variant_name||m.variant_id)}</div><div class="ids">${esc(m.axis_id)} / ${esc(m.subaxis_id)} / ${esc(m.variant_id)}</div><div class="ev">${esc(m.applicability||"APPLICABLE")} · source ${esc(m.source_verdict||"—")} → golden ${esc(m.golden_verdict||"—")}${m.severity?" · "+esc(m.severity):""}</div>${m.trigger_turn_ids?.length?`<div class="ev">trigger: ${esc(m.trigger_turn_ids.join(", "))}</div>`:""}${m.evidence_turn_ids?.length?`<div class="ev">evidence: ${esc(m.evidence_turn_ids.join(", "))}</div>`:""}${m.observed_behavior?`<div class="ev"><b>Observed:</b> ${esc(m.observed_behavior)}</div>`:""}${m.expected_behavior?`<div class="ev"><b>Expected:</b> ${esc(m.expected_behavior)}</div>`:""}${m.missing_evidence?`<div class="ev"><b>Missing:</b> ${esc(m.missing_evidence)}</div>`:""}${m.evidence_quote?`<div class="q">“${esc(m.evidence_quote)}”</div>`:""}</div>`}
async function select(id){SEL=id;renderList();document.getElementById("shell").classList.add("show-detail");const host=document.getElementById("detail");host.innerHTML='<p class="empty">Loading complete conversation…</p>';try{const r=await fetch("api/conversation/"+encodeURIComponent(id),{cache:"no-store"});if(!r.ok)throw new Error("HTTP "+r.status);const c=await r.json(),cl=c.classification||{},src=c.source_context||{},hr=c.human_review||{},st=c.stats,s=ST[c.terminal.status]||{c:"var(--muted)",l:c.terminal.status};const turns=(c.turns||[]).map(t=>{if(t.role==="user")return `<article class="turn user"><div class="thead">${pill("User — source preserved","var(--good)")}<span class="tid">${esc(t.turn_id)}</span></div><p class="utt">${esc(t.text)}</p></article>`;if(t.role==="tool")return `<article class="turn tool"><div class="thead">${pill("Tool result","var(--q2)")}<span class="tid">${esc(t.turn_id)}</span></div><p class="utt">${esc(t.text)}</p></article>`;const ex=t.excluded_from_golden,changed=t.action==="REPLACE"&&t.golden_text;let body;if(changed){const[L,R]=diff(t.source_text,t.golden_text);body=`<div class="cmp"><div class="pane"><h3>Source assistant</h3><p class="utt">${L}</p></div><div class="pane"><h3>Golden assistant</h3><p class="utt">${R}</p></div></div>`}else body=`<p class="utt">${esc(t.golden_text||t.source_text)}</p>`;return `<article class="turn${ex?" excluded":""}"><div class="thead">${pill("Assistant")}${pill(t.action||"NOT_REFINED",changed?"var(--q3)":"var(--good)")}${t.source_quality?pill(t.source_quality,QC[t.source_quality]):""}${pill(t.downstream_coherence||"coherence pending")}${ex?pill("excluded","var(--serious)"):""}<span class="tid">${esc(t.turn_id)}</span></div>${body}${t.correction_reason?`<p class="why"><b>Why changed:</b> ${esc(t.correction_reason)}</p>`:""}${t.divergence_reason?`<p class="why"><b>Divergence:</b> ${esc(t.divergence_reason)}</p>`:""}${(t.metric_citations||[]).length?`<details class="cites"><summary>${t.metric_citations.length} metric citation(s)</summary>${t.metric_citations.map(citation).join("")}</details>`:""}</article>`}).join("");const iterations=(c.iterations||[]).map(i=>`<div class="iteration"><b>${esc(i.kind)} round ${esc(i.round)}</b> · ${i.replaced} replacements · verifier ${esc(i.verifier_decision||"PENDING")}${(i.verifier_findings||[]).map(f=>`<div class="cite"><div class="path">${esc(f.turn_id||"conversation")} · ${esc(f.variant_id||f.category||"finding")}</div><div class="ev">${esc(f.reason||f.suggested_fix||"")}</div></div>`).join("")}</div>`).join("")||'<p class="utt">No completed refinement iteration yet.</p>';host.innerHTML=`<button class="chip back" onclick="document.getElementById('shell').classList.remove('show-detail')">← Back</button><h1>Conversation #${c.number}</h1><p class="meta">${esc(c.source_id_full||c.source_id)} · ${esc(cl.domain||"Unclassified")} · ${esc(cl.primary_language||"Unknown")}</p><div class="badges">${pill(s.l,s.c)}${pill("review "+(hr.state||"UNAVAILABLE"))}${pill("audit "+((c.audit||{}).verdict||"PENDING"))}${pill(st.replaced+" changed")}${pill(st.kept+" kept")}${st.excluded?pill(st.excluded+" excluded","var(--serious)"):""}</div><section class="card"><h2>Agent and source configuration</h2><p class="utt">Agent: ${esc(src.agent_id||c.agent_id||"unknown")} · version: ${esc(src.agent_version||c.agent_version||"—")}<br>Configuration: ${esc(c.configuration_id)} · ${esc(c.configuration_status)}<br>Call: ${esc(src.call_id||c.call_id||"—")}<br>System prompt SHA-256: ${esc(src.system_prompt_sha256||"—")}</p><details class="prompt-box"><summary>Show complete source system prompt</summary><pre class="prompt">${esc(src.system_prompt||"Prompt unavailable for this packet.")}</pre></details></section><section class="card"><h2>Human review and terminal state</h2><p class="utt">State: ${esc(hr.state||"UNAVAILABLE")} · candidate revision ${esc(hr.candidate_revision??"—")} · decision revision ${esc(hr.decision_revision??"—")}</p>${hr.latest_decision?`<p class="why">Latest decision: ${esc(JSON.stringify(hr.latest_decision))}</p>`:""}${c.terminal.reason?`<div class="warn"><b>${esc(c.terminal.status)}:</b> ${esc(c.terminal.reason)}</div>`:""}</section><section class="card"><h2>Refinement and independent verification</h2>${iterations}</section>${c.judge?`<section class="card"><h2>Independent QA judge · ${esc(c.judge.verdict||"PENDING")}</h2><p class="utt">${esc(c.judge.summary||"No judge summary yet.")}</p>${(c.judge.findings||[]).map(f=>`<div class="cite"><div class="path">${esc(f.turn_id)} · ${esc(f.kind)}</div><div class="ev">${esc(f.detail)}</div></div>`).join("")}</section>`:""}<section>${turns}</section>`;host.scrollTop=0}catch(e){host.innerHTML=`<div class="warn"><b>Conversation could not load.</b> ${esc(e.message)}. Reopen the tokenized URL and hard-refresh.</div>`}}
function setup(d){DATA=d.conversations||[];document.getElementById("run").textContent=(d.datasets||[]).map(x=>x.version+" "+x.run_id.slice(0,8)).join(" · ");option("dataset","datasets",[...new Set(DATA.map(c=>c.dataset))].map(v=>({value:v,count:DATA.filter(c=>c.dataset===v).length})));option("status","statuses",[...new Set(DATA.map(c=>c.status))].map(v=>({value:v,count:DATA.filter(c=>c.status===v).length})));option("domain","domains",d.facets?.domains);option("agent","agents",d.facets?.agents);option("language","languages",d.facets?.languages);option("review","review states",d.facets?.review_states);const counts={};DATA.forEach(c=>counts[c.status]=(counts[c.status]||0)+1);document.getElementById("chips").innerHTML=Object.keys(ST).filter(k=>counts[k]).map(k=>`<button class="chip" data-status="${k}" aria-pressed="false"><span class="sw" style="background:${ST[k].c}"></span>${ST[k].l} ${counts[k]}</button>`).join("");document.querySelectorAll(".chip[data-status]").forEach(b=>b.onclick=()=>{STATUS.has(b.dataset.status)?STATUS.delete(b.dataset.status):STATUS.add(b.dataset.status);b.setAttribute("aria-pressed",String(STATUS.has(b.dataset.status)));renderList()});renderMetrics(d.metric_hierarchy);renderList()}
document.getElementById("q").addEventListener("input",e=>{Q=e.target.value.trim().toLowerCase();VISIBLE=250;renderList()});document.querySelectorAll("select").forEach(s=>s.addEventListener("change",()=>{VISIBLE=250;renderList()}));
(async()=>{try{const r=await fetch("api/conversations",{cache:"no-store"});if(!r.ok)throw new Error("HTTP "+r.status);setup(await r.json())}catch(e){document.getElementById("rows").innerHTML=`<div class="warn"><b>Dashboard data failed to load.</b> ${esc(e.message)}. Use the tokenized URL, then hard-refresh.</div>`}})();
</script></body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zen-factory-status",
        description="Serve a live status page for one factory run",
    )
    parser.add_argument("run_id")
    parser.add_argument(
        "--previous", action="append", default=[], metavar="RUN_ID",
        help="an earlier run to show alongside this one as dataset v1 "
             "(repeatable); the run_id argument is v2",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument(
        "--allow-network", action="store_true",
        help="permit binding beyond loopback",
    )
    parser.add_argument("--token", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-auth", action="store_true",
        help="serve without a token; loopback only, never with a tunnel",
    )
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.allow_network:
        raise PermissionError("non-loopback binding requires --allow-network")
    if not 1 <= args.port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    config = load_config(args.root)
    root = config.root
    # This data is RESTRICTED_SOURCE_NOT_DEIDENTIFIED. Anything reachable beyond
    # loopback carries a token by default so a tunnel URL alone is not access.
    token = None if args.no_auth else (
        args.token or os.environ.get("ZEN_STATUS_TOKEN") or secrets.token_urlsafe(24)
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "ZenFactoryStatus/1.0"

        def _authenticated(self) -> bool:
            if token is None:
                return True
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if supplied and secrets.compare_digest(supplied, token):
                return True
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            for part in query.split("&"):
                if part.startswith("token=") and secrets.compare_digest(
                    unquote(part[6:]), token
                ):
                    self._grant = True
                    return True
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookie.get("zen_status_token")
            return bool(value and secrets.compare_digest(value.value, token))

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if getattr(self, "_grant", False) and token:
                # First visit carries the token in the URL; store it so links
                # and the auto-refresh keep working without it.
                self.send_header(
                    "Set-Cookie",
                    f"zen_status_token={token}; Path=/; HttpOnly; SameSite=Lax",
                )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self._grant = False
            if not self._authenticated():
                body = b"Unauthorized. Append ?token=... to the URL."
                self.send_response(401)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            html = "text/html; charset=utf-8"
            if path in {"/", "/index.html"}:
                self._send(PAGE.replace("__THEME__", THEME).encode("utf-8"), html)
            elif path == "/conversations":
                self._send(
                    CONVERSATIONS.replace("__THEME__", THEME).encode("utf-8"), html
                )
            elif path == "/api/status":
                self._send(
                    json.dumps(snapshot(root, args.run_id)).encode("utf-8"),
                    "application/json",
                )
            elif path == "/api/metrics":
                # Observability lives in its own store so the reporting query
                # can never contend with the queue's hot write path.
                from .observability import MetricsStore

                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                wanted = args.run_id
                for part in query.split("&"):
                    if part.startswith("run_id="):
                        wanted = unquote(part[7:]) or args.run_id
                store = MetricsStore(root / ".zen" / "metrics.db")
                try:
                    payload = {
                        "run_id": wanted,
                        "totals": store.totals(wanted),
                        "by_role": store.by_role(wanted),
                        "failures": store.failure_reasons(wanted),
                        "throughput": store.throughput(wanted),
                        "economics": store.cost_per_conversation(wanted),
                    }
                finally:
                    store.close()
                self._send(json.dumps(payload).encode("utf-8"), "application/json")
            elif path == "/api/conversations":
                self._send(
                    json.dumps(
                        conversation_index(root, args.run_id, args.previous)
                    ).encode("utf-8"),
                    "application/json",
                )
            elif path.startswith("/api/conversation/"):
                source_id = unquote(path.rsplit("/", 1)[-1])
                detail = None
                for candidate in [args.run_id, *args.previous]:
                    detail = conversation_detail(root, candidate, source_id)
                    if detail is not None:
                        break
                if detail is None:
                    self.send_error(404)
                    return
                self._send(json.dumps(detail).encode("utf-8"), "application/json")
            else:
                self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            self._grant = False
            if not self._authenticated():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
                return
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path != "/api/corpus-decisions":
                self.send_error(404)
                return
            if self.headers.get("X-Zen-Review") != "1":
                self._json(HTTPStatus.FORBIDDEN, {"error": "missing review request guard"})
                return
            if self.headers.get_content_type() != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application/json required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 32768:
                    raise ValueError("request body must be between 1 and 32768 bytes")
                value = json.loads(self.rfile.read(length))
                allowed = {"source_id", "decision", "reviewer_identity", "summary", "idempotency_key"}
                if not isinstance(value, dict) or set(value) - allowed:
                    raise ValueError("unsupported request fields")
                missing = allowed - set(value)
                if missing:
                    raise ValueError(f"missing required request fields: {sorted(missing)}")
                source_id = value["source_id"]
                decision = value["decision"]
                if decision not in {"KEEP", "REMOVE"}:
                    raise ValueError("decision must be KEEP or REMOVE")
                detail = None
                detail_run = None
                for candidate_run in [args.run_id, *args.previous]:
                    detail = conversation_detail(root, candidate_run, str(source_id))
                    if detail is not None:
                        detail_run = candidate_run
                        break
                if detail is None or detail_run is None:
                    self.send_error(404)
                    return
                human = detail.get("human_review") or {}
                item_id = human.get("item_id")
                if not item_id:
                    raise ReviewFeedbackError("conversation has no human review item yet")
                terminal_status = (detail.get("terminal") or {}).get("status")
                if decision == "KEEP" and terminal_status not in CANDIDATE_STATUSES:
                    raise ReviewFeedbackError("only verified or partial candidates can be kept in corpus")
                action = "APPROVE" if decision == "KEEP" else "REJECT"
                feedback = {
                    "summary": f"Corpus decision: {decision.lower()}. {value['summary']}",
                    "reason_codes": ["CORPUS_" + decision],
                    "evidence_turn_ids": [],
                    "metric_citations": [],
                    "requested_changes": [],
                }
                with ReviewFeedbackStore(root / ".zen" / "review-feedback.db") as store:
                    item = store.get_item(item_id, include_history=False)
                    if item["run_id"] != detail_run:
                        raise ReviewFeedbackError("review item belongs to another run")
                    decision_row = store.record_decision(
                        item_id,
                        action=action,
                        reviewer_identity=value["reviewer_identity"],
                        idempotency_key=value["idempotency_key"],
                        feedback=feedback,
                    )
                    updated = store.get_item(item_id)
                _REVIEW_CACHE.pop(detail_run, None)
                self._json(HTTPStatus.CREATED, {"decision": decision_row, "item": updated})
            except (ValueError, KeyError, ReviewFeedbackError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    suffix = f"?token={token}" if token else ""
    print(
        f"Zen factory status for {args.run_id}\n"
        f"  http://{args.host}:{args.port}/{suffix}\n"
        f"  JSON: http://{args.host}:{args.port}/api/status\n"
        "Ctrl-C to stop.",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
