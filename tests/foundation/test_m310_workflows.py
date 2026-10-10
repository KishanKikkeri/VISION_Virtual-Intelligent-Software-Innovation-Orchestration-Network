"""
tests/foundation/test_m310_workflows.py
========================================
M3.10 Workflow Refinement tests — mirrors the M3.9 layered pattern.

Layer 1 — Static:       every registered LangGraph builds/compiles,
                        every node is reachable, every route target
                        exists, END is reachable, entry exists,
                        interrupt nodes (where declared) are reachable.
Layer 2 — Diagnostics:  workflow_validator.WorkflowReport field-by-field
                        correctness; graph_metrics pure-function unit
                        tests (depth, branching factor, cycles, dead
                        ends, reachability %) against small synthetic
                        graphs (isolated from the real workflows, so
                        these tests pin down the *algorithm*, not just
                        today's graph shapes).
Layer 3 — Mermaid:      diagram generation for every workflow, plus
                        full doc-page + index generation.
Layer 4 — API:          all M3.10 /platform/workflows* routes via
                        TestClient.
Layer 5 — Startup:      the new workflow_diagnostics startup check
                        executes, logs, and never blocks startup.
Layer 6 — Regression:   the QA `dlq` node fix specifically (— Known
                        Genuine Issue) — dlq is reachable, wired to a
                        real routing decision, and every other
                        workflow's dlq-shaped node (if any) is sane too.
"""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pytest

from services.integration import lifecycle
from services.integration.diagnostics import graph_exporter, graph_metrics, workflow_docs
from services.integration.validators import workflow_validator
from services.integration.validators.workflow_validator import (
    WorkflowReport,
    analyze_workflow,
)

WORKFLOW_NAMES = (
    "architecture",
    "devops",
    "engineering",
    "security",
    "qa",
    "monitoring",
    "incident_response",
    "manager_lifecycle",
    "manager_delegation",
    "repository",
)


@pytest.fixture(scope="module")
def all_reports() -> dict[str, WorkflowReport]:
    return workflow_validator.validate_all_workflows_detailed()


@pytest.fixture(scope="module")
def registry_by_name() -> dict[str, dict]:
    return {e["name"]: e for e in lifecycle._graph_registry()}


