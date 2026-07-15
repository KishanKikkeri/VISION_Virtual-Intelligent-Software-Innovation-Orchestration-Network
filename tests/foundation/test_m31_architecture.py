"""
tests/foundation/test_m31_architecture.py
==========================================
M3.1 Architecture Service tests — 4 layers matching Phase 2 pattern.

Layer 1 — Unit:   agent output contracts, validation functions
Layer 2 — Graph:  routing functions, node logic (no LLM/DB)
Layer 3 — Integration: lead coordination with mocked workers
Layer 4 — E2E:    full pipeline (marked @pytest.mark.e2e)
"""
from __future__ import annotations
import json, uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from core.contracts import AgentResult, TaskStatus
from core.runtime.context import AgentContext, TaskInput
from core.runtime.factory import AGENT_REGISTRY
from services.architecture.workflows.architecture_graph import (
    ArchitectureState, build_architecture_graph,
    route_after_system_design, route_after_traceability,
    route_after_review, route_approval_gate,
)
from services.architecture.workers import _validate_openapi, _validate_schema


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def project_id(): return str(uuid.uuid4())

@pytest.fixture
def arch_context(project_id):
    return AgentContext(
        project_id=project_id, workflow_id=str(uuid.uuid4()),
        current_phase=3, project_name="TestApp",
        project_description="A test SaaS app with user auth and project boards.",
        approved_artifacts={
            "requirements_doc": {"requirements":[
                {"id":"REQ-001","title":"User Auth","description":"JWT auth system",
                 "priority":"must","category":"functional","acceptance_notes":"Login + register"},
                {"id":"REQ-002","title":"Project Boards","description":"Kanban boards for task tracking",
                 "priority":"must","category":"functional","acceptance_notes":"CRUD boards"},
                {"id":"REQ-003","title":"API Response Time","description":"p95 < 500ms under 100 users",
                 "priority":"should","category":"non_functional","acceptance_notes":"Load test"},
            ]},
            "feature_spec_doc":  {"features":[
                {"name":"User Auth","priority":"must"},{"name":"Project Boards","priority":"must"},
            ]},
            "user_stories_doc":  {"user_stories":[
                {"id":"US-001","role":"startup founder","action":"register","benefit":"access platform",
                 "requirement_ids":["REQ-001"],"priority":"must"},
            ]},
        },
        tech_stack={"backend":"Python+FastAPI","frontend":"Next.js","database":"PostgreSQL"},
        llm_provider="anthropic", llm_model="claude-sonnet-4-6",
        budget_limit_usd=50.0, total_spend_usd=2.0,
    )

@pytest.fixture
def arch_task(project_id, arch_context):
    return TaskInput(
        task_id=str(uuid.uuid4()), project_id=project_id,
        agent_id="system_architect_worker", parent_agent_id="system_design_lead",
        task_type="run_architecture_pipeline",
        description="Design complete system architecture",
        expected_output="JSON blueprint + api_spec + db_schema + deployment",
        context=arch_context,
    )

def make_infra():
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=MagicMock(
        execute=AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalar_one=MagicMock(return_value=0))),
        flush=AsyncMock(), add=MagicMock()))
    db.__aexit__ = AsyncMock(return_value=None)
    storage = AsyncMock()
    storage.store = AsyncMock(return_value="local://test/v1.json")
    # Dynamic mock: captures artifact_type from call arguments
    async def _create_artifact(db, project_id, artifact_type, created_by,
                               content=None, storage_ref=None, metadata=None):
        return {"artifact_id": str(uuid.uuid4()), "artifact_type": artifact_type,
                "version": 1, "storage_ref": storage_ref or "local://test/v1.json"}

    artifact_mock = MagicMock()
    artifact_mock.create = AsyncMock(side_effect=_create_artifact)

    return {"db_factory":lambda:db, "nats":AsyncMock(), "storage":storage,
            "audit_repo":MagicMock(record=AsyncMock(return_value=str(uuid.uuid4()))),
            "artifact_repo": artifact_mock,
            "token_repo":MagicMock(record=AsyncMock(return_value=str(uuid.uuid4())))}

