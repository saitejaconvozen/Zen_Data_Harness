from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

from zen_agent.models import Plan, TaskSpec, ToolRisk
from zen_agent.plugins import WorkflowSpec
from zen_agent.tools import ToolSpec


def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _source_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if root != path and root not in path.parents:
        raise PermissionError("review batch path escapes harness workspace")
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > 100_000_000:
        raise ValueError("review batch exceeds 100 MB site-build limit")
    return path


def _review_payload(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") == "zen.golden-review-batch/1":
        return value
    candidates = [
        value.get("result", {}).get("review_batch"),
        value.get("review_batch"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("schema_version") == "zen.golden-review-batch/1":
            return candidate
    raise ValueError("input is not a zen.golden-review-batch/1 artifact")


def _validate_review(review: dict) -> None:
    conversations = review.get("conversations")
    if not isinstance(conversations, list):
        raise ValueError("review batch conversations must be an array")
    for conversation in conversations:
        if conversation.get("status") not in {"READY_FOR_HUMAN_REVIEW", "QUARANTINED"}:
            raise ValueError("unknown conversation review status")
        verifier = conversation.get("verifier", {})
        if verifier.get("decision") not in {"PASS", "FAIL", "ABSTAIN"}:
            raise ValueError("missing verifier decision")
        for turn in conversation.get("turns", []):
            if turn.get("role") == "user" and turn.get("source_preserved") is not True:
                raise ValueError("website refuses a mutated user turn")
            if turn.get("role") == "assistant":
                citations = turn.get("metric_citations")
                if not isinstance(citations, list) or not citations:
                    raise ValueError("assistant turn lacks metric citations")
                for citation in citations:
                    for field in ("axis_name", "subaxis_name", "variant_name"):
                        if not isinstance(citation.get(field), str) or not citation[field]:
                            raise ValueError("metric citation lacks a display name")


def _build(context, inputs):
    source = _source_path(context.workspace, inputs["review_batch"])
    review = _review_payload(source)
    _validate_review(review)
    plugin = context.workspace / "plugins" / "review-website"
    target = context.workspace / ".zen" / "sites" / context.run_id
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    for name in ("index.html", "app.js", "styles.css"):
        shutil.copyfile(plugin / "assets" / name, target / name)
        os.chmod(target / name, 0o600)
    data_path = target / "review.json"
    data_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(data_path, 0o600)
    files = {
        name: _sha(target / name)
        for name in ("index.html", "app.js", "styles.css", "review.json")
    }
    manifest = {
        "schema_version": "zen.review-site/1",
        "site_run_id": context.run_id,
        "source": {
            "path": str(source.relative_to(context.workspace)),
            "sha256": _sha(source),
        },
        "security": {
            "contains_restricted_conversations": True,
            "default_bind": "127.0.0.1",
            "authentication": "bearer-or-cookie-token",
            "external_publication_requires_human_approval": True,
        },
        "counts": review.get("counts", {}),
        "files": files,
    }
    manifest_path = target / "site-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return {
        "schema_version": "zen.review-site-build/1",
        "site_path": str(target.relative_to(context.workspace)),
        "manifest_path": str(manifest_path.relative_to(context.workspace)),
        "manifest_sha256": _sha(manifest_path),
        "counts": review.get("counts", {}),
        "serve_command": f".venv/bin/python plugins/review-website/scripts/serve.py --site {target.relative_to(context.workspace)}",
    }


def _plan(objective, inputs, _max_attempts):
    review_batch = inputs.get("review_batch")
    if not isinstance(review_batch, str) or not review_batch:
        raise ValueError("review_batch is required")
    return Plan(
        "golden-review-website",
        objective,
        (
            TaskSpec(
                "build-site",
                "Validate verified candidates and build protected review website",
                "golden.build_review_website",
                {"review_batch": review_batch},
                max_attempts=1,
            ),
        ),
        "Build an owner-only website from a validated golden review batch; hosting remains local and authenticated by default.",
        {"review_batch": review_batch},
    )


def register(registry):
    registry.tools.register(
        ToolSpec(
            "golden.build_review_website",
            "0.1.0",
            "Build a protected website for verified golden-conversation candidates",
            ToolRisk.WORKSPACE_WRITE,
            {
                "type": "object",
                "required": ["review_batch"],
                "additionalProperties": False,
                "properties": {"review_batch": {"type": "string", "minLength": 1}},
            },
            {
                "type": "object",
                "required": ["schema_version", "site_path", "manifest_path", "manifest_sha256", "counts", "serve_command"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "string"},
                    "site_path": {"type": "string"},
                    "manifest_path": {"type": "string"},
                    "manifest_sha256": {"type": "string"},
                    "counts": {"type": "object"},
                    "serve_command": {"type": "string"},
                },
            },
            _build,
        )
    )
    registry.register_workflow(
        WorkflowSpec(
            "golden-review-website",
            "Build the authenticated local review website for verified generated conversations",
            ("build review website", "host verified conversations", "conversation website"),
            _plan,
        )
    )