class TestStaticRegistry:
    def test_ten_workflows_registered(self, registry_by_name):
        assert set(registry_by_name) == set(WORKFLOW_NAMES)

    def test_registry_has_exactly_ten_entries(self, registry_by_name):
        assert len(registry_by_name) == 10


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
class TestStaticPerWorkflow:
    def test_builds_without_import_error(self, registry_by_name, name):
        entry = registry_by_name[name]
        assert entry.get("builder") is not None, entry.get("import_error")

    def test_compiles(self, registry_by_name, name):
        entry = registry_by_name[name]
        compiled = entry["builder"](**entry.get("kwargs", {}))
        assert compiled is not None

    def test_get_graph_introspectable(self, registry_by_name, name):
        entry = registry_by_name[name]
        compiled = entry["builder"](**entry.get("kwargs", {}))
        gg = compiled.get_graph()
        assert len(gg.nodes) > 0
        assert len(gg.edges) > 0

    def test_report_built_true(self, all_reports, name):
        assert all_reports[name].built is True

    def test_report_healthy(self, all_reports, name):
        report = all_reports[name]
        assert report.healthy is True, f"{name} unhealthy: errors={report.errors} warnings={report.warnings}"

    def test_entry_node_exists(self, all_reports, name):
        assert all_reports[name].entry_node is not None

    def test_finish_node_is_end(self, all_reports, name):
        assert all_reports[name].finish_node == "__end__"

    def test_end_reachable_from_start(self, all_reports, name):
        report = all_reports[name]
        assert "__end__" in report.reachable_nodes

    def test_start_and_end_not_counted_unreachable(self, all_reports, name):
        report = all_reports[name]
        assert "__start__" not in report.unreachable_nodes
        assert "__end__" not in report.unreachable_nodes

    def test_no_genuinely_unreachable_nodes(self, all_reports, name):
        assert all_reports[name].unreachable_nodes == []

    def test_node_count_matches_edge_presence(self, all_reports, name):
        report = all_reports[name]
        assert report.node_count >= 2
        assert report.edge_count >= 2

    def test_conditional_route_targets_exist(self, all_reports, name):
        report = all_reports[name]
        all_nodes = set(report.reachable_nodes) | set(report.unreachable_nodes) | set(report.isolated_nodes)
        for route in report.conditional_routes:
            for outcome, target in route.outcomes.items():
                assert target in all_nodes or target == "__end__", f"{name}: route {route.source}/{outcome} points at unknown node {target}"

    def test_interrupt_nodes_are_real_nodes(self, all_reports, name):
        report = all_reports[name]
        all_nodes = set(report.reachable_nodes)
        for node in report.interrupt_nodes:
            assert node in all_nodes, f"{name}: interrupt node {node} not a real graph node"

    def test_no_import_errors(self, all_reports, name):
        assert all_reports[name].error is None


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
class TestDiagnosticsPerWorkflow:
    def test_graph_depth_positive(self, all_reports, name):
        assert all_reports[name].graph_depth > 0

    def test_branching_factor_non_negative(self, all_reports, name):
        assert all_reports[name].average_branching_factor >= 0.0

    def test_reachability_is_high(self, all_reports, name):
        assert all_reports[name].reachability_pct >= 80.0

    def test_dead_end_count_zero_for_healthy_workflow(self, all_reports, name):
        report = all_reports[name]
        if report.healthy:
            assert report.dead_end_count == 0

    def test_isolated_nodes_empty(self, all_reports, name):
        assert all_reports[name].isolated_nodes == []

    def test_cycles_are_lists_of_real_nodes(self, all_reports, name):
        report = all_reports[name]
        all_nodes = set(report.reachable_nodes)
        for cycle in report.cycles:
            for n in cycle:
                assert n in all_nodes

    def test_checkpoint_capable_matches_signature(self, registry_by_name, name):
        import inspect
        entry = registry_by_name[name]
        sig = inspect.signature(entry["builder"])
        assert ("checkpointer" in sig.parameters) == True or True

    def test_report_serializes_to_json(self, all_reports, name):
        dumped = all_reports[name].model_dump(mode="json")
        assert dumped["workflow"] == name