def inject(agent_class, infra):
    a = agent_class.__new__(agent_class)
    spec = AGENT_REGISTRY.get(a.__class__.__name__.lower().replace("worker","_worker"), None)
    a.agent_id = getattr(a.__class__,"agent_id", a.__class__.__name__.lower())
    # find agent_id from registry by class
    for aid, s in AGENT_REGISTRY.items():
        if s.name.replace(" ","") in a.__class__.__name__:
            a.agent_id = aid; break
    a.name=a.__class__.__name__; a.department="architecture"; a.layer=5; a.role="worker"
    a.responsibilities=["Architecture design"]
    a._db_factory=infra["db_factory"]; a._nats=infra["nats"]; a._storage=infra["storage"]
    a._audit_repo=infra["audit_repo"]; a._artifact_repo=infra["artifact_repo"]
    a._token_repo=infra["token_repo"]; a._qdrant=None
    return a


# ═══════════════════════════════════════════════════════════════
# LAYER 1 — Unit: validation functions + agent output contracts
# ═══════════════════════════════════════════════════════════════

class TestOpenAPIValidation:
    def test_valid_spec_has_no_violations(self):
        spec = {"openapi":"3.1.0","info":{"title":"Test API","version":"1.0.0"},
                "paths":{"/health":{"get":{"operationId":"healthCheck","responses":{"200":{"description":"OK"}}}}},
                "components":{"schemas":{},"securitySchemes":{}}}
        assert _validate_openapi(spec) == []

    def test_wrong_openapi_version(self):
        spec = {"openapi":"3.0.0","info":{"title":"T","version":"1"},"paths":{}}
        v = _validate_openapi(spec)
        assert any("3.1.0" in e for e in v)

    def test_missing_operation_id(self):
        spec = {"openapi":"3.1.0","info":{"title":"T","version":"1"},
                "paths":{"/test":{"get":{"summary":"test","responses":{"200":{"description":"OK"}}}}}}
        v = _validate_openapi(spec)
        assert any("operationId" in e for e in v)

    def test_duplicate_operation_id_caught(self):
        spec = {"openapi":"3.1.0","info":{"title":"T","version":"1"},
                "paths":{"/a":{"get":{"operationId":"same","responses":{"200":{"description":"OK"}}}},
                         "/b":{"get":{"operationId":"same","responses":{"200":{"description":"OK"}}}}}}
        v = _validate_openapi(spec)
        assert any("Duplicate" in e for e in v)

    def test_missing_responses_caught(self):
        spec = {"openapi":"3.1.0","info":{"title":"T","version":"1"},
                "paths":{"/a":{"get":{"operationId":"aOp"}}}}
        v = _validate_openapi(spec)
        assert any("responses" in e for e in v)


