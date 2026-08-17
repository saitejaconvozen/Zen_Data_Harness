from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from .graph import GraphCatalog
from .tools import ToolRegistry


def load_graphs(plugin_paths: tuple[Path, ...], tools: ToolRegistry) -> GraphCatalog:
    catalog = GraphCatalog()
    for root in plugin_paths:
        for manifest_path in sorted(root.glob("*/plugin.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            module_path = (manifest_path.parent / manifest["entrypoint"]).resolve()
            spec = importlib.util.spec_from_file_location(
                f"zen_graph_plugin_{manifest['id'].replace('-', '_')}", module_path
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load graph plugin {manifest['id']}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register_graphs = getattr(module, "register_graphs", None)
            if callable(register_graphs):
                register_graphs(catalog, tools)
    return catalog