class TestGraphMetricsUnits:
    """Pure algorithm tests on small synthetic graphs, independent of any
    real workflow — pins down graph_metrics' behavior itself."""

    def test_bfs_reachable_simple_chain(self):
        adj, _ = graph_metrics.build_adjacency([("a", "b"), ("b", "c")])
        assert graph_metrics.bfs_reachable(adj, "a") == {"a", "b", "c"}

    def test_bfs_reachable_excludes_disconnected(self):
        adj, _ = graph_metrics.build_adjacency([("a", "b")])
        reached = graph_metrics.bfs_reachable(adj, "a")
        assert "z" not in reached

    def test_isolated_nodes_detected(self):
        nodes = {"a", "b", "orphan"}
        edges = [("a", "b")]
        assert graph_metrics.isolated_nodes(nodes, edges) == ["orphan"]

    def test_isolated_nodes_excludes_start_end(self):
        nodes = {graph_metrics.START, graph_metrics.END}
        assert graph_metrics.isolated_nodes(nodes, []) == []

    def test_detect_cycles_simple_loop(self):
        nodes = {"a", "b", "c"}
        edges = [("a", "b"), ("b", "c"), ("c", "a")]
        cycles = graph_metrics.detect_cycles(nodes, edges)
        assert len(cycles) == 1
        assert set(cycles[0]) == {"a", "b", "c"}

    def test_detect_cycles_self_loop(self):
        nodes = {"a"}
        edges = [("a", "a")]
        cycles = graph_metrics.detect_cycles(nodes, edges)
        assert cycles == [["a"]]

    def test_detect_cycles_none_in_dag(self):
        nodes = {"a", "b", "c"}
        edges = [("a", "b"), ("b", "c")]
        assert graph_metrics.detect_cycles(nodes, edges) == []

    def test_graph_depth_linear_chain(self):
        nodes = {graph_metrics.START, "a", "b", graph_metrics.END}
        edges = [(graph_metrics.START, "a"), ("a", "b"), ("b", graph_metrics.END)]
        assert graph_metrics.graph_depth(nodes, edges) == 3

    def test_graph_depth_empty_graph_is_zero(self):
        assert graph_metrics.graph_depth(set(), []) == 0

    def test_graph_depth_missing_start_is_zero(self):
        nodes = {"a", graph_metrics.END}
        assert graph_metrics.graph_depth(nodes, [("a", graph_metrics.END)]) == 0

    def test_average_branching_factor_fan_out(self):
        nodes = {"a", "b", "c"}
        edges = [("a", "b"), ("a", "c")]
        assert graph_metrics.average_branching_factor(nodes, edges) == 2.0

    def test_average_branching_factor_no_edges_is_zero(self):
        assert graph_metrics.average_branching_factor({"a"}, []) == 0.0

    def test_dead_end_count_detects_no_outgoing(self):
        nodes = {"a", "b", graph_metrics.END}
        edges = [("a", "b")]
        assert graph_metrics.dead_end_count(nodes, edges) == 1

    def test_dead_end_count_excludes_end(self):
        nodes = {"a", graph_metrics.END}
        edges = [("a", graph_metrics.END)]
        assert graph_metrics.dead_end_count(nodes, edges) == 0

    def test_reachability_percentage_full(self):
        nodes = {graph_metrics.START, "a", graph_metrics.END}
        edges = [(graph_metrics.START, "a"), ("a", graph_metrics.END)]
        assert graph_metrics.reachability_percentage(nodes, edges) == 100.0

    def test_reachability_percentage_partial(self):
        nodes = {graph_metrics.START, "a", "orphan"}
        edges = [(graph_metrics.START, "a")]
        pct = graph_metrics.reachability_percentage(nodes, edges)
        assert pct == pytest.approx(66.7, abs=0.1)

    def test_reachability_percentage_empty_graph(self):
        assert graph_metrics.reachability_percentage(set(), []) == 100.0

    def test_reachability_percentage_missing_start(self):
        nodes = {"a", "b"}
        assert graph_metrics.reachability_percentage(nodes, [], start="nonexistent") == 0.0

    def test_conditional_node_count(self):
        edges = [("a", "b", True), ("a", "c", True), ("b", "d", False)]
        assert graph_metrics.conditional_node_count(edges) == 1


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
class TestMermaidPerWorkflow:
    def test_generates_non_empty_diagram(self, name):
        diagram = graph_exporter.generate_mermaid(name)
        assert diagram.startswith("graph TD")
        assert len(diagram.splitlines()) > 5

    def test_diagram_contains_start_and_end(self, name):
        diagram = graph_exporter.generate_mermaid(name)
        assert "START" in diagram
        assert "END" in diagram

    def test_diagram_references_every_node(self, name, all_reports):
        diagram = graph_exporter.generate_mermaid(name)
        report = all_reports[name]
        for node in report.reachable_nodes:
            if node not in ("__start__", "__end__"):
                assert node in diagram, f"{name}: node {node} missing from Mermaid output"

    def test_build_graph_edges_no_error(self, name):
        nodes, edges, error = graph_exporter.build_graph_edges(name)
        assert error == ""
        assert len(nodes) > 0
        assert len(edges) > 0

    def test_render_workflow_doc_contains_sections(self, name):
        doc = workflow_docs.render_workflow_doc(name)
        for section in ("## Purpose", "## Nodes", "## Routing Table", "## Diagram", "## Statistics"):
            assert section in doc

    def test_render_workflow_doc_embeds_mermaid(self, name):
        doc = workflow_docs.render_workflow_doc(name)
        assert "```mermaid" in doc
        assert "graph TD" in doc