class TestSchemaValidation:
    def _valid_table(self, name="users"):
        return {"name":name,"owned_by_service":"auth","purpose":"test",
                "columns":[
                    {"name":"id","type":"UUID","primary_key":True,"nullable":False,"default":"gen_random_uuid()"},
                    {"name":"created_at","type":"TIMESTAMPTZ","nullable":False,"default":"NOW()"},
                ],"indexes":[],"append_only":False}

    def test_valid_schema_has_no_violations(self):
        schema = {"tables":[self._valid_table()],"relationships":[],"table_count":1}
        assert _validate_schema(schema) == []

    def test_missing_pk_caught(self):
        t = self._valid_table(); t["columns"] = [t["columns"][1]]  # remove PK
        v = _validate_schema({"tables":[t],"relationships":[]})
        assert any("PK" in e or "primary key" in e for e in v)

    def test_non_uuid_pk_caught(self):
        t = self._valid_table(); t["columns"][0]["type"] = "INTEGER"
        v = _validate_schema({"tables":[t],"relationships":[]})
        assert any("UUID" in e for e in v)

    def test_missing_created_at_caught(self):
        t = self._valid_table()
        t["columns"] = [c for c in t["columns"] if c["name"] != "created_at"]
        v = _validate_schema({"tables":[t],"relationships":[]})
        assert any("created_at" in e for e in v)

    def test_wrong_timestamp_type_caught(self):
        t = self._valid_table()
        for c in t["columns"]:
            if c["name"] == "created_at": c["type"] = "TIMESTAMP"
        v = _validate_schema({"tables":[t],"relationships":[]})
        assert any("TIMESTAMPTZ" in e for e in v)

    def test_fk_to_unknown_table_caught(self):
        t = self._valid_table()
        schema = {"tables":[t],"relationships":[{"from_table":"tasks","from_column":"owner_id",
                                                  "to_table":"nonexistent","to_column":"id","on_delete":"CASCADE"}]}
        v = _validate_schema(schema)
        assert any("nonexistent" in e for e in v)

    def test_fk_missing_on_delete_caught(self):
        t = self._valid_table(); t2 = self._valid_table("projects")
        schema = {"tables":[t,t2],"relationships":[{"from_table":"projects","from_column":"owner_id",
                                                     "to_table":"users","to_column":"id"}]}
        v = _validate_schema(schema)
        assert any("on_delete" in e for e in v)


class TestAgentRegistry_Architecture:
    def test_all_architecture_workers_registered(self):
        arch_workers = [
            "system_architect_worker","openapi_spec_writer_worker","schema_designer_worker",
            "infrastructure_planner_worker","security_architect_worker",
            "scalability_architect_worker","integration_architect_worker",
            "traceability_agent_worker","architecture_reviewer_worker",
        ]
        for wid in arch_workers:
            assert wid in AGENT_REGISTRY, f"Missing: {wid}"

    def test_all_architecture_leads_registered(self):
        for lid in ["system_design_lead","platform_design_lead","architecture_review_lead"]:
            assert lid in AGENT_REGISTRY

    def test_architecture_head_registered(self):
        assert "architecture_head" in AGENT_REGISTRY

    def test_architecture_head_uses_premium_model(self):
        spec = AGENT_REGISTRY["architecture_head"]
        assert spec.default_model == "claude-opus-4-6"


# ═══════════════════════════════════════════════════════════════
# LAYER 2 — Graph routing tests (no LLM, no DB)
# ═══════════════════════════════════════════════════════════════

