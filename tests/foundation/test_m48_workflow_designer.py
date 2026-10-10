"""
tests/foundation/test_m48_workflow_designer.py
=================================
M4.8 Visual Workflow Designer tests.

Layer 1  — Models:             designer_models construction/defaults, enum coverage.
Layer 2  — Node Library:       builtin templates, plugin discovery, custom templates, build_library.
Layer 3  — Canvas State:       add/move/delete/undo/redo/selection/clipboard/viewport/autosave.
Layer 4  — Serializer:         JSON/YAML export, dict shape, format dispatch, missing-PyYAML path.
Layer 5  — Deserializer:       JSON/YAML import, round-trip, Mermaid import subset + shape hints.
Layer 6  — Graph Builder:      canvas -> runtime shape, dangling edges, entry point handling,
                                structural equality, Mermaid export (fallback renderer).
Layer 7  — Validation Bridge:  local structural checks, external-source graceful degradation,
                                layout diff, version history / replay overlay unavailability.
Layer 8  — Repository:         designer_repository against a real in-memory SQLite async engine.
Layer 9  — Export:             json/yaml/mermaid/markdown dispatch + fallback markdown content.
Layer 10 — Dashboard:          fetch_designer_dashboard_section, build_designer_summary,
                                PlatformDashboard.designer wiring.
Layer 11 — API:                every /designer* route, in-memory + DB-backed.
Layer 12 — CLI:                designer_cli.main against fixture files.
Layer 13 — Regression/isolation.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from services.integration.workflow_designer import (
    canvas_state, designer_export, graph_builder, node_library, validation_bridge,
    workflow_deserializer, workflow_serializer,
)
from services.integration.workflow_designer.designer_models import (
    CanvasSelection, CanvasState, ClipboardPayload, DesignerEdge, DesignerGroup, DesignerLibrary,
    DesignerNode, DesignerPluginNodeAction, DesignerSession, ExportFormat, GraphBuildResult, LayoutDiffResult,
    NodeTemplate, NodeType, ReplayNodeState, ReplayOverlay, ValidationBridgeIssue, ValidationBridgeResult,
    VersionDiffEntry, WorkflowLayout,
)
from services.integration.workflow_designer.workflow_serializer import SerializerError


# ══════════════════════════════════════════════════════════════
# Fixtures / helpers
# ══════════════════════════════════════════════════════════════

def _simple_layout(workflow_name: str = "wf1") -> WorkflowLayout:
    return WorkflowLayout(
        workflow_name=workflow_name,
        nodes=[
            DesignerNode(id="a", node_type=NodeType.AGENT, label="Agent A", x=0, y=0),
            DesignerNode(id="b", node_type=NodeType.TOOL, label="Tool B", x=150, y=0),
            DesignerNode(id="c", node_type=NodeType.DECISION, label="Decide", x=300, y=0),
        ],
        edges=[
            DesignerEdge(id="e1", source="a", target="b"),
            DesignerEdge(id="e2", source="b", target="c", condition="result == True"),
        ],
        entry_node_id="a",
    )


# ══════════════════════════════════════════════════════════════
# LAYER 1 — Models
# ══════════════════════════════════════════════════════════════

def test_node_type_enum_covers_brief_node_catalog():
    expected = {"agent", "decision", "conditional", "parallel", "join", "tool", "event",
                "human_approval", "retry", "delay", "plugin", "custom"}
    assert {t.value for t in NodeType} == expected


def test_export_format_enum():
    assert {f.value for f in ExportFormat} == {"json", "yaml", "mermaid", "markdown"}


def test_designer_node_defaults():
    node = DesignerNode(id="n1", node_type=NodeType.AGENT)
    assert node.label == ""
    assert node.x == 0.0 and node.y == 0.0
    assert node.config == {}
    assert node.plugin_id is None


def test_designer_edge_defaults():
    edge = DesignerEdge(id="e1", source="a", target="b")
    assert edge.label == ""
    assert edge.condition is None


def test_designer_group_defaults():
    group = DesignerGroup(id="g1")
    assert group.node_ids == []
    assert group.label == ""


def test_workflow_layout_defaults():
    layout = WorkflowLayout(workflow_name="wf")
    assert layout.version == "0.0.1"
    assert layout.nodes == []
    assert layout.viewport.zoom == 1.0
    assert layout.entry_node_id is None


def test_canvas_state_defaults():
    layout = _simple_layout()
    cs = CanvasState(layout=layout)
    assert cs.history == []
    assert cs.future == []
    assert cs.dirty is False
    assert cs.clipboard is None


def test_node_template_defaults():
    tmpl = NodeTemplate(node_type=NodeType.AGENT, category="core", label="Agent")
    assert tmpl.source == "builtin"
    assert tmpl.default_config == {}


def test_validation_bridge_result_errors_warnings_properties():
    result = ValidationBridgeResult(workflow_name="wf", issues=[
        ValidationBridgeIssue(rule_id="R1", severity="error", message="bad"),
        ValidationBridgeIssue(rule_id="R2", severity="warning", message="meh"),
    ])
    assert len(result.errors) == 1
    assert len(result.warnings) == 1


def test_layout_diff_result_is_breaking():
    diff_breaking = LayoutDiffResult(workflow_name="wf", from_version="1", to_version="2",
                                      entries=[VersionDiffEntry(kind="node_removed", node_id="x")])
    diff_safe = LayoutDiffResult(workflow_name="wf", from_version="1", to_version="2",
                                  entries=[VersionDiffEntry(kind="node_added", node_id="y")])
    assert diff_breaking.is_breaking is True
    assert diff_safe.is_breaking is False


def test_replay_overlay_defaults():
    overlay = ReplayOverlay(workflow_name="wf", execution_id="exec1")
    assert overlay.available is True
    assert overlay.node_states == []


def test_designer_session_and_snapshot_construct():
    sess = DesignerSession(id="s1", workflow_name="wf")
    assert sess.dirty is False
    assert sess.replay_mode is False


def test_designer_library_construct():
    lib = DesignerLibrary()
    assert lib.templates == []
    assert lib.categories == []


def test_designer_plugin_node_action_label_default_is_empty_string():
    action = DesignerPluginNodeAction(plugin_id="p1", kind="toolbar_action", key="run")
    assert action.label == ""


def test_workflow_layout_round_trip_model_dump():
    layout = _simple_layout()
    data = layout.model_dump(mode="json")
    restored = WorkflowLayout.model_validate(data)
    assert restored == layout


# ══════════════════════════════════════════════════════════════
# LAYER 2 — Node Library
# ══════════════════════════════════════════════════════════════

def test_builtin_templates_cover_all_brief_node_types():
    covered = {t.node_type for t in node_library.BUILTIN_TEMPLATES}
    expected = {NodeType.AGENT, NodeType.DECISION, NodeType.CONDITIONAL, NodeType.PARALLEL, NodeType.JOIN,
                NodeType.TOOL, NodeType.EVENT, NodeType.HUMAN_APPROVAL, NodeType.RETRY, NodeType.DELAY}
    assert covered == expected


def test_get_builtin_template_found_and_missing():
    assert node_library.get_builtin_template(NodeType.AGENT) is not None
    assert node_library.get_builtin_template(NodeType.PLUGIN) is None


def test_builtin_categories_sorted():
    cats = node_library.builtin_categories()
    assert cats == sorted(cats)
    assert "core" in cats


def test_plugin_node_templates_empty_when_no_modules():
    assert node_library.plugin_node_templates(None) == []
    assert node_library.plugin_node_templates({}) == []


class _FakeModule:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_plugin_node_templates_discovers_designer_nodes_attribute():
    module = _FakeModule(DESIGNER_NODES=[
        {"label": "Custom Fetch", "category": "plugin", "default_config": {"url": ""}},
    ])
    templates = node_library.plugin_node_templates({"my-plugin": module})
    assert len(templates) == 1
    assert templates[0].plugin_id == "my-plugin"
    assert templates[0].node_type == NodeType.PLUGIN
    assert templates[0].source == "plugin"


def test_plugin_node_templates_skips_non_dict_entries():
    module = _FakeModule(DESIGNER_NODES=["not-a-dict", 123, {"label": "OK"}])
    templates = node_library.plugin_node_templates({"p": module})
    assert len(templates) == 1
    assert templates[0].label == "OK"


def test_plugin_node_templates_skips_module_without_attribute():
    module = _FakeModule()
    assert node_library.plugin_node_templates({"p": module}) == []


def test_plugin_actions_discovers_all_kinds():
    module = _FakeModule(
        DESIGNER_PROPERTY_EDITORS=[{"key": "pe1"}],
        DESIGNER_VALIDATION_RULES=[{"key": "vr1", "label": "Rule 1"}],
        DESIGNER_TOOLBAR_ACTIONS=[{"key": "ta1"}],
        DESIGNER_CONTEXT_MENU_ACTIONS=[{"key": "cma1"}],
    )
    actions = node_library.plugin_actions({"p": module})
    kinds = {a.kind for a in actions}
    assert kinds == {"property_editor", "validation_rule", "toolbar_action", "context_menu_action"}
    assert len(actions) == 4


def test_plugin_actions_skips_entries_without_key():
    module = _FakeModule(DESIGNER_TOOLBAR_ACTIONS=[{"label": "no key here"}])
    assert node_library.plugin_actions({"p": module}) == []


def test_plugin_actions_empty_when_no_modules():
    assert node_library.plugin_actions(None) == []


def test_custom_templates_builds_valid_entries():
    defs = [{"label": "Custom A", "category": "custom", "default_config": {"x": 1}}]
    templates = node_library.custom_templates(defs)
    assert len(templates) == 1
    assert templates[0].node_type == NodeType.CUSTOM
    assert templates[0].source == "custom"


def test_custom_templates_skips_malformed_entries():
    defs = [{"no_label_field": True}, {"label": "Valid"}]
    templates = node_library.custom_templates(defs)
    assert len(templates) == 1
    assert templates[0].label == "Valid"


def test_custom_templates_empty_when_none():
    assert node_library.custom_templates(None) == []


def test_build_library_merges_builtin_plugin_custom():
    module = _FakeModule(DESIGNER_NODES=[{"label": "Plugin Node"}])
    lib = node_library.build_library(
        loaded_modules={"p": module},
        custom_definitions=[{"label": "Custom Node"}],
    )
    sources = {t.source for t in lib.templates}
    assert sources == {"builtin", "plugin", "custom"}
    assert len(lib.templates) == len(node_library.BUILTIN_TEMPLATES) + 2
    assert lib.generated_at != ""


def test_build_library_categories_sorted_and_deduplicated():
    lib = node_library.build_library()
    assert lib.categories == sorted(set(lib.categories))


# ══════════════════════════════════════════════════════════════
# LAYER 3 — Canvas State
# ══════════════════════════════════════════════════════════════

def test_new_canvas_state_wraps_layout():
    cs = canvas_state.new_canvas_state(_simple_layout())
    assert cs.layout.workflow_name == "wf1"
    assert cs.dirty is False


def test_add_node_pushes_history_and_appends():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs2 = canvas_state.add_node(cs, DesignerNode(id="d", node_type=NodeType.DELAY, x=10, y=10))
    assert len(cs2.layout.nodes) == 4
    assert cs2.dirty is True
    assert len(cs2.history) == 1


def test_add_node_replaces_existing_id():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs2 = canvas_state.add_node(cs, DesignerNode(id="a", node_type=NodeType.EVENT, label="Replaced"))
    assert len([n for n in cs2.layout.nodes if n.id == "a"]) == 1
    assert next(n for n in cs2.layout.nodes if n.id == "a").label == "Replaced"


def test_move_node_updates_coordinates():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs2 = canvas_state.move_node(cs, "a", 99, 88)
    moved = next(n for n in cs2.layout.nodes if n.id == "a")
    assert (moved.x, moved.y) == (99, 88)


def test_update_node_config_merges():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.update_node_config(cs, "a", {"agent_name": "x"})
    cs = canvas_state.update_node_config(cs, "a", {"prompt_template": "y"})
    node = next(n for n in cs.layout.nodes if n.id == "a")
    assert node.config == {"agent_name": "x", "prompt_template": "y"}


def test_add_edge_appends():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs2 = canvas_state.add_edge(cs, DesignerEdge(id="e3", source="a", target="c"))
    assert len(cs2.layout.edges) == 3


def test_set_selection_does_not_push_history():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs2 = canvas_state.set_selection(cs, node_ids=["a"])
    assert cs2.selection.node_ids == ["a"]
    assert cs2.history == []
    assert cs2.dirty is False


def test_delete_selection_removes_nodes_and_dangling_edges():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.set_selection(cs, node_ids=["b"])
    cs = canvas_state.delete_selection(cs)
    assert all(n.id != "b" for n in cs.layout.nodes)
    assert all(e.source != "b" and e.target != "b" for e in cs.layout.edges)
    assert cs.selection.node_ids == []


def test_delete_selection_clears_entry_node_if_deleted():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.set_selection(cs, node_ids=["a"])
    cs = canvas_state.delete_selection(cs)
    assert cs.layout.entry_node_id is None


def test_delete_selection_removes_from_groups():
    layout = _simple_layout()
    layout.groups = [DesignerGroup(id="g1", node_ids=["a", "b"])]
    cs = canvas_state.new_canvas_state(layout)
    cs = canvas_state.set_selection(cs, node_ids=["a"])
    cs = canvas_state.delete_selection(cs)
    assert cs.layout.groups[0].node_ids == ["b"]


def test_undo_reverts_last_change():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.add_node(cs, DesignerNode(id="d", node_type=NodeType.DELAY))
    assert len(cs.layout.nodes) == 4
    cs = canvas_state.undo(cs)
    assert len(cs.layout.nodes) == 3


def test_undo_on_empty_history_is_noop():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs2 = canvas_state.undo(cs)
    assert cs2 == cs


def test_redo_reapplies_undone_change():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.add_node(cs, DesignerNode(id="d", node_type=NodeType.DELAY))
    cs = canvas_state.undo(cs)
    cs = canvas_state.redo(cs)
    assert len(cs.layout.nodes) == 4


def test_redo_on_empty_future_is_noop():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs2 = canvas_state.redo(cs)
    assert cs2 == cs


def test_new_edit_after_undo_clears_future():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.add_node(cs, DesignerNode(id="d", node_type=NodeType.DELAY))
    cs = canvas_state.undo(cs)
    assert len(cs.future) == 1
    cs = canvas_state.add_node(cs, DesignerNode(id="e", node_type=NodeType.DELAY))
    assert cs.future == []


def test_history_bounded_by_max_history():
    cs = canvas_state.new_canvas_state(_simple_layout())
    for i in range(canvas_state.MAX_HISTORY + 20):
        cs = canvas_state.add_node(cs, DesignerNode(id=f"n{i}", node_type=NodeType.DELAY))
    assert len(cs.history) == canvas_state.MAX_HISTORY


def test_copy_selection_only_includes_internal_edges():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.set_selection(cs, node_ids=["a", "b"])
    cs = canvas_state.copy_selection(cs)
    assert cs.clipboard is not None
    assert len(cs.clipboard.nodes) == 2
    assert len(cs.clipboard.edges) == 1  # e1: a->b; e2 excluded (c not selected)


def test_cut_selection_copies_then_deletes():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.set_selection(cs, node_ids=["c"])
    cs = canvas_state.cut_selection(cs)
    assert cs.clipboard is not None
    assert len(cs.clipboard.nodes) == 1
    assert all(n.id != "c" for n in cs.layout.nodes)


def test_paste_with_empty_clipboard_is_noop():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs2 = canvas_state.paste(cs)
    assert cs2 == cs


def test_paste_remaps_ids_and_offsets_position():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.set_selection(cs, node_ids=["a"])
    cs = canvas_state.copy_selection(cs)
    cs = canvas_state.paste(cs, offset_x=10, offset_y=20)
    pasted = [n for n in cs.layout.nodes if n.id == "a_copy"]
    assert len(pasted) == 1
    assert (pasted[0].x, pasted[0].y) == (10, 20)
    assert cs.selection.node_ids == ["a_copy"]


def test_paste_twice_avoids_id_collision():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.set_selection(cs, node_ids=["a"])
    cs = canvas_state.copy_selection(cs)
    cs = canvas_state.paste(cs)
    cs = canvas_state.paste(cs)
    ids = [n.id for n in cs.layout.nodes]
    assert len(ids) == len(set(ids))  # all unique


def test_paste_preserves_internal_edges_with_remapped_endpoints():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.set_selection(cs, node_ids=["a", "b"])
    cs = canvas_state.copy_selection(cs)
    cs = canvas_state.paste(cs)
    new_edge = next(e for e in cs.layout.edges if e.id.endswith("_copy") and e.source == "a_copy")
    assert new_edge.target == "b_copy"


def test_set_viewport_does_not_push_history():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs2 = canvas_state.set_viewport(cs, zoom=2.0, pan_x=5, pan_y=6)
    assert cs2.layout.viewport.zoom == 2.0
    assert cs2.history == []


def test_mark_autosaved_clears_dirty():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.add_node(cs, DesignerNode(id="d", node_type=NodeType.DELAY))
    assert cs.dirty is True
    cs = canvas_state.mark_autosaved(cs)
    assert cs.dirty is False
    assert cs.last_autosaved_at is not None


def test_needs_autosave_false_when_not_dirty():
    cs = canvas_state.new_canvas_state(_simple_layout())
    assert canvas_state.needs_autosave(cs) is False


def test_needs_autosave_true_when_dirty_never_saved():
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.add_node(cs, DesignerNode(id="d", node_type=NodeType.DELAY))
    assert canvas_state.needs_autosave(cs) is True


def test_needs_autosave_respects_interval():
    from datetime import datetime, timedelta, timezone
    cs = canvas_state.new_canvas_state(_simple_layout())
    cs = canvas_state.add_node(cs, DesignerNode(id="d", node_type=NodeType.DELAY))
    cs = canvas_state.mark_autosaved(cs)
    cs = canvas_state.add_node(cs, DesignerNode(id="e", node_type=NodeType.DELAY))
    now_soon = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert canvas_state.needs_autosave(cs, interval_seconds=30, now=now_soon) is False
    now_later = datetime.now(timezone.utc) + timedelta(seconds=31)
    assert canvas_state.needs_autosave(cs, interval_seconds=30, now=now_later) is True


# ══════════════════════════════════════════════════════════════
# LAYER 4 — Serializer
# ══════════════════════════════════════════════════════════════

def test_to_dict_is_format_agnostic_source_of_truth():
    layout = _simple_layout()
    d = workflow_serializer.to_dict(layout)
    assert d["workflow_name"] == "wf1"
    assert len(d["nodes"]) == 3


def test_export_json_produces_valid_json():
    layout = _simple_layout()
    text = workflow_serializer.export_json(layout)
    parsed = json.loads(text)
    assert parsed["workflow_name"] == "wf1"


def test_export_yaml_produces_valid_yaml():
    layout = _simple_layout()
    text = workflow_serializer.export_yaml(layout)
    import yaml
    parsed = yaml.safe_load(text)
    assert parsed["workflow_name"] == "wf1"


def test_export_dispatch_json_and_yaml():
    layout = _simple_layout()
    assert workflow_serializer.export(layout, "json") == workflow_serializer.export_json(layout)
    assert workflow_serializer.export(layout, "yaml") == workflow_serializer.export_yaml(layout)


def test_export_dispatch_unknown_format_raises():
    with pytest.raises(SerializerError):
        workflow_serializer.export(_simple_layout(), "xml")


def test_export_yaml_raises_clear_error_when_pyyaml_missing(monkeypatch):
    monkeypatch.setattr(workflow_serializer, "_yaml", None)
    with pytest.raises(SerializerError, match="PyYAML"):
        workflow_serializer.export_yaml(_simple_layout())


# ══════════════════════════════════════════════════════════════
# LAYER 5 — Deserializer
# ══════════════════════════════════════════════════════════════

def test_import_json_round_trips_export_json():
    layout = _simple_layout()
    text = workflow_serializer.export_json(layout)
    restored = workflow_deserializer.import_json(text)
    assert restored == layout


def test_import_json_invalid_json_raises():
    with pytest.raises(SerializerError, match="not valid JSON"):
        workflow_deserializer.import_json("{not json")


def test_import_yaml_round_trips_export_yaml():
    layout = _simple_layout()
    text = workflow_serializer.export_yaml(layout)
    restored = workflow_deserializer.import_yaml(text)
    assert restored == layout


def test_import_yaml_invalid_yaml_raises():
    with pytest.raises(SerializerError, match="not valid YAML"):
        workflow_deserializer.import_yaml(":\n  - bad: [unclosed")


def test_import_yaml_non_mapping_top_level_raises():
    with pytest.raises(SerializerError, match="mapping"):
        workflow_deserializer.import_yaml("- 1\n- 2\n")


def test_import_yaml_raises_clear_error_when_pyyaml_missing(monkeypatch):
    monkeypatch.setattr(workflow_deserializer, "_yaml", None)
    with pytest.raises(SerializerError, match="PyYAML"):
        workflow_deserializer.import_yaml("workflow_name: wf")


def test_import_workflow_dispatch():
    layout = _simple_layout()
    text = workflow_serializer.export_json(layout)
    assert workflow_deserializer.import_workflow(text, "json") == layout


def test_import_workflow_unknown_format_raises():
    with pytest.raises(SerializerError):
        workflow_deserializer.import_workflow("{}", "xml")


def test_import_mermaid_parses_nodes_and_edges():
    mermaid = 'flowchart TD\n    a["Start"]\n    b["End"]\n    a --> b\n'
    layout = workflow_deserializer.import_mermaid(mermaid, workflow_name="from_mermaid")
    assert {n.id for n in layout.nodes} == {"a", "b"}
    assert layout.edges[0].source == "a" and layout.edges[0].target == "b"
    assert layout.entry_node_id == "a"


def test_import_mermaid_edge_with_label():
    mermaid = 'flowchart TD\n    a --> |yes| b\n'
    layout = workflow_deserializer.import_mermaid(mermaid)
    assert layout.edges[0].label == "yes"


def test_import_mermaid_diamond_shape_hints_decision():
    mermaid = 'flowchart TD\n    a{"Is valid?"}\n'
    layout = workflow_deserializer.import_mermaid(mermaid)
    assert layout.nodes[0].node_type == NodeType.DECISION


def test_import_mermaid_circle_shape_hints_event():
    mermaid = 'flowchart TD\n    a(("Trigger"))\n'
    layout = workflow_deserializer.import_mermaid(mermaid)
    assert layout.nodes[0].node_type == NodeType.EVENT


def test_import_mermaid_skips_unrecognized_lines():
    mermaid = 'flowchart TD\n    style a fill:#fff\n    a["Node"]\n'
    layout = workflow_deserializer.import_mermaid(mermaid)
    assert len(layout.nodes) == 1


def test_import_mermaid_empty_text_produces_empty_layout():
    layout = workflow_deserializer.import_mermaid("flowchart TD\n")
    assert layout.nodes == []
    assert layout.entry_node_id is None


def test_json_yaml_round_trip_produce_structurally_identical_layouts():
    layout = _simple_layout()
    via_json = workflow_deserializer.import_json(workflow_serializer.export_json(layout))
    via_yaml = workflow_deserializer.import_yaml(workflow_serializer.export_yaml(layout))
    assert via_json == via_yaml == layout


# ══════════════════════════════════════════════════════════════
# LAYER 6 — Graph Builder
# ══════════════════════════════════════════════════════════════

def test_build_graph_maps_nodes_and_edges():
    result = graph_builder.build_graph(_simple_layout())
    assert {n["id"] for n in result.nodes} == {"a", "b", "c"}
    assert len(result.edges) == 2
    assert result.entry_point == "a"
    assert result.warnings == []


def test_build_graph_never_includes_designer_only_fields():
    result = graph_builder.build_graph(_simple_layout())
    for node in result.nodes:
        assert "x" not in node and "y" not in node and "color" not in node and "comment" not in node


def test_build_graph_drops_dangling_source_edge_with_warning():
    layout = WorkflowLayout(workflow_name="wf", nodes=[DesignerNode(id="b", node_type=NodeType.TOOL)],
                             edges=[DesignerEdge(id="e1", source="missing", target="b")])
    result = graph_builder.build_graph(layout)
    assert result.edges == []
    assert any("missing" in w for w in result.warnings)


def test_build_graph_drops_dangling_target_edge_with_warning():
    layout = WorkflowLayout(workflow_name="wf", nodes=[DesignerNode(id="a", node_type=NodeType.AGENT)],
                             edges=[DesignerEdge(id="e1", source="a", target="missing")])
    result = graph_builder.build_graph(layout)
    assert result.edges == []
    assert any("missing" in w for w in result.warnings)


def test_build_graph_invalid_entry_point_dropped_with_warning():
    layout = WorkflowLayout(workflow_name="wf", nodes=[DesignerNode(id="a", node_type=NodeType.AGENT)],
                             entry_node_id="nonexistent")
    result = graph_builder.build_graph(layout)
    assert result.entry_point is None
    assert any("nonexistent" in w for w in result.warnings)


def test_build_graph_missing_entry_point_warns_when_nodes_exist():
    layout = WorkflowLayout(workflow_name="wf", nodes=[DesignerNode(id="a", node_type=NodeType.AGENT)])
    result = graph_builder.build_graph(layout)
    assert any("no entry_node_id" in w for w in result.warnings)


def test_build_graph_empty_layout_no_warnings_about_entry_point():
    layout = WorkflowLayout(workflow_name="wf")
    result = graph_builder.build_graph(layout)
    assert result.warnings == []


def test_build_graph_preserves_condition_on_edges():
    result = graph_builder.build_graph(_simple_layout())
    conditional = next(e for e in result.edges if e["id"] == "e2")
    assert conditional["condition"] == "result == True"


def test_diff_structure_identical_layouts_are_equal():
    layout = _simple_layout()
    a = graph_builder.build_graph(layout)
    b = graph_builder.build_graph(layout.model_copy(deep=True))
    assert graph_builder.diff_structure(a, b) is True


def test_diff_structure_differs_when_node_added():
    layout = _simple_layout()
    a = graph_builder.build_graph(layout)
    layout2 = layout.model_copy(deep=True)
    layout2.nodes.append(DesignerNode(id="z", node_type=NodeType.DELAY))
    b = graph_builder.build_graph(layout2)
    assert graph_builder.diff_structure(a, b) is False


def test_diff_structure_ignores_layout_only_changes():
    layout = _simple_layout()
    a = graph_builder.build_graph(layout)
    layout2 = layout.model_copy(deep=True)
    for n in layout2.nodes:
        n.x += 500
        n.color = "#ff0000"
    b = graph_builder.build_graph(layout2)
    assert graph_builder.diff_structure(a, b) is True


def test_to_mermaid_fallback_renders_flowchart():
    text = graph_builder.to_mermaid(_simple_layout())
    assert text.startswith("flowchart TD")
    assert "a" in text and "b" in text and "c" in text


def test_to_mermaid_marks_entry_node():
    text = graph_builder.to_mermaid(_simple_layout())
    assert "entry" in text


def test_to_mermaid_renders_decision_as_diamond_shape():
    text = graph_builder.to_mermaid(_simple_layout())
    assert "{" in text  # node "c" is a DECISION


def test_to_mermaid_uses_real_exporter_when_importable(monkeypatch):
    import sys
    import types

    fake_module = types.ModuleType("services.integration.mermaid_exporter")
    fake_module.export_mermaid = lambda graph: "REAL EXPORTER OUTPUT"
    monkeypatch.setitem(sys.modules, "services.integration.mermaid_exporter", fake_module)
    assert graph_builder.to_mermaid(_simple_layout()) == "REAL EXPORTER OUTPUT"
    del sys.modules["services.integration.mermaid_exporter"]


# ══════════════════════════════════════════════════════════════
# LAYER 7 — Validation Bridge
# ══════════════════════════════════════════════════════════════

def test_validate_layout_valid_simple_layout():
    result = validation_bridge.validate_layout(_simple_layout())
    assert result.valid is True
    assert result.sources_available == {"workflow_validator": False, "graph_linter": False}


def test_validate_layout_flags_duplicate_node_ids():
    layout = WorkflowLayout(workflow_name="wf", nodes=[
        DesignerNode(id="a", node_type=NodeType.AGENT), DesignerNode(id="a", node_type=NodeType.TOOL),
    ])
    result = validation_bridge.validate_layout(layout)
    assert result.valid is False
    assert any(i.rule_id == "DESIGNER-DUPLICATE-NODE-ID" for i in result.issues)


def test_validate_layout_flags_empty_workflow_as_warning_only():
    layout = WorkflowLayout(workflow_name="wf")
    result = validation_bridge.validate_layout(layout)
    assert result.valid is True  # warnings don't invalidate
    assert any(i.rule_id == "DESIGNER-EMPTY-WORKFLOW" for i in result.issues)


def test_validate_layout_surfaces_graph_builder_warnings():
    layout = WorkflowLayout(workflow_name="wf", nodes=[DesignerNode(id="a", node_type=NodeType.AGENT)],
                             edges=[DesignerEdge(id="e1", source="a", target="missing")])
    result = validation_bridge.validate_layout(layout)
    assert any(i.rule_id == "DESIGNER-GRAPH-WARNING" for i in result.issues)


def test_validate_layout_uses_real_workflow_validator_when_importable(monkeypatch):
    import sys
    import types

    class FakeIssue:
        rule_id = "WV-001"
        severity = "error"
        message = "external failure"

    fake_module = types.ModuleType("services.workflow.workflow_validator")
    fake_module.validate_graph = lambda graph: [FakeIssue()]
    monkeypatch.setitem(sys.modules, "services.workflow.workflow_validator", fake_module)

    result = validation_bridge.validate_layout(_simple_layout())
    assert result.sources_available["workflow_validator"] is True
    assert result.valid is False
    assert any(i.source == "workflow_validator" and i.rule_id == "WV-001" for i in result.issues)
    del sys.modules["services.workflow.workflow_validator"]


def test_validate_layout_handles_external_validator_raising(monkeypatch):
    import sys
    import types

    def _raise(graph):
        raise RuntimeError("boom")

    fake_module = types.ModuleType("services.workflow.workflow_validator")
    fake_module.validate_graph = _raise
    monkeypatch.setitem(sys.modules, "services.workflow.workflow_validator", fake_module)

    result = validation_bridge.validate_layout(_simple_layout())
    assert any(i.rule_id == "WORKFLOW-VALIDATOR-ERROR" for i in result.issues)
    del sys.modules["services.workflow.workflow_validator"]


def test_diff_layouts_detects_node_added_and_removed():
    old = _simple_layout()
    new = old.model_copy(deep=True)
    new.nodes = [n for n in new.nodes if n.id != "c"]
    new.nodes.append(DesignerNode(id="z", node_type=NodeType.DELAY))
    result = validation_bridge.diff_layouts(old, new)
    kinds = {e.kind for e in result.entries}
    assert "node_added" in kinds and "node_removed" in kinds
    assert result.is_breaking is True


def test_diff_layouts_detects_node_moved():
    old = _simple_layout()
    new = old.model_copy(deep=True)
    for n in new.nodes:
        if n.id == "a":
            n.x = 999
    result = validation_bridge.diff_layouts(old, new)
    assert any(e.kind == "node_moved" and e.node_id == "a" for e in result.entries)


def test_diff_layouts_detects_node_config_changed():
    old = _simple_layout()
    new = old.model_copy(deep=True)
    for n in new.nodes:
        if n.id == "a":
            n.config = {"changed": True}
    result = validation_bridge.diff_layouts(old, new)
    assert any(e.kind == "node_changed" and e.node_id == "a" for e in result.entries)


def test_diff_layouts_detects_edge_added_and_removed():
    old = _simple_layout()
    new = old.model_copy(deep=True)
    new.edges = [e for e in new.edges if e.id != "e2"]
    result = validation_bridge.diff_layouts(old, new)
    assert any(e.kind == "edge_removed" and e.edge_id == "e2" for e in result.entries)


def test_diff_layouts_identical_layouts_have_no_entries():
    layout = _simple_layout()
    result = validation_bridge.diff_layouts(layout, layout.model_copy(deep=True))
    assert result.entries == []
    assert result.is_breaking is False


def test_fetch_version_history_returns_none_when_unavailable():
    assert validation_bridge.fetch_version_history("wf1") is None


def test_fetch_version_history_uses_real_registry_when_importable(monkeypatch):
    import sys
    import types

    fake_module = types.ModuleType("services.workflow.version_registry")
    fake_module.list_versions = lambda name: [{"version": "1.0.0"}]
    monkeypatch.setitem(sys.modules, "services.workflow.version_registry", fake_module)
    result = validation_bridge.fetch_version_history("wf1")
    assert result == [{"version": "1.0.0"}]
    del sys.modules["services.workflow.version_registry"]


def test_fetch_replay_overlay_unavailable_when_no_engine():
    overlay = validation_bridge.fetch_replay_overlay("wf1", "exec1")
    assert overlay.available is False


def test_fetch_replay_overlay_uses_real_engine_when_importable(monkeypatch):
    import sys
    import types

    class FakeState:
        node_id = "a"
        status = "running"
        duration_ms = 12.5
        started_at = None
        finished_at = None

    class FakeTrace:
        node_states = [FakeState()]

    fake_module = types.ModuleType("services.workflow.replay_engine")
    fake_module.get_execution_trace = lambda exec_id: FakeTrace()
    monkeypatch.setitem(sys.modules, "services.workflow.replay_engine", fake_module)

    overlay = validation_bridge.fetch_replay_overlay("wf1", "exec1")
    assert overlay.available is True
    assert overlay.current_node_id == "a"
    del sys.modules["services.workflow.replay_engine"]


def test_fetch_replay_overlay_handles_engine_raising(monkeypatch):
    import sys
    import types

    def _raise(exec_id):
        raise RuntimeError("boom")

    fake_module = types.ModuleType("services.workflow.replay_engine")
    fake_module.get_execution_trace = _raise
    monkeypatch.setitem(sys.modules, "services.workflow.replay_engine", fake_module)

    overlay = validation_bridge.fetch_replay_overlay("wf1", "exec1")
    assert overlay.available is False
    del sys.modules["services.workflow.replay_engine"]


# ══════════════════════════════════════════════════════════════
# LAYER 8 — Repository
# ══════════════════════════════════════════════════════════════

@pytest.fixture
async def db_session():
    from infrastructure.database.workflow_designer_models import Base

    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False},
                                  poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_repository_save_and_get_layout(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    layout = _simple_layout()
    await DesignerRepository.save_layout(db_session, layout)
    await db_session.commit()

    got = await DesignerRepository.get_layout(db_session, "wf1")
    assert got == layout


@pytest.mark.asyncio
async def test_repository_get_layout_missing_returns_none(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    assert await DesignerRepository.get_layout(db_session, "nope") is None


@pytest.mark.asyncio
async def test_repository_save_layout_upserts_existing(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    layout = _simple_layout()
    await DesignerRepository.save_layout(db_session, layout)
    await db_session.commit()

    updated = layout.model_copy(update={"version": "0.0.2"})
    await DesignerRepository.save_layout(db_session, updated)
    await db_session.commit()

    got = await DesignerRepository.get_layout(db_session, "wf1")
    assert got.version == "0.0.2"

    all_layouts = await DesignerRepository.list_layouts(db_session)
    assert len(all_layouts) == 1


@pytest.mark.asyncio
async def test_repository_list_layouts_sorted_by_name(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    await DesignerRepository.save_layout(db_session, _simple_layout("wf_b"))
    await DesignerRepository.save_layout(db_session, _simple_layout("wf_a"))
    await db_session.commit()

    layouts = await DesignerRepository.list_layouts(db_session)
    assert [l.workflow_name for l in layouts] == ["wf_a", "wf_b"]


@pytest.mark.asyncio
async def test_repository_delete_layout(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    await DesignerRepository.save_layout(db_session, _simple_layout())
    await db_session.commit()

    deleted = await DesignerRepository.delete_layout(db_session, "wf1")
    await db_session.commit()
    assert deleted is True
    assert await DesignerRepository.get_layout(db_session, "wf1") is None


@pytest.mark.asyncio
async def test_repository_delete_layout_missing_returns_false(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    assert await DesignerRepository.delete_layout(db_session, "nope") is False


@pytest.mark.asyncio
async def test_repository_save_layout_creates_snapshot_by_default(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    await DesignerRepository.save_layout(db_session, _simple_layout())
    await db_session.commit()

    snapshots = await DesignerRepository.list_snapshots(db_session, "wf1")
    assert len(snapshots) == 1
    assert snapshots[0].reason == "save"


@pytest.mark.asyncio
async def test_repository_save_layout_snapshot_false_skips_snapshot(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    await DesignerRepository.save_layout(db_session, _simple_layout(), snapshot=False)
    await db_session.commit()

    snapshots = await DesignerRepository.list_snapshots(db_session, "wf1")
    assert snapshots == []


@pytest.mark.asyncio
async def test_repository_multiple_saves_accumulate_snapshot_history(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    layout = _simple_layout()
    await DesignerRepository.save_layout(db_session, layout)
    await DesignerRepository.save_layout(db_session, layout.model_copy(update={"version": "0.0.2"}))
    await db_session.commit()

    snapshots = await DesignerRepository.list_snapshots(db_session, "wf1")
    assert len(snapshots) == 2


@pytest.mark.asyncio
async def test_repository_restore_snapshot_sets_current_layout(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    original = _simple_layout()
    await DesignerRepository.save_layout(db_session, original)
    await db_session.commit()
    snapshots = await DesignerRepository.list_snapshots(db_session, "wf1")
    original_snapshot_id = snapshots[0].id

    changed = original.model_copy(update={"version": "9.9.9"})
    await DesignerRepository.save_layout(db_session, changed)
    await db_session.commit()

    restored = await DesignerRepository.restore_snapshot(db_session, original_snapshot_id)
    await db_session.commit()
    assert restored.version == "0.0.1"

    current = await DesignerRepository.get_layout(db_session, "wf1")
    assert current.version == "0.0.1"


@pytest.mark.asyncio
async def test_repository_restore_snapshot_missing_returns_none(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    assert await DesignerRepository.restore_snapshot(db_session, "nonexistent-id") is None


@pytest.mark.asyncio
async def test_repository_open_and_touch_session(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    await DesignerRepository.save_layout(db_session, _simple_layout())
    await db_session.commit()

    session = await DesignerRepository.open_session(db_session, "wf1", user_id="u1")
    await db_session.commit()
    assert session.dirty is False

    touched = await DesignerRepository.touch_session(db_session, session.id, dirty=True, replay_mode=True)
    await db_session.commit()
    assert touched.dirty is True
    assert touched.replay_mode is True


@pytest.mark.asyncio
async def test_repository_touch_session_missing_returns_none(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    assert await DesignerRepository.touch_session(db_session, "nonexistent") is None


@pytest.mark.asyncio
async def test_repository_save_and_list_node_templates(db_session):
    from services.integration.workflow_designer.designer_repository import DesignerRepository

    template = NodeTemplate(node_type=NodeType.CUSTOM, category="custom", label="Custom A")
    template_id = await DesignerRepository.save_node_template(db_session, template)
    await db_session.commit()
    assert template_id

    templates = await DesignerRepository.list_node_templates(db_session)
    assert len(templates) == 1
    assert templates[0].label == "Custom A"


@pytest.mark.asyncio
async def test_fetch_designer_dashboard_section_no_db_returns_zeros():
    from services.integration.workflow_designer.designer_repository import fetch_designer_dashboard_section

    section = await fetch_designer_dashboard_section(None)
    assert section == {"workflow_count": 0, "recent_edits": [], "invalid_count": 0}


@pytest.mark.asyncio
async def test_fetch_designer_dashboard_section_with_data(db_session):
    from services.integration.workflow_designer.designer_repository import (
        DesignerRepository, fetch_designer_dashboard_section,
    )

    await DesignerRepository.save_layout(db_session, _simple_layout())
    await db_session.commit()

    section = await fetch_designer_dashboard_section(db_session)
    assert section["workflow_count"] == 1
    assert section["invalid_count"] == 0
    assert len(section["recent_edits"]) == 1


@pytest.mark.asyncio
async def test_fetch_designer_dashboard_section_counts_invalid_workflows(db_session):
    from services.integration.workflow_designer.designer_repository import (
        DesignerRepository, fetch_designer_dashboard_section,
    )

    bad_layout = WorkflowLayout(workflow_name="bad", nodes=[
        DesignerNode(id="x", node_type=NodeType.AGENT), DesignerNode(id="x", node_type=NodeType.TOOL),
    ])
    await DesignerRepository.save_layout(db_session, bad_layout)
    await db_session.commit()

    section = await fetch_designer_dashboard_section(db_session)
    assert section["invalid_count"] == 1


# ══════════════════════════════════════════════════════════════
# LAYER 9 — Export
# ══════════════════════════════════════════════════════════════

def test_designer_export_json_matches_serializer():
    layout = _simple_layout()
    assert designer_export.export_workflow(layout, "json") == workflow_serializer.export_json(layout)


def test_designer_export_yaml_matches_serializer():
    layout = _simple_layout()
    assert designer_export.export_workflow(layout, "yaml") == workflow_serializer.export_yaml(layout)


def test_designer_export_mermaid_matches_graph_builder():
    layout = _simple_layout()
    assert designer_export.export_workflow(layout, "mermaid") == graph_builder.to_mermaid(layout)


def test_designer_export_unknown_format_raises():
    with pytest.raises(SerializerError):
        designer_export.export_workflow(_simple_layout(), "pdf")


def test_designer_export_markdown_fallback_contains_nodes_and_edges():
    layout = _simple_layout()
    text = designer_export.export_workflow(layout, "markdown")
    assert "# Workflow: wf1" in text
    assert "| a | agent |" in text
    assert "| a | b |" in text


def test_designer_export_markdown_includes_warnings_section_when_present():
    layout = WorkflowLayout(workflow_name="wf", nodes=[DesignerNode(id="a", node_type=NodeType.AGENT)])
    text = designer_export.export_workflow(layout, "markdown")
    assert "## Warnings" in text


def test_render_markdown_uses_real_generator_when_importable(monkeypatch):
    import sys
    import types

    fake_module = types.ModuleType("services.workflow.doc_generator")
    fake_module.render_workflow_doc = lambda graph: "REAL DOC OUTPUT"
    monkeypatch.setitem(sys.modules, "services.workflow.doc_generator", fake_module)
    assert designer_export.render_markdown(_simple_layout()) == "REAL DOC OUTPUT"
    del sys.modules["services.workflow.doc_generator"]


# ══════════════════════════════════════════════════════════════
# LAYER 10 — Dashboard
# ══════════════════════════════════════════════════════════════

def test_build_designer_summary_none_when_raw_none():
    from services.integration.dashboard.dashboard_builder import build_designer_summary

    assert build_designer_summary(None) is None


def test_build_designer_summary_populates_fields():
    from services.integration.dashboard.dashboard_builder import build_designer_summary

    summary = build_designer_summary({"workflow_count": 3, "recent_edits": [{"workflow_name": "wf1"}],
                                       "invalid_count": 1})
    assert summary.workflow_count == 3
    assert summary.invalid_count == 1
    assert len(summary.recent_edits) == 1


def test_platform_dashboard_wires_designer_field():
    from services.integration.dashboard.dashboard_builder import assemble_platform_dashboard
    from services.integration.dashboard.dashboard_models import DesignerSummary, MetricsSnapshot

    dashboard = assemble_platform_dashboard(
        services=[], workflows=[], events=[], incidents=[], versions=[], metrics=MetricsSnapshot(),
        designer=DesignerSummary(workflow_count=2),
    )
    assert dashboard.designer is not None
    assert dashboard.designer.workflow_count == 2


def test_platform_dashboard_designer_defaults_to_none():
    from services.integration.dashboard.dashboard_builder import assemble_platform_dashboard
    from services.integration.dashboard.dashboard_models import MetricsSnapshot

    dashboard = assemble_platform_dashboard(services=[], workflows=[], events=[], incidents=[], versions=[],
                                             metrics=MetricsSnapshot())
    assert dashboard.designer is None


# ══════════════════════════════════════════════════════════════
# LAYER 11 — API
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def app_no_db():
    from fastapi import FastAPI

    from services.integration.api.designer import router
    app = FastAPI()
    app.include_router(router)
    app.state.db_factory = None
    return app


@pytest.fixture
def app_with_db():
    from fastapi import FastAPI

    from services.integration.api.designer import router
    app = FastAPI()
    app.include_router(router)

    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False},
                                  poolclass=StaticPool)

    @asynccontextmanager
    async def db_factory():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
            await session.commit()

    app.state.db_factory = db_factory
    app.state._engine = engine
    return app


@pytest.fixture
async def _init_schema(app_with_db):
    from infrastructure.database.workflow_designer_models import Base

    async with app_with_db.state._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return app_with_db


def _client(app):
    from httpx import ASGITransport, AsyncClient
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_api_list_workflows_empty_in_memory(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/designer/workflows")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0
        assert resp.json()["source"] == "in-memory"


@pytest.mark.asyncio
async def test_api_create_and_get_workflow_in_memory(app_no_db):
    layout = _simple_layout().model_dump(mode="json")
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/workflows", json=layout)
        assert resp.status_code == 200
        assert resp.json()["persisted"] is True

        resp = await client.get("/designer/workflows/wf1")
        assert resp.status_code == 200
        assert resp.json()["workflow_name"] == "wf1"


@pytest.mark.asyncio
async def test_api_get_workflow_missing_returns_404(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/designer/workflows/nope")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_create_workflow_invalid_body_returns_422(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/workflows", json={"nodes": "not-a-list"})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_update_workflow_overrides_name_from_path(app_no_db):
    layout = _simple_layout().model_dump(mode="json")
    async with _client(app_no_db) as client:
        await client.post("/designer/workflows", json=layout)
        resp = await client.put("/designer/workflows/wf1", json={"nodes": [], "edges": []})
        assert resp.status_code == 200
        assert resp.json()["workflow"]["workflow_name"] == "wf1"
        assert resp.json()["workflow"]["nodes"] == []


@pytest.mark.asyncio
async def test_api_delete_workflow_in_memory(app_no_db):
    layout = _simple_layout().model_dump(mode="json")
    async with _client(app_no_db) as client:
        await client.post("/designer/workflows", json=layout)
        resp = await client.delete("/designer/workflows/wf1")
        assert resp.status_code == 200
        resp = await client.get("/designer/workflows/wf1")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_delete_workflow_missing_returns_404(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.delete("/designer/workflows/nope")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_get_library_includes_builtins(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/designer/library")
        assert resp.status_code == 200
        assert len(resp.json()["templates"]) == len(node_library.BUILTIN_TEMPLATES)


@pytest.mark.asyncio
async def test_api_get_designer_plugins_empty_without_modules(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/designer/plugins")
        assert resp.status_code == 200
        assert resp.json()["node_templates"] == []


@pytest.mark.asyncio
async def test_api_validate_workflow(app_no_db):
    layout = _simple_layout().model_dump(mode="json")
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/validate", json=layout)
        assert resp.status_code == 200
        assert resp.json()["valid"] is True


@pytest.mark.asyncio
async def test_api_validate_invalid_body_returns_422(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/validate", json={"nodes": "bad"})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_export_workflow_json(app_no_db):
    layout = _simple_layout().model_dump(mode="json")
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/export", json={"layout": layout, "format": "json"})
        assert resp.status_code == 200
        assert json.loads(resp.json()["content"])["workflow_name"] == "wf1"


@pytest.mark.asyncio
async def test_api_export_missing_layout_returns_422(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/export", json={"format": "json"})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_export_unknown_format_returns_422(app_no_db):
    layout = _simple_layout().model_dump(mode="json")
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/export", json={"layout": layout, "format": "pdf"})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_import_workflow_json(app_no_db):
    layout = _simple_layout()
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/import", json={"content": workflow_serializer.export_json(layout),
                                                              "format": "json"})
        assert resp.status_code == 200
        assert resp.json()["workflow"]["workflow_name"] == "wf1"


@pytest.mark.asyncio
async def test_api_import_workflow_mermaid(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/import",
                                   json={"content": 'flowchart TD\n    a --> b\n', "format": "mermaid"})
        assert resp.status_code == 200
        assert len(resp.json()["workflow"]["nodes"]) == 2


@pytest.mark.asyncio
async def test_api_import_missing_content_returns_422(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/import", json={"format": "json"})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_history_no_db_returns_empty(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.get("/designer/history/wf1")
        assert resp.status_code == 200
        assert resp.json()["snapshots"] == []


@pytest.mark.asyncio
async def test_api_replay_no_engine_returns_unavailable(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/replay/wf1", json={"execution_id": "exec1"})
        assert resp.status_code == 200
        assert resp.json()["available"] is False


@pytest.mark.asyncio
async def test_api_replay_missing_execution_id_returns_422(app_no_db):
    async with _client(app_no_db) as client:
        resp = await client.post("/designer/replay/wf1", json={})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_workflows_db_backed_round_trip(_init_schema):
    app = _init_schema
    layout = _simple_layout().model_dump(mode="json")
    async with _client(app) as client:
        resp = await client.post("/designer/workflows", json=layout)
        assert resp.json()["persisted"] is True

        resp = await client.get("/designer/workflows")
        assert resp.json()["source"] == "database"
        assert resp.json()["count"] == 1

        resp = await client.get("/designer/history/wf1")
        assert len(resp.json()["snapshots"]) == 1

        resp = await client.delete("/designer/workflows/wf1")
        assert resp.status_code == 200

        resp = await client.get("/designer/workflows/wf1")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_db_error_returns_503(app_with_db):
    # schema not initialized -> underlying query should fail cleanly as 503, not 500-crash silently
    async with _client(app_with_db) as client:
        resp = await client.get("/designer/workflows")
        assert resp.status_code == 503


# ══════════════════════════════════════════════════════════════
# LAYER 12 — CLI
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def workflow_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wf1.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(workflow_serializer.export_json(_simple_layout()))
        yield path


@pytest.fixture
def workflow_file_yaml():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wf1.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(workflow_serializer.export_yaml(_simple_layout()))
        yield path


def test_cli_validate_valid_workflow(workflow_file, capsys):
    from services.integration.workflow_designer import designer_cli

    code = designer_cli.main(["designer", "validate", workflow_file])
    assert code == 0
    assert "VALID" in capsys.readouterr().out


def test_cli_validate_missing_file_returns_error(capsys):
    from services.integration.workflow_designer import designer_cli

    code = designer_cli.main(["designer", "validate", "/tmp/does-not-exist-m48.json"])
    assert code == 1
    assert "Could not load" in capsys.readouterr().out


def test_cli_export_json(workflow_file, capsys):
    from services.integration.workflow_designer import designer_cli

    code = designer_cli.main(["designer", "export", workflow_file, "--format", "json"])
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["workflow_name"] == "wf1"


def test_cli_export_mermaid(workflow_file, capsys):
    from services.integration.workflow_designer import designer_cli

    code = designer_cli.main(["designer", "export", workflow_file, "--format", "mermaid"])
    assert code == 0
    assert "flowchart TD" in capsys.readouterr().out


def test_cli_import_yaml_file_autodetected(workflow_file_yaml, capsys):
    from services.integration.workflow_designer import designer_cli

    code = designer_cli.main(["designer", "import", workflow_file_yaml])
    assert code == 0
    assert "Imported workflow" in capsys.readouterr().out


def test_cli_import_without_db_url_notes_not_persisted(workflow_file, capsys):
    from services.integration.workflow_designer import designer_cli

    designer_cli.main(["designer", "import", workflow_file])
    assert "not durably recorded" in capsys.readouterr().out


def test_cli_diff_identical_files_non_breaking(workflow_file, capsys):
    from services.integration.workflow_designer import designer_cli

    code = designer_cli.main(["designer", "diff", workflow_file, workflow_file])
    assert code == 0
    assert "non-breaking" in capsys.readouterr().out


def test_cli_diff_breaking_change(workflow_file, capsys):
    from services.integration.workflow_designer import designer_cli

    with tempfile.TemporaryDirectory() as tmp:
        new_path = os.path.join(tmp, "wf1_new.json")
        smaller = _simple_layout()
        smaller.nodes = [n for n in smaller.nodes if n.id != "c"]
        smaller.edges = [e for e in smaller.edges if "c" not in (e.source, e.target)]
        with open(new_path, "w", encoding="utf-8") as f:
            f.write(workflow_serializer.export_json(smaller))
        code = designer_cli.main(["designer", "diff", workflow_file, new_path])
    assert code == 0
    assert "(breaking)" in capsys.readouterr().out


def test_cli_replay_unavailable(capsys):
    from services.integration.workflow_designer import designer_cli

    code = designer_cli.main(["designer", "replay", "exec1", "--workflow", "wf1"])
    assert code == 1
    assert "unavailable" in capsys.readouterr().out


def test_cli_library_lists_categories(capsys):
    from services.integration.workflow_designer import designer_cli

    code = designer_cli.main(["designer", "library"])
    assert code == 0
    out = capsys.readouterr().out
    assert "core:" in out


def test_cli_requires_subcommand():
    from services.integration.workflow_designer import designer_cli

    with pytest.raises(SystemExit):
        designer_cli.main(["designer"])


# ══════════════════════════════════════════════════════════════
# LAYER 13 — Regression / isolation
# ══════════════════════════════════════════════════════════════

def test_graph_build_result_identical_for_handwritten_equivalent_layout():
    """§4's core claim: two structurally-equal workflows (one built via
    the designer, one assembled 'by hand' as a WorkflowLayout with the
    same nodes/edges/entry point) produce byte-identical GraphBuildResult
    payloads regardless of how they were authored."""
    designer_authored = _simple_layout()
    handwritten = WorkflowLayout(
        workflow_name="wf1",
        nodes=[
            DesignerNode(id="a", node_type=NodeType.AGENT, label="Agent A", x=9999, y=9999, color="#000"),
            DesignerNode(id="b", node_type=NodeType.TOOL, label="Tool B", x=1, y=1),
            DesignerNode(id="c", node_type=NodeType.DECISION, label="Decide", x=2, y=2, comment="hand-authored"),
        ],
        edges=[
            DesignerEdge(id="e1", source="a", target="b", label="handwritten label"),
            DesignerEdge(id="e2", source="b", target="c", condition="result == True"),
        ],
        entry_node_id="a",
    )
    a = graph_builder.build_graph(designer_authored)
    b = graph_builder.build_graph(handwritten)
    assert graph_builder.diff_structure(a, b) is True
    assert a.model_dump(exclude={"workflow_name"}) == b.model_dump(exclude={"workflow_name"})


def test_canvas_state_never_mutates_original_layout_object_on_undo():
    original = _simple_layout()
    cs = canvas_state.new_canvas_state(original)
    cs = canvas_state.add_node(cs, DesignerNode(id="d", node_type=NodeType.DELAY))
    assert len(original.nodes) == 3  # untouched — canvas_state deep-copies before mutating


def test_validation_bridge_never_duplicates_graph_builder_logic():
    """DESIGNER-GRAPH-WARNING issues must come verbatim from
    graph_builder.build_graph's own warnings list, not a second,
    parallel implementation."""
    layout = WorkflowLayout(workflow_name="wf", nodes=[DesignerNode(id="a", node_type=NodeType.AGENT)],
                             edges=[DesignerEdge(id="e1", source="a", target="missing")])
    build_warnings = set(graph_builder.build_graph(layout).warnings)
    validation_warnings = {i.message for i in validation_bridge.validate_layout(layout).issues
                            if i.rule_id == "DESIGNER-GRAPH-WARNING"}
    assert validation_warnings == build_warnings


def test_export_and_import_full_round_trip_via_designer_export():
    layout = _simple_layout()
    text = designer_export.export_workflow(layout, "json")
    restored = workflow_deserializer.import_json(text)
    assert restored == layout


def test_mermaid_export_import_round_trip_preserves_node_and_edge_topology():
    layout = _simple_layout()
    mermaid = graph_builder.to_mermaid(layout)
    restored = workflow_deserializer.import_mermaid(mermaid, workflow_name=layout.workflow_name)
    assert {n.id for n in restored.nodes} == {n.id for n in layout.nodes}
    assert {(e.source, e.target) for e in restored.edges} == {(e.source, e.target) for e in layout.edges}