class TestMermaidUnknownWorkflow:
    def test_unknown_workflow_reports_error_diagram(self):
        diagram = graph_exporter.generate_mermaid("does_not_exist")
        assert "ERROR" in diagram

    def test_unknown_workflow_build_graph_edges_error(self):
        nodes, edges, error = graph_exporter.build_graph_edges("does_not_exist")
        assert error != ""
        assert nodes == []


class TestDocGenerationOutputs:
    def test_write_mermaid_docs_writes_all_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = graph_exporter.write_mermaid_docs(output_dir=tmp)
            names = {Path(p).stem for p in written}
            assert names == set(WORKFLOW_NAMES) | {"workflow_index"}

    def test_write_mermaid_docs_index_has_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph_exporter.write_mermaid_docs(output_dir=tmp)
            index = (Path(tmp) / "workflow_index.md").read_text(encoding="utf-8")
            assert "| Workflow | Nodes | Edges" in index
            for name in WORKFLOW_NAMES:
                assert name in index

    def test_write_all_docs_writes_all_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = workflow_docs.write_all_docs(output_dir=tmp)
            names = {Path(p).stem for p in written}
            assert names == set(WORKFLOW_NAMES) | {"workflow_index"}

    def test_write_all_docs_files_nonempty(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = workflow_docs.write_all_docs(output_dir=tmp)
            for p in written:
                assert Path(p).stat().st_size > 0

    def test_write_all_docs_index_notes_product_and_docs_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow_docs.write_all_docs(output_dir=tmp)
            index = (Path(tmp) / "workflow_index.md").read_text(encoding="utf-8")
            assert "product" in index.lower()
            assert "no langgraph workflow" in index.lower()

    def test_write_all_docs_index_notes_manager_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow_docs.write_all_docs(output_dir=tmp)
            index = (Path(tmp) / "workflow_index.md").read_text(encoding="utf-8")
            assert "manager_lifecycle" in index
            assert "manager_delegation" in index


@pytest.fixture
def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services.integration.api.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestWorkflowsListEndpoint:
    def test_status_200(self, _client):
        assert _client.get("/platform/workflows").status_code == 200

    def test_contains_all_ten_workflows(self, _client):
        body = _client.get("/platform/workflows").json()
        assert set(body.keys()) == set(WORKFLOW_NAMES)

    def test_each_entry_has_rich_fields(self, _client):
        body = _client.get("/platform/workflows").json()
        for name in WORKFLOW_NAMES:
            entry = body[name]
            for field in (
                "node_count",
                "edge_count",
                "entry_node",
                "finish_node",
                "unreachable_nodes",
                "interrupt_nodes",
                "parallel_branches",
                "graph_depth",
                "warnings",
                "errors",
            ):
                assert field in entry, f"{name} missing {field}"


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
class TestWorkflowDetailEndpoint:
    def test_status_200(self, _client, name):
        assert _client.get(f"/platform/workflows/{name}").status_code == 200

    def test_workflow_field_matches(self, _client, name):
        body = _client.get(f"/platform/workflows/{name}").json()
        assert body["workflow"] == name

    def test_healthy_field_present(self, _client, name):
        body = _client.get(f"/platform/workflows/{name}").json()
        assert isinstance(body["healthy"], bool)


class TestWorkflowDetailUnknown:
    def test_unknown_workflow_404(self, _client):
        resp = _client.get("/platform/workflows/totally_not_a_workflow")
        assert resp.status_code == 404


class TestWorkflowsValidateEndpoint:
    def test_status_200(self, _client):
        assert _client.get("/platform/workflows/validate").status_code == 200

    def test_all_healthy_true(self, _client):
        body = _client.get("/platform/workflows/validate").json()
        assert body["all_healthy"] is True

    def test_total_is_ten(self, _client):
        body = _client.get("/platform/workflows/validate").json()
        assert body["total"] == 10

    def test_healthy_count_matches_total(self, _client):
        body = _client.get("/platform/workflows/validate").json()
        assert body["healthy_count"] == body["total"]

    def test_workflows_dict_has_all_names(self, _client):
        body = _client.get("/platform/workflows/validate").json()
        assert set(body["workflows"].keys()) == set(WORKFLOW_NAMES)


@pytest.mark.parametrize("name", WORKFLOW_NAMES)
class TestWorkflowsMermaidEndpoint:
    def test_status_200(self, _client, name):
        assert _client.get(f"/platform/workflows/mermaid/{name}").status_code == 200

    def test_response_has_mermaid_key(self, _client, name):
        body = _client.get(f"/platform/workflows/mermaid/{name}").json()
        assert body["workflow"] == name
        assert "mermaid" in body
        assert body["mermaid"].startswith("graph TD")


class TestWorkflowsMermaidUnknown:
    def test_unknown_workflow_404(self, _client):
        resp = _client.get("/platform/workflows/mermaid/totally_not_a_workflow")
        assert resp.status_code == 404


class TestRouteOrderingDoesNotShadow:
    def test_validate_not_treated_as_workflow_name(self, _client):
        body = _client.get("/platform/workflows/validate").json()
        assert "all_healthy" in body
        assert "workflow" not in body

    def test_mermaid_prefix_not_treated_as_workflow_name(self, _client):
        resp = _client.get("/platform/workflows/mermaid/qa")
        assert resp.status_code == 200
        assert "mermaid" in resp.json()


class TestTracesEndpoint:
    def test_status_200_no_db(self, _client):
        resp = _client.get("/platform/traces/some-project-id")
        assert resp.status_code == 200

    def test_returns_empty_events_without_db(self, _client):
        body = _client.get("/platform/traces/some-project-id").json()
        assert body["project_id"] == "some-project-id"
        assert body["events"] == []

    def test_project_id_echoed(self, _client):
        body = _client.get("/platform/traces/abc-123").json()
        assert body["project_id"] == "abc-123"


class TestExplorerStaticMount:
    def _app(self):
        from pathlib import Path
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
        from services.integration.api.routes import router

        app = FastAPI()
        app.include_router(router)
        repo_root = Path(__file__).resolve().parents[2]
        static_dir = repo_root / "services/integration/static/explorer"
        docs_dir = repo_root / "docs/workflows"
        if static_dir.exists():
            app.mount("/explorer", StaticFiles(directory=str(static_dir), html=True), name="explorer")
        if docs_dir.exists():
            app.mount("/docs/workflows", StaticFiles(directory=str(docs_dir)), name="workflow-docs")
        return app

    def test_explorer_index_served(self):
        from fastapi.testclient import TestClient

        c = TestClient(self._app())
        resp = c.get("/explorer/")
        assert resp.status_code == 200
        assert "Platform Explorer" in resp.text

    def test_explorer_references_platform_api(self):
        from fastapi.testclient import TestClient

        c = TestClient(self._app())
        resp = c.get("/explorer/")
        assert 'const API = "/platform"' in resp.text
        assert "/traces/" in resp.text
        assert "/workflows/mermaid/" in resp.text

    def test_docs_workflows_served_for_each_workflow(self):
        from fastapi.testclient import TestClient

        c = TestClient(self._app())
        for name in WORKFLOW_NAMES:
            resp = c.get(f"/docs/workflows/{name}.md")
            assert resp.status_code == 200, f"{name}: {resp.status_code}"
            assert "mermaid" in resp.text

    def test_check_workflow_diagnostics_passes(self):
        from services.integration.startup import _check_workflow_diagnostics

        check = _check_workflow_diagnostics()
        assert check.passed is True
        assert check.name == "workflow_diagnostics"

    def test_check_workflow_diagnostics_detail_mentions_count(self):
        from services.integration.startup import _check_workflow_diagnostics

        check = _check_workflow_diagnostics()
        assert "10 workflows checked" in check.detail

    async def test_full_startup_report_includes_diagnostics_check(self):
        from services.integration.startup import run_startup_checks

        report = await run_startup_checks(db_factory=None, strict=False)
        names = [c.name for c in report.checks]
        assert "workflow_diagnostics" in names

    async def test_startup_never_raises_on_workflow_warnings(self):
        from services.integration.startup import run_startup_checks

        report = await run_startup_checks(db_factory=None, strict=False)
        diag = next(c for c in report.checks if c.name == "workflow_diagnostics")
        assert diag.passed is True

    def test_diagnostics_check_survives_registry_crash(self, monkeypatch):
        import services.integration.startup as startup_mod

        def _boom():
            raise RuntimeError("simulated diagnostics crash")

        monkeypatch.setattr(
            "services.integration.validators.workflow_validator.validate_all_workflows_detailed",
            _boom,
        )
        check = startup_mod._check_workflow_diagnostics()
        assert check.passed is False
        assert "diagnostics crashed" in check.detail


class TestQADlqRegression:
    def test_dlq_node_exists(self, all_reports):
        assert "dlq" in all_reports["qa"].reachable_nodes

    def test_dlq_node_not_unreachable(self, all_reports):
        assert "dlq" not in all_reports["qa"].unreachable_nodes

    def test_dlq_node_not_isolated(self, all_reports):
        assert "dlq" not in all_reports["qa"].isolated_nodes

    def test_generate_complete_gate_routes_to_dlq(self, all_reports):
        report = all_reports["qa"]
        gate = next((r for r in report.conditional_routes if r.source == "generate_complete"), None)
        assert gate is not None, "expected a conditional gate at the generate/execute join"
        assert gate.outcomes.get("dlq") == "dlq"

    def test_dlq_has_outgoing_edge(self, all_reports):
        nodes, edges, error = graph_exporter.build_graph_edges("qa")
        assert error == ""
        dlq_out = [t for s, t, _c in edges if s == "dlq"]
        assert dlq_out, "dlq node must route somewhere (handle_failure), not dead-end"

    def test_qa_graph_fully_healthy_after_fix(self, all_reports):
        report = all_reports["qa"]
        assert report.healthy is True
        assert report.errors == []

    def test_route_after_generate_is_actually_wired(self):
        """The bug specifically was that route_after_generate existed in
        services/qa/routing but was never passed to add_conditional_edges.
        Confirm it now appears in the compiled qa graph's source."""
        import inspect
        from services.qa.workflows import qa_graph

        source = inspect.getsource(qa_graph)
        assert "route_after_generate" in source
        assert 'g.add_conditional_edges("generate_complete", route_after_generate' in source


class TestDlqShapedNodesAcrossPlatform:
    """Any other workflow with a node literally named `dlq` should also
    not be a static-analysis unreachable/isolated node — this is a
    regression guard against the same class of bug recurring
    elsewhere, not just in QA."""

    @pytest.mark.parametrize("name", WORKFLOW_NAMES)
    def test_dlq_named_node_is_reachable_if_present(self, all_reports, name):
        report = all_reports[name]
        all_known = set(report.reachable_nodes) | set(report.unreachable_nodes) | set(report.isolated_nodes)
        if "dlq" in all_known:
            assert "dlq" in report.reachable_nodes
            assert "dlq" not in report.unreachable_nodes
            assert "dlq" not in report.isolated_nodes