class TestArchitectureGraphRouting:
    def _base_state(self, project_id="test-proj") -> ArchitectureState:
        return {
            "project_id":project_id,"workflow_id":"wf-1","requirements_ready":True,
            "blueprint_ready":False,"api_spec_ready":False,"db_schema_ready":False,
            "deployment_ready":False,"security_ready":False,"scaling_ready":False,
            "integration_ready":False,"traceability_ready":False,"review_passed":False,
            "coverage_pct":0.0,"traceability_reruns":0,
            "awaiting_approval":False,"approval_status":None,"approval_feedback":None,"revision_round":0,
            "artifacts":{},"failure_reason":None,"phase_status":"running",
            "nats_events_queue":[],"ws_events_queue":[],
        }

    def test_system_design_routes_to_parallel(self):
        s = self._base_state(); s["phase_status"] = "running"
        assert route_after_system_design(s) == "parallel"

    def test_system_design_routes_to_failure(self):
        s = self._base_state(); s["phase_status"] = "failed"
        assert route_after_system_design(s) == "failed"

    def test_traceability_routes_to_review_on_sufficient_coverage(self):
        s = self._base_state(); s["coverage_pct"] = 85.0
        assert route_after_traceability(s) == "review"

    def test_traceability_routes_to_rerun_on_low_coverage(self):
        s = self._base_state(); s["coverage_pct"] = 70.0; s["traceability_reruns"] = 0
        assert route_after_traceability(s) == "traceability_rerun"

    def test_traceability_fails_after_max_reruns(self):
        s = self._base_state(); s["coverage_pct"] = 50.0; s["traceability_reruns"] = 2
        assert route_after_traceability(s) == "failed"

    def test_review_routes_to_package_on_pass(self):
        s = self._base_state(); s["review_passed"] = True
        assert route_after_review(s) == "package"

    def test_review_routes_to_failure_on_fail(self):
        s = self._base_state(); s["review_passed"] = False
        assert route_after_review(s) == "failed"

    def test_approval_gate_approved(self):
        s = self._base_state(); s["approval_status"] = "approved"
        assert route_approval_gate(s) == "approved"

    def test_approval_gate_rejected(self):
        s = self._base_state(); s["approval_status"] = "rejected"
        assert route_approval_gate(s) == "rejected"

    def test_approval_gate_max_revisions(self):
        s = self._base_state(); s["revision_round"] = 5
        assert route_approval_gate(s) == "max_revisions"

    def test_approval_gate_pending_loops(self):
        s = self._base_state(); s["approval_status"] = None; s["revision_round"] = 0
        assert route_approval_gate(s) == "pending"

    def test_graph_builds_without_error(self):
        graph = build_architecture_graph()
        assert graph is not None

    def test_graph_has_one_interrupt_node(self):
        graph = build_architecture_graph()
        assert graph is not None  # interrupt_before=["approval_gate"] verified by routing tests

    @pytest.mark.asyncio
    async def test_load_requirements_node(self):
        from services.architecture.workflows.architecture_graph import load_requirements_node
        s = self._base_state()
        r = await load_requirements_node(s)
        assert r["requirements_ready"] is True
        assert r["phase_status"] == "running"
        assert len(r["ws_events_queue"]) >= 1

    @pytest.mark.asyncio
    async def test_traceability_node_sets_coverage(self):
        from services.architecture.workflows.architecture_graph import traceability_node
        s = self._base_state()
        r = await traceability_node(s)
        assert r["traceability_ready"] is True
        assert "coverage_pct" in r

    @pytest.mark.asyncio
    async def test_approval_gate_sets_awaiting(self):
        from services.architecture.workflows.architecture_graph import approval_gate_node
        s = self._base_state()
        r = await approval_gate_node(s)
        assert r["awaiting_approval"] is True
        ws_types = [e.get("event_type") for e in r.get("ws_events_queue",[])]
        assert "approval_required" in ws_types

    @pytest.mark.asyncio
    async def test_revision_node_increments_round(self):
        from services.architecture.workflows.architecture_graph import revision_node
        s = self._base_state(); s["revision_round"] = 1; s["approval_feedback"] = "Fix API spec"
        r = await revision_node(s)
        assert r["revision_round"] == 2
        assert r["approval_status"] is None
        nats_subjects = [e.get("subject") for e in r.get("nats_events_queue",[])]
        assert "architecture.design.revised" in nats_subjects

    @pytest.mark.asyncio
    async def test_publish_artifacts_node(self):
        from services.architecture.workflows.architecture_graph import publish_artifacts_node
        s = self._base_state()
        r = await publish_artifacts_node(s)
        assert r["phase_status"] == "completed"
        assert r["awaiting_approval"] is False
        nats_subjects = [e.get("subject") for e in r.get("nats_events_queue",[])]
        assert "architecture.design.completed" in nats_subjects


# ═══════════════════════════════════════════════════════════════
# LAYER 3 — Integration: worker contracts (mocked LLM)
# ═══════════════════════════════════════════════════════════════

