#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from zen_agent.domains.golden_taxonomy import compile_taxonomy


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile the governed Zen metrics CSV")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    taxonomy = compile_taxonomy(args.source)
    manifest = taxonomy.to_manifest()
    manifest["taxonomy_id"] = "zen-eval-axes"
    manifest["taxonomy_version"] = "2026-q2-v1"
    manifest["source"]["path"] = args.source.name
    manifest["governance"] = {
        "axis_subaxis_variant_are_independent_fields": True,
        "parent_path_validation_required": True,
        "disabled_nodes_may_not_be_annotated": True,
        "changes_require_new_version_and_checksum": True,
    }
    payload = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ).encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{args.output.name}.", dir=args.output.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.output)
        os.chmod(args.output, 0o644)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "source_sha256": taxonomy.source_checksum,
                "axes": taxonomy.axis_count,
                "subaxes": taxonomy.subaxis_count,
                "variants": taxonomy.variant_count,
                "warnings": len(taxonomy.warnings),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
