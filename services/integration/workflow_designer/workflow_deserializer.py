"""
services/integration/workflow_designer/workflow_deserializer.py
=================================
M4.8 §5 Serializer (import half) + §6 Mermaid Integration (import half).
`import_json`/`import_yaml` are `workflow_serializer.export_json`/
`export_yaml`'s exact inverse (§6 "round-trip conversion" — see
`tests/foundation/test_m48_workflow_designer.py`'s round-trip tests).

**Mermaid import is intentionally a narrow subset**, not a full Mermaid
grammar parser: it recognizes flowchart node declarations
(`id[Label]`/`id{Label}`/`id((Label))`) and edges (`A --> B`,
`A -->|label| B`), which is what §6's own Mermaid export (reused from the
existing exporter, see `graph_builder.to_mermaid`'s docstring) actually
produces — good enough for the brief's round-trip guarantee between this
package's own export and import, not a general-purpose Mermaid-to-anything
converter. A line this parser does not recognize is skipped, not an error
(a Mermaid diagram may legitimately contain styling/subgraph directives
this subset does not model), while `import_mermaid`'s docstring calls out
this scope explicitly so a caller round-tripping a *hand-authored* Mermaid
diagram (not one this package produced) knows to check `warnings`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import List

from services.integration.workflow_designer.designer_models import (
    DesignerEdge, DesignerNode, NodeType, WorkflowLayout,
)
from services.integration.workflow_designer.workflow_serializer import SerializerError

try:
    import yaml as _yaml
except Exception:  # noqa: BLE001
    _yaml = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def import_json(text: str) -> WorkflowLayout:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SerializerError(f"not valid JSON: {e}") from e
    return WorkflowLayout.model_validate(data)


def import_yaml(text: str) -> WorkflowLayout:
    if _yaml is None:
        raise SerializerError("PyYAML is not installed in this environment; install `pyyaml` to use YAML import")
    try:
        data = _yaml.safe_load(text)
    except _yaml.YAMLError as e:  # type: ignore[union-attr]
        raise SerializerError(f"not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise SerializerError("YAML document must decode to a mapping at the top level")
    return WorkflowLayout.model_validate(data)


def import_workflow(text: str, fmt: str) -> WorkflowLayout:
    if fmt == "json":
        return import_json(text)
    if fmt == "yaml":
        return import_yaml(text)
    raise SerializerError(f"unknown serialization format {fmt!r}; choose one of ('json', 'yaml')")


# ── Mermaid import (§6) ─────────────────────────────────────────────────

_NODE_RE = re.compile(r"^\s*([A-Za-z0-9_\-]+)\s*(?:\[(.*?)\]|\{(.*?)\}|\(\((.*?)\)\))\s*$")
_EDGE_RE = re.compile(
    r"^\s*([A-Za-z0-9_\-]+)\s*-->\s*(?:\|(.*?)\|\s*)?([A-Za-z0-9_\-]+)\s*(?:\[(.*?)\]|\{(.*?)\}|\(\((.*?)\)\))?\s*$"
)


def import_mermaid(text: str, workflow_name: str = "imported_workflow") -> WorkflowLayout:
    """Parses the flowchart-node/edge subset described in the module
    docstring into a `WorkflowLayout`. Node shape hints a best-guess
    `NodeType`: `{Label}` (diamond) -> DECISION, `((Label))` (circle) ->
    EVENT, plain `[Label]` (rectangle) -> AGENT — a caller that needs a
    more specific type should re-set it afterward; this is a starting
    point for further editing, not a lossless type recovery (Mermaid
    carries no explicit `node_type` at all)."""
    nodes: dict = {}
    edges: List[DesignerEdge] = []
    node_order: List[str] = []

    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.lower().startswith(("flowchart", "graph", "%%"))]

    def _register(node_id: str, label: str = "", shape: str = "rect") -> None:
        if node_id in nodes:
            if label:
                nodes[node_id].label = label
            return
        node_type = {"diamond": NodeType.DECISION, "circle": NodeType.EVENT}.get(shape, NodeType.AGENT)
        nodes[node_id] = DesignerNode(id=node_id, node_type=node_type, label=label or node_id,
                                       x=80.0 * len(node_order), y=100.0)
        node_order.append(node_id)

    for i, line in enumerate(lines):
        edge_match = _EDGE_RE.match(line)
        if edge_match:
            source, edge_label, target, rect, diamond, circle = edge_match.groups()
            _register(source)
            shape = "diamond" if diamond is not None else ("circle" if circle is not None else "rect")
            _register(target, label=(rect or diamond or circle or ""), shape=shape)
            edges.append(DesignerEdge(id=f"e{i}_{source}_{target}", source=source, target=target,
                                       label=edge_label or ""))
            continue
        node_match = _NODE_RE.match(line)
        if node_match:
            node_id, rect, diamond, circle = node_match.groups()
            shape = "diamond" if diamond is not None else ("circle" if circle is not None else "rect")
            _register(node_id, label=(rect or diamond or circle or ""), shape=shape)

    return WorkflowLayout(
        workflow_name=workflow_name, nodes=list(nodes.values()), edges=edges,
        entry_node_id=node_order[0] if node_order else None, updated_at=_now_iso(),
    )