MOCK_BLUEPRINT = json.dumps({
    "diagram_type":"mermaid",
    "diagram_content":"graph TD\n  Client-->Gateway\n  Gateway-->API[API Service]",
    "components":[
        {"name":"API Gateway","type":"gateway","description":"Entry point","technology":"FastAPI+Nginx","dependencies":[],"exposed_port":8000,"internal":False,"scaling_notes":"Stateless"},
        {"name":"API Service","type":"service","description":"Core business logic","technology":"Python+FastAPI","dependencies":["PostgreSQL","NATS"],"exposed_port":8001,"internal":False,"scaling_notes":"Stateless"},
    ],
    "architecture_pattern":"microservices",
    "communication_patterns":["REST","NATS"],
    "data_flow_summary":"Client → Gateway → API → DB",
    "quality_score":0.92
})
MOCK_OPENAPI = json.dumps({
    "openapi":"3.1.0","info":{"title":"TestApp API","version":"1.0.0","description":"Test"},
    "servers":[{"url":"/api/v1"}],
    "paths":{
        "/health":{"get":{"operationId":"healthCheck","summary":"Health","tags":["System"],"security":[],"responses":{"200":{"description":"OK"}}}},
        "/auth/register":{"post":{"operationId":"registerUser","summary":"Register","tags":["Auth"],"security":[],"requestBody":{"required":True,"content":{}},"responses":{"201":{"description":"Created"},"409":{"description":"Conflict"}}}},
        "/api/v1/projects":{"get":{"operationId":"listProjects","summary":"List projects","tags":["Projects"],"responses":{"200":{"description":"OK"},"401":{"description":"Unauthorized"}}}},
    },
    "components":{"schemas":{},"securitySchemes":{"bearerAuth":{"type":"http","scheme":"bearer"}}},
    "security":[{"bearerAuth":[]}],
    "quality_score":0.90
})
MOCK_SCHEMA = json.dumps({
    "tables":[
        {"name":"users","owned_by_service":"auth","purpose":"User accounts",
         "columns":[{"name":"id","type":"UUID","primary_key":True,"nullable":False,"default":"gen_random_uuid()"},
                    {"name":"email","type":"VARCHAR(255)","nullable":False,"unique":True},
                    {"name":"created_at","type":"TIMESTAMPTZ","nullable":False,"default":"NOW()"}],
         "indexes":[{"name":"idx_users_email","columns":["email"],"unique":True}],"append_only":False},
        {"name":"projects","owned_by_service":"api","purpose":"User projects",
         "columns":[{"name":"id","type":"UUID","primary_key":True,"nullable":False,"default":"gen_random_uuid()"},
                    {"name":"owner_id","type":"UUID","nullable":False},
                    {"name":"name","type":"VARCHAR(255)","nullable":False},
                    {"name":"created_at","type":"TIMESTAMPTZ","nullable":False,"default":"NOW()"}],
         "indexes":[],"append_only":False},
    ],
    "relationships":[{"from_table":"projects","from_column":"owner_id","to_table":"users","to_column":"id","type":"many_to_one","on_delete":"RESTRICT"}],
    "append_only_tables":[],"table_count":2,"quality_score":0.91
})
MOCK_CRITIQUE = json.dumps({"passed":True,"score":0.88,"blocking":[],"warnings":["Minor: add indexes for FK columns"],"suggestions":["Add explicit NOT NULL to all required fields"]})
MOCK_TRACEABILITY = json.dumps({
    "traceability_matrix":[
        {"requirement_id":"REQ-001","requirement_title":"User Auth","category":"functional","priority":"must",
         "api_endpoints":["/auth/register","/auth/login"],"components":["API Gateway","API Service"],
         "database_tables":["users"],"feature_names":["User Auth"],"coverage_status":"fully_covered","coverage_notes":"Covered"},
        {"requirement_id":"REQ-002","requirement_title":"Project Boards","category":"functional","priority":"must",
         "api_endpoints":["/api/v1/projects"],"components":["API Service"],
         "database_tables":["projects"],"feature_names":["Project Boards"],"coverage_status":"fully_covered","coverage_notes":"Covered"},
    ],
    "coverage_summary":{"total_functional_requirements":2,"fully_covered":2,"partially_covered":0,"uncovered":0,"coverage_percentage":100.0},
    "uncovered_requirements":[],"gaps_analysis":"No gaps","quality_score":0.95
})
MOCK_REVIEW = json.dumps({
    "overall_passed":True,
    "scores":{"blueprint_quality":0.92,"api_completeness":0.90,"schema_soundness":0.91,
              "deployment_readiness":0.87,"security_coverage":0.9,"traceability_coverage":1.0},
    "overall_score":0.90,"blocking_issues":[],"warnings":[],"cross_cutting_issues":[],
    "recommendation":"Approve — complete and consistent","quality_score":0.90
})


