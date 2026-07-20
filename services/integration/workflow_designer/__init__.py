"""
services/integration/workflow_designer/
=================================
M4.8 — Visual Workflow Designer. See
docs/M4.8_Workflow_Designer_Handover.md for the full writeup.

A browser-based designer sitting on top of the existing workflow engine:
create/edit/validate/compare/export workflows visually, generating
runtime-compatible workflow definitions. Not a replacement for LangGraph
or existing workflow definitions — see `graph_builder.py`'s module
docstring for how that boundary is enforced structurally.

    designer_models.py        Pure Pydantic shapes (WorkflowLayout, DesignerNode,
                              DesignerEdge, CanvasState, NodeTemplate,
                              ValidationBridgeResult, LayoutDiffResult,
                              ReplayOverlay, ...).
    node_library.py            Builtin node templates (Agent/Decision/Conditional/
                              Parallel/Join/Tool/Event/Human Approval/Retry/Delay)
                              + plugin node discovery + custom categories.
    canvas_state.py             Zoom/pan/selection/undo/redo/clipboard/autosave —
                              entirely frontend-agnostic.
    workflow_serializer.py      WorkflowLayout -> JSON/YAML (export).
    workflow_deserializer.py    JSON/YAML/Mermaid -> WorkflowLayout (import).
    graph_builder.py            WorkflowLayout -> {nodes, edges, entry_point}
                              (the pure runtime shape) + Mermaid export.
    validation_bridge.py        Bridges into workflow_validator / graph_linter /
                              version_registry / replay_engine — never
                              duplicates their logic, degrades gracefully when
                              those integrations aren't wired.
    designer_repository.py      Repository-pattern DB persistence (workflow_layouts /
                              designer_sessions / canvas_snapshots / node_templates)
                              + fetch_designer_dashboard_section.
    designer_export.py          json/yaml/mermaid/markdown export.
    designer_cli.py             python designer_cli.py designer [validate|export|
                              import|diff|replay|library]
"""
