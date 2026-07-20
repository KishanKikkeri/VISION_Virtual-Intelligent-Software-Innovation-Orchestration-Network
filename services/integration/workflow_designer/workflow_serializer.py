"""
services/integration/workflow_designer/workflow_serializer.py
=================================
M4.8 §5 Serializer (export half) — `WorkflowLayout` -> JSON/YAML text,
preserving every layout field (§5 "Preserve layout information") rather
than narrowing to the runtime-only shape `graph_builder.py` produces.
The runtime-only export path is `designer_export.py`'s job (it composes
`graph_builder.build_graph` + this narrower JSON, for a caller that wants
"just what LangGraph needs," e.g. a CI job diffing generated graphs) —
this module's `export_json`/`export_yaml` are always full round-trip-
capable (§6 "round-trip conversion" and §5's own "Preserve layout
information" both depend on nothing being dropped here).

YAML support uses stdlib-adjacent `yaml` (PyYAML) if importable; when it
is not installed in a given environment, `export_yaml`/`import_yaml`
(in `workflow_deserializer.py`) raise a clear `SerializerError` rather
than a bare `ImportError` or a silent JSON-in-YAML-clothing fallback —
same "readable error over hidden approximation" principle
`plugin_manifest.ManifestError` follows for its own optional-format
methods.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from services.integration.workflow_designer.designer_models import WorkflowLayout

try:
    import yaml as _yaml
except Exception:  # noqa: BLE001
    _yaml = None


class SerializerError(Exception):
    pass


def to_dict(layout: WorkflowLayout) -> Dict[str, Any]:
    """The canonical, format-agnostic dict both `export_json` and
    `export_yaml` serialize — one source of truth so JSON and YAML
    exports of the same layout are always structurally identical
    (differing only in text encoding), which is what §6's round-trip
    guarantee actually needs."""
    return layout.model_dump(mode="json")


def export_json(layout: WorkflowLayout, indent: int = 2) -> str:
    return json.dumps(to_dict(layout), indent=indent, sort_keys=False)


def export_yaml(layout: WorkflowLayout) -> str:
    if _yaml is None:
        raise SerializerError("PyYAML is not installed in this environment; install `pyyaml` to use YAML export")
    return _yaml.safe_dump(to_dict(layout), sort_keys=False, default_flow_style=False)


def export(layout: WorkflowLayout, fmt: str) -> str:
    """Dispatches by format name — the one function `designer_cli.py`/
    the export API route call, mirroring `plugin_manifest.load_manifest`'s
    own dispatch-by-name convention."""
    if fmt == "json":
        return export_json(layout)
    if fmt == "yaml":
        return export_yaml(layout)
    raise SerializerError(f"unknown serialization format {fmt!r}; choose one of ('json', 'yaml')")
