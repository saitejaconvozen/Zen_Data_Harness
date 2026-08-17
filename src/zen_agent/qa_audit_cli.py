"""`zen-factory-audit` — sample and re-check harness-approved conversations."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from .config import load_config
from .factory_review import build_review
from .qa_audit import (
    CANDIDATE_STATUSES,
    Finding,
    DEFAULT_BATCH,
    DEFAULT_RATE,
    AuditLedger,
    audit_conversation,
    select_sample,
)


def _system_prompts(root: Path, review: dict) -> dict[str, str]:
    """Map source_id -> system prompt by reading the prepared packet batches."""

    by_packet: dict[str, str] = {}
    blobs = root / ".zen" / "factory-artifacts" / "blobs"
    for path in blobs.glob("*/*") if blobs.is_dir() else ():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("tool") != "golden.prepare_refinement_packets":
            continue
        for packet in payload.get("result", {}).get("packets", []):
            if packet.get("packet_id"):
                by_packet[packet["packet_id"]] = packet.get("system_prompt") or ""
    return {
        c.get("source_id"): by_packet.get(c.get("packet_id"), "")
        for c in review["conversations"]
    }


def _run_judge(root: Path, conversation: dict, system_prompt: str) -> dict | None:
    """Ask a fresh model whether each edit improved the turn or damaged it."""

    script = root / "plugins" / "golden-conversations" / "scripts" / "run_judge.py"
    with tempfile.TemporaryDirectory(prefix="zen-judge-case-") as directory:
        case = Path(directory) / "case.json"
        out = Path(directory) / "judgement.json"
        case.write_text(json.dumps(
            {"conversation": conversation, "system_prompt": system_prompt},
            ensure_ascii=False), encoding="utf-8")
        done = subprocess.run(
            [sys.executable, str(script), "--case", str(case), "--output", str(out)],
            capture_output=True, text=True, check=False, timeout=1200,
        )
        if done.returncode != 0:
            return {"error": (done.stderr or done.stdout)[-300:]}
        return json.loads(out.read_text(encoding="utf-8"))["judgement"]


def _judge_batch(root: Path, sample: list[dict], prompts: dict, workers: int) -> dict:
    results: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                _run_judge, root, c, prompts.get(c.get("source_id"), "")
            ): c.get("source_id")
            for c in sample
        }
        for future, source_id in futures.items():
            try:
                results[source_id] = future.result()
            except Exception as exc:
                results[source_id] = {"error": f"{type(exc).__name__}: {exc}"}
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zen-factory-audit",
        description=(
            "Sample a percentage of every batch of newly approved conversations "
            "and re-check them outside the models"
        ),
    )
    parser.add_argument("run_id")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH,
                        help="audit once this many new candidates exist")
    parser.add_argument("--sample-rate", type=float, default=DEFAULT_RATE,
                        help="fraction of each batch to audit")
    parser.add_argument("--all-batches", action="store_true",
                        help="keep auditing while whole batches remain")
    parser.add_argument("--history", action="store_true", help="show past batches and exit")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument(
        "--judge", action="store_true",
        help="also run the GPT-5.6-sol judge over each sampled conversation",
    )
    parser.add_argument("--judge-workers", type=int, default=6)
    parser.add_argument(
        "--full", action="store_true",
        help="audit every candidate not yet audited, ignoring batch sampling",
    )
    parser.add_argument(
        "--chunk", type=int, default=40,
        help="conversations per commit in --full mode, so progress is durable",
    )
    args = parser.parse_args(argv)
    if not 0 < args.sample_rate <= 1 or args.batch_size < 1:
        parser.error("sample-rate must be in (0,1] and batch-size positive")

    config = load_config(args.root)
    ledger = AuditLedger(config.state_directory / "qa-audit.db")
    try:
        if args.history:
            rows = ledger.history(args.run_id)
            print(json.dumps(rows, indent=2) if args.json else
                  "\n".join(f"batch {r['batch']}: sampled {r['sampled']}, "
                            f"flagged {r['flagged']}" for r in rows) or "no audits yet")
            return 0

        review = build_review(config.root, args.run_id)
        # build_review omits the system prompt, so without this every name and
        # figure the prompt supplies looked like fabrication.
        prompts = _system_prompts(config.root, review)
        candidates = [
            c for c in review["conversations"]
            if (c.get("terminal") or {}).get("status") in CANDIDATE_STATUSES
        ]
        candidates.sort(key=lambda c: c.get("number", 0))

        batches = []
        while True:
            if args.full:
                # Sweep everything, committing in chunks so a long run that is
                # interrupted keeps the work it already paid for.
                done = ledger.audited(args.run_id)
                remaining = [c for c in candidates if c["source_id"] not in done]
                sample = remaining[:args.chunk]
            else:
                sample = select_sample(
                    candidates, ledger.audited(args.run_id), args.run_id,
                    batch_size=args.batch_size, rate=args.sample_rate,
                )
            if not sample:
                break
            batch = ledger.next_batch(args.run_id)
            audits = [
                audit_conversation(c, prompts.get(c.get("source_id"), ""))
                for c in sample
            ]
            if args.judge:
                verdicts = _judge_batch(
                    config.root, sample, prompts, args.judge_workers
                )
                for audit in audits:
                    verdict = verdicts.get(audit.source_id) or {}
                    audit.judge_verdict = verdict.get("conversation_verdict")
                    audit.judge_summary = verdict.get("summary")
                    for turn in verdict.get("turns", []):
                        if turn["verdict"] in {"HARMFUL", "UNNECESSARY"}:
                            audit.findings.append(Finding(
                                audit.source_id, turn["turn_id"],
                                f"judge-{turn['verdict'].lower()}",
                                turn["reason"][:200],
                                "SERIOUS" if turn["verdict"] == "HARMFUL" else "REVIEW",
                            ))
            if args.full:
                unaudited = []
            else:
                # Everything in the batch is marked seen; only the sample is checked.
                fresh = [c for c in candidates if c["source_id"] not in ledger.audited(args.run_id)]
                seen = {c["source_id"] for c in fresh[:args.batch_size]}
                unaudited = [
                    type(audits[0])(source_id=sid, number=0, status="NOT_SAMPLED",
                                    assistant_turns=0, replaced=0)
                    for sid in seen - {a.source_id for a in audits}
                ] if audits else []
            ledger.record(args.run_id, batch, audits + unaudited)
            batches.append((batch, audits))
            if args.full:
                total_done = len(ledger.audited(args.run_id))
                print(f"[audit] {total_done}/{len(candidates)} candidates judged",
                      flush=True)
                continue
            if not args.all_batches:
                break

        if not batches:
            done = len(ledger.audited(args.run_id))
            print(
                f"No full batch of {args.batch_size} new candidates yet "
                f"({len(candidates)} candidates, {done} already seen)."
            )
            return 0

        payload = []
        for batch, audits in batches:
            flagged = [a for a in audits if not a.clean]
            payload.append({
                "batch": batch,
                "sampled": len(audits),
                "flagged": len(flagged),
                "conversations": [
                    {
                        "source_id": a.source_id, "number": a.number, "status": a.status,
                        "assistant_turns": a.assistant_turns, "replaced": a.replaced,
                        "judge_verdict": getattr(a, "judge_verdict", None),
                        "findings": [f.as_dict() for f in a.findings],
                    }
                    for a in audits
                ],
            })
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0

        for entry in payload:
            print(f"\n=== QA batch {entry['batch']} — sampled {entry['sampled']}, "
                  f"flagged {entry['flagged']} ===")
            for c in entry["conversations"]:
                mark = "FLAG" if c["findings"] else " ok "
                print(f"  [{mark}] #{c['number']:<5} {c['source_id']}  "
                      f"{c['replaced']}/{c['assistant_turns']} turns replaced")
                for f in c["findings"]:
                    print(f"          {f['severity']:8} {f['kind']:26} {f['turn_id']}: {f['detail'][:90]}")
        total_flag = sum(e["flagged"] for e in payload)
        total = sum(e["sampled"] for e in payload)
        print(f"\n{total_flag} of {total} sampled conversations need a human read.")
        return 2 if total_flag else 0
    except Exception as exc:
        print(f"zen-factory-audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
