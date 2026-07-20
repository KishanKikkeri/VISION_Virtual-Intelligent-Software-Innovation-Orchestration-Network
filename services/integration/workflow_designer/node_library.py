"""
services/integration/workflow_designer/node_library.py
=================================
M4.8 §2 Node Library — the eleven builtin node templates named verbatim by
the brief (Agent/Decision/Conditional/Parallel(Send)/Join/Tool/Event/Human
Approval/Retry/Delay), plus plugin node discovery (§10, reusing M4.7's
`PluginRegistry`/`CapabilityInfo` rather than inventing a second plugin
catalog) and support for user-defined custom categories (§2 "Support custom
node categories").

**Plugin node discovery convention.** M4.7's `PluginManifest` has no
designer-specific field (a plugin author writing a manifest before M4.8
existed had no such field to fill in) — `plugin_node_templates` reads
plugin-declared capabilities the same way `plugin_registry.list_capabilities`
already exposes them (hooks + permissions) and additionally looks for an
optional, additive `manifest.dependencies`-sibling convention: a plugin
whose loaded entrypoint module exposes a module-level `DESIGNER_NODES`
list of dicts (`{"node_type": "plugin", "label": ..., "default_config":
{...}}`) contributes one `NodeTemplate` per entry. A plugin with no such
attribute contributes nothing — this is additive discovery, never a
required plugin-author contract (§10 "No hardcoded plugin behavior").
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.integration.workflow_designer.designer_models import (
    DesignerLibrary, DesignerPluginNodeAction, NodeTemplate, NodeType,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


BUILTIN_TEMPLATES: List[NodeTemplate] = [
    NodeTemplate(
        node_type=NodeType.AGENT, category="core", label="Agent", icon="bot",
        description="Invokes a LangGraph agent node.",
        default_config={"agent_name": "", "prompt_template": ""},
        property_schema={"agent_name": "string", "prompt_template": "string", "timeout_seconds": "number"},
    ),
    NodeTemplate(
        node_type=NodeType.DECISION, category="control_flow", label="Decision", icon="git-branch",
        description="Single-predicate branch: exactly one outgoing edge is taken.",
        default_config={"predicate": ""},
        property_schema={"predicate": "string"},
    ),
    NodeTemplate(
        node_type=NodeType.CONDITIONAL, category="control_flow", label="Conditional", icon="git-merge",
        description="Multi-branch conditional edge selector (LangGraph add_conditional_edges).",
        default_config={"branches": {}},
        property_schema={"branches": "object"},
    ),
    NodeTemplate(
        node_type=NodeType.PARALLEL, category="control_flow", label="Parallel (Send)", icon="split",
        description="Fans out to multiple nodes via LangGraph's Send primitive.",
        default_config={"targets": []},
        property_schema={"targets": "array"},
    ),
    NodeTemplate(
        node_type=NodeType.JOIN, category="control_flow", label="Join", icon="git-pull-request",
        description="Waits for all fanned-out branches before continuing.",
        default_config={"expected_branches": 0},
        property_schema={"expected_branches": "number"},
    ),
    NodeTemplate(
        node_type=NodeType.TOOL, category="core", label="Tool", icon="wrench",
        description="Invokes a registered tool function.",
        default_config={"tool_name": "", "arguments": {}},
        property_schema={"tool_name": "string", "arguments": "object"},
    ),
    NodeTemplate(
        node_type=NodeType.EVENT, category="integration", label="Event", icon="zap",
        description="Publishes or waits for a platform event.",
        default_config={"event_type": "", "mode": "publish"},
        property_schema={"event_type": "string", "mode": "string"},
    ),
    NodeTemplate(
        node_type=NodeType.HUMAN_APPROVAL, category="control_flow", label="Human Approval", icon="user-check",
        description="Pauses the workflow pending a human decision.",
        default_config={"approvers": [], "timeout_seconds": 3600},
        property_schema={"approvers": "array", "timeout_seconds": "number"},
    ),
    NodeTemplate(
        node_type=NodeType.RETRY, category="control_flow", label="Retry", icon="refresh-cw",
        description="Wraps a target node with retry semantics.",
        default_config={"target_node_id": "", "max_attempts": 3, "backoff_seconds": 1.0},
        property_schema={"target_node_id": "string", "max_attempts": "number", "backoff_seconds": "number"},
    ),
    NodeTemplate(
        node_type=NodeType.DELAY, category="control_flow", label="Delay", icon="clock",
        description="Pauses execution for a fixed or computed duration.",
        default_config={"seconds": 0},
        property_schema={"seconds": "number"},
    ),
]

_BUILTIN_BY_TYPE: Dict[NodeType, NodeTemplate] = {t.node_type: t for t in BUILTIN_TEMPLATES}


def get_builtin_template(node_type: NodeType) -> Optional[NodeTemplate]:
    return _BUILTIN_BY_TYPE.get(node_type)


def builtin_categories() -> List[str]:
    return sorted({t.category for t in BUILTIN_TEMPLATES})


def plugin_node_templates(loaded_modules: Optional[Dict[str, Any]] = None,
                           records: Optional[List[Any]] = None) -> List[NodeTemplate]:
    """§10 Plugin Integration — discovers `NodeTemplate`s from enabled
    plugins' loaded modules. `loaded_modules` maps plugin id -> already
    imported module (the same shape `plugin_runtime.dispatch_hook`
    consumes — see that module's docstring); `records` is the matching
    `PluginRecord` list (for `plugin_id`/permission context). Both are
    optional and default to empty: a platform with the Plugin SDK
    unavailable or no plugins installed simply contributes no plugin
    templates (§10's "gracefully handle missing optional integrations")."""
    if not loaded_modules:
        return []
    templates: List[NodeTemplate] = []
    for plugin_id, module in loaded_modules.items():
        entries = getattr(module, "DESIGNER_NODES", None)
        if not entries:
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            templates.append(NodeTemplate(
                node_type=NodeType.PLUGIN,
                category=entry.get("category", "plugin"),
                label=entry.get("label", entry.get("node_type", plugin_id)),
                description=entry.get("description", ""),
                icon=entry.get("icon", "puzzle"),
                default_config=entry.get("default_config", {}),
                property_schema=entry.get("property_schema", {}),
                plugin_id=plugin_id,
                source="plugin",
            ))
    return templates


def plugin_actions(loaded_modules: Optional[Dict[str, Any]] = None) -> List[DesignerPluginNodeAction]:
    """§10's remaining extension points — property editors, validation
    rules, toolbar actions, context menu actions — discovered the same
    additive way as `plugin_node_templates` via optional module-level
    `DESIGNER_PROPERTY_EDITORS` / `DESIGNER_VALIDATION_RULES` /
    `DESIGNER_TOOLBAR_ACTIONS` / `DESIGNER_CONTEXT_MENU_ACTIONS` lists of
    plain `{"key": ..., "label": ...}` dicts."""
    if not loaded_modules:
        return []
    kind_attrs = {
        "property_editor": "DESIGNER_PROPERTY_EDITORS",
        "validation_rule": "DESIGNER_VALIDATION_RULES",
        "toolbar_action": "DESIGNER_TOOLBAR_ACTIONS",
        "context_menu_action": "DESIGNER_CONTEXT_MENU_ACTIONS",
    }
    actions: List[DesignerPluginNodeAction] = []
    for plugin_id, module in loaded_modules.items():
        for kind, attr in kind_attrs.items():
            entries = getattr(module, attr, None)
            if not entries:
                continue
            for entry in entries:
                if not isinstance(entry, dict) or "key" not in entry:
                    continue
                actions.append(DesignerPluginNodeAction(
                    plugin_id=plugin_id, kind=kind, key=entry["key"], label=entry.get("label", entry["key"]),
                ))
    return actions


def custom_templates(definitions: Optional[List[Dict[str, Any]]] = None) -> List[NodeTemplate]:
    """§2 "Support custom node categories" — a caller (typically the API's
    `POST /designer/library/custom` or a project-local config file) may
    supply raw template dicts; this turns each into a `NodeTemplate` with
    `source="custom"`, skipping malformed entries rather than raising (a
    single bad custom-template definition should not break the whole
    library fetch)."""
    if not definitions:
        return []
    out: List[NodeTemplate] = []
    for d in definitions:
        try:
            out.append(NodeTemplate(
                node_type=NodeType.CUSTOM, category=d.get("category", "custom"), label=d["label"],
                description=d.get("description", ""), icon=d.get("icon", "shapes"),
                default_config=d.get("default_config", {}), property_schema=d.get("property_schema", {}),
                source="custom",
            ))
        except Exception:  # noqa: BLE001 — malformed custom entry, skip rather than fail the whole library
            continue
    return out


def build_library(loaded_modules: Optional[Dict[str, Any]] = None,
                   custom_definitions: Optional[List[Dict[str, Any]]] = None) -> DesignerLibrary:
    """§13 API's `GET /designer/library` payload — every builtin template
    plus any discoverable plugin/custom templates, merged into one
    response with a de-duplicated, sorted category list."""
    templates = list(BUILTIN_TEMPLATES) + plugin_node_templates(loaded_modules) + custom_templates(custom_definitions)
    categories = sorted({t.category for t in templates})
    return DesignerLibrary(
        templates=templates, categories=categories, plugin_actions=plugin_actions(loaded_modules),
        generated_at=_now_iso(),
    )