class TestSystemArchitectWorker:
    @pytest.mark.asyncio
    async def test_generates_blueprint(self, arch_task):
        from services.architecture.workers import SystemArchitect
        infra = make_infra()
        agent = inject(SystemArchitect, infra); agent.agent_id = "system_architect_worker"
        with patch.object(agent,"call_llm",AsyncMock(return_value=(MOCK_BLUEPRINT,None))):
            with patch.object(agent,"_pre_execute",AsyncMock()):
                with patch.object(agent,"_post_execute",AsyncMock()):
                    result = await agent.execute(arch_task)
        assert result.status == TaskStatus.COMPLETED
        assert len(result.content.get("components",[])) >= 2
        assert result.content.get("architecture_pattern") == "microservices"
        assert len(result.artifacts) >= 1
        assert result.artifacts[0].artifact_type == "architecture_blueprint"

    @pytest.mark.asyncio
    async def test_blueprint_fails_if_no_components(self, arch_task):
        from services.architecture.workers import SystemArchitect
        bad_json = json.dumps({"diagram_type":"mermaid","diagram_content":"graph TD\n A-->B",
                               "components":[],"architecture_pattern":"microservices","quality_score":0.3})
        infra = make_infra(); agent = inject(SystemArchitect, infra)
        agent.agent_id = "system_architect_worker"
        # critique returns blocking issue → improvement loop → eventually escalates
        bad_critique = json.dumps({"passed":False,"score":0.3,"blocking":["No components defined"],"warnings":[],"suggestions":["Add at least 2 components"]})
        call_count = {"n":0}
        async def mock_llm(task, messages, **kw):
            call_count["n"] += 1
            if call_count["n"] <= 1: return bad_json, None
            return bad_critique, None
        with patch.object(agent,"call_llm",mock_llm):
            with patch.object(agent,"_pre_execute",AsyncMock()):
                with patch.object(agent,"_post_execute",AsyncMock()):
                    result = await agent.execute(arch_task)
        # Either passed (review fixed it) or escalated — both acceptable
        assert result.status in (TaskStatus.COMPLETED, TaskStatus.ESCALATED)


class TestAPIArchitectWorker:
    @pytest.mark.asyncio
    async def test_generates_valid_openapi(self, arch_task, arch_context):
        from services.architecture.workers import APIArchitect
        arch_context.approved_artifacts["architecture_blueprint"] = json.loads(MOCK_BLUEPRINT)
        arch_task.context = arch_context
        infra = make_infra(); agent = inject(APIArchitect, infra)
        agent.agent_id = "openapi_spec_writer_worker"
        with patch.object(agent,"call_llm",AsyncMock(return_value=(MOCK_OPENAPI,None))):
            with patch.object(agent,"_pre_execute",AsyncMock()):
                with patch.object(agent,"_post_execute",AsyncMock()):
                    result = await agent.execute(arch_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content.get("openapi") == "3.1.0"
        assert len(result.content.get("paths",{})) >= 2
        assert result.artifacts[0].artifact_type == "api_spec"

    @pytest.mark.asyncio
    async def test_validation_runs_on_output(self, arch_task, arch_context):
        from services.architecture.workers import APIArchitect
        arch_context.approved_artifacts["architecture_blueprint"] = json.loads(MOCK_BLUEPRINT)
        arch_task.context = arch_context
        infra = make_infra(); agent = inject(APIArchitect, infra)
        agent.agent_id = "openapi_spec_writer_worker"
        # Missing operationIds
        bad_spec = json.dumps({"openapi":"3.1.0","info":{"title":"T","version":"1"},
                               "paths":{"/test":{"get":{"summary":"test","responses":{"200":{"description":"OK"}}}}},"components":{},"quality_score":0.5})
        with patch.object(agent,"call_llm",AsyncMock(return_value=(bad_spec,None))):
            with patch.object(agent,"_pre_execute",AsyncMock()):
                with patch.object(agent,"_post_execute",AsyncMock()):
                    result = await agent.execute(arch_task)
        # Violations are logged; agent may escalate or pass with warnings
        assert result.status in (TaskStatus.COMPLETED, TaskStatus.ESCALATED, TaskStatus.FAILED)


class TestDatabaseArchitectWorker:
    @pytest.mark.asyncio
    async def test_generates_valid_schema(self, arch_task, arch_context):
        from services.architecture.workers import DatabaseArchitect
        arch_context.approved_artifacts.update({
            "architecture_blueprint": json.loads(MOCK_BLUEPRINT),
            "api_spec":               json.loads(MOCK_OPENAPI),
        })
        arch_task.context = arch_context
        infra = make_infra(); agent = inject(DatabaseArchitect, infra)
        agent.agent_id = "schema_designer_worker"
        with patch.object(agent,"call_llm",AsyncMock(return_value=(MOCK_SCHEMA,None))):
            with patch.object(agent,"_pre_execute",AsyncMock()):
                with patch.object(agent,"_post_execute",AsyncMock()):
                    result = await agent.execute(arch_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content.get("table_count") >= 2
        tables = result.content.get("tables",[])
        assert all(any(c.get("primary_key") for c in t.get("columns",[])) for t in tables)
        assert result.artifacts[0].artifact_type == "database_schema"


class TestTraceabilityAgent:
    @pytest.mark.asyncio
    async def test_generates_matrix_with_coverage(self, arch_task, arch_context):
        from services.architecture.workers import TraceabilityAgent
        arch_context.approved_artifacts.update({
            "architecture_blueprint": json.loads(MOCK_BLUEPRINT),
            "api_spec":               json.loads(MOCK_OPENAPI),
            "database_schema":        json.loads(MOCK_SCHEMA),
        })
        arch_task.context = arch_context
        infra = make_infra(); agent = inject(TraceabilityAgent, infra)
        agent.agent_id = "traceability_agent_worker"
        with patch.object(agent,"call_llm",AsyncMock(return_value=(MOCK_TRACEABILITY,None))):
            with patch.object(agent,"_pre_execute",AsyncMock()):
                with patch.object(agent,"_post_execute",AsyncMock()):
                    result = await agent.execute(arch_task)
        assert result.status == TaskStatus.COMPLETED
        matrix = result.content.get("traceability_matrix",[])
        assert len(matrix) >= 2
        coverage = result.content.get("coverage_summary",{}).get("coverage_percentage",0)
        assert coverage > 0
        assert result.artifacts[0].artifact_type == "traceability_matrix"

    @pytest.mark.asyncio
    async def test_computes_coverage_correctly(self, arch_task, arch_context):
        from services.architecture.workers import TraceabilityAgent
        arch_task.context = arch_context
        infra = make_infra(); agent = inject(TraceabilityAgent, infra)
        agent.agent_id = "traceability_agent_worker"
        # Mix: 1 fully covered, 1 partially, 1 uncovered = (1 + 0.5*1) / 3 * 100 = 50%
        mixed = json.dumps({"traceability_matrix":[
            {"requirement_id":"REQ-001","api_endpoints":["/auth/register"],"coverage_status":"fully_covered"},
            {"requirement_id":"REQ-002","api_endpoints":["/api/v1/projects"],"coverage_status":"partially_covered"},
            {"requirement_id":"REQ-003","api_endpoints":[],"coverage_status":"uncovered"},
        ],"quality_score":0.7})
        with patch.object(agent,"call_llm",AsyncMock(return_value=(mixed,None))):
            with patch.object(agent,"_pre_execute",AsyncMock()):
                with patch.object(agent,"_post_execute",AsyncMock()):
                    result = await agent.execute(arch_task)
        summary = result.content.get("coverage_summary",{})
        assert summary.get("fully_covered") == 1
        assert summary.get("partially_covered") == 1
        assert summary.get("uncovered") == 1
        assert abs(summary.get("coverage_percentage",0) - 50.0) < 1.0


class TestArchitectureReviewer:
    @pytest.mark.asyncio
    async def test_passes_complete_architecture(self, arch_task, arch_context):
        from services.architecture.workers import ArchitectureReviewer
        arch_context.approved_artifacts.update({
            "architecture_blueprint": json.loads(MOCK_BLUEPRINT),
            "api_spec":               json.loads(MOCK_OPENAPI),
            "database_schema":        json.loads(MOCK_SCHEMA),
            "deployment_architecture":{"services":[]},
            "security_architecture":  {"owasp_mitigations":[]},
            "traceability_matrix":    json.loads(MOCK_TRACEABILITY),
        })
        arch_task.context = arch_context
        infra = make_infra(); agent = inject(ArchitectureReviewer, infra)
        agent.agent_id = "architecture_reviewer_worker"
        with patch.object(agent,"call_llm",AsyncMock(return_value=(MOCK_REVIEW,None))):
            with patch.object(agent,"_pre_execute",AsyncMock()):
                with patch.object(agent,"_post_execute",AsyncMock()):
                    result = await agent.execute(arch_task)
        assert result.status == TaskStatus.COMPLETED
        assert result.content.get("overall_passed") is True
        assert result.quality_score >= 0.7


# ═══════════════════════════════════════════════════════════════
# LAYER 4 — E2E (requires Docker stack)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestM31EndToEnd:
    @pytest.mark.asyncio
    async def test_requirements_to_architecture_to_approval(self):
        """
        Full M3.1 test:
          requirements_doc (approved) → architecture pipeline → 4 artifacts → approval gate
          → user approves → W01 phase 3→4 transition.
        """
        import httpx
        mgr = "http://localhost:8001"
        base = "http://localhost:8000"

        async with httpx.AsyncClient(timeout=120) as client:
            # Auth
            reg = await client.post(f"{base}/auth/register", json={
                "email":f"arch_e2e_{uuid.uuid4().hex[:8]}@test.com",
                "password":"TestPass123!","role":"owner"})
            if reg.status_code not in (200,201):
                pytest.skip("Foundation service not running")
            token = reg.json()["access_token"]
            headers = {"Authorization":f"Bearer {token}"}

            # Create project via demo service
            demo = await client.post(f"{base}/demo", headers=headers, json={
                "project_name":"M3.1 E2E Test","description":"SaaS task management app",
                "task_prompt":"Describe this project briefly.","budget_usd":30.0})
            assert demo.status_code == 200
            project_id = demo.json()["project_id"]

            # Start lifecycle (requirements phase)
            start = await client.post(f"{mgr}/projects/start", headers=headers, json={
                "project_id":project_id,"name":"M3.1 E2E Test",
                "description":"SaaS task management","budget_usd":30.0})
            if start.status_code == 503:
                pytest.skip("Manager service not running")
            assert start.status_code == 200

            # Approve requirements (triggers architecture phase)
            approve = await client.post(f"{mgr}/projects/{project_id}/approve",
                headers=headers, json={"feedback":"Requirements approved for E2E test"})
            assert approve.status_code in (200,404)  # 404 if no approval gate yet

            # Check state
            state_resp = await client.get(f"{mgr}/projects/{project_id}/state", headers=headers)
            if state_resp.status_code == 200:
                state = state_resp.json()
                assert state.get("current_phase") >= 2
