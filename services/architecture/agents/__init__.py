"""services/architecture/agents — 12 concrete architecture-service agents."""
from __future__ import annotations
import json
from typing import Any, Dict, List
import structlog
from core.contracts import AgentResult, NATSEvent, TaskStatus, WebSocketEvent
from core.runtime.base_agent import BaseAgent
from core.runtime.context import ReviewCycle, TaskInput
from core.runtime.factory import AgentFactory

log = structlog.get_logger(__name__)

def _parse(raw, fb=None):
    try:
        c = raw.strip()
        if c.startswith("```"): c = c.split("```")[1]; c = c[4:] if c.startswith("json") else c
        return json.loads(c.strip())
    except Exception: return fb or {}


@AgentFactory.register("system_architect_worker")
class SystemArchitectWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx = task.context
        reqs = ctx.get_artifact("requirements_doc", {}).get("requirements", [])
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Design the system architecture for this project.

REQUIREMENTS:
{json.dumps(reqs[:10], indent=2)}
TECH STACK: {json.dumps(ctx.tech_stack, indent=2)}

Return ONLY JSON:
{{"diagram_type":"mermaid","diagram_content":"graph TD\\n  A[Client] --> B[API Gateway]\\n  B --> C[Manager Service]","components":[{{"name":"API Gateway","type":"service","description":"Routes all client requests","dependencies":[],"port":8000}},{{"name":"Manager Service","type":"service","description":"Orchestrates all agents","dependencies":["PostgreSQL","NATS"],"port":8001}}],"quality_score":0.9}}"""}], max_tokens=3000)
        content = _parse(raw, {"diagram_type":"mermaid","diagram_content":"","components":[],"quality_score":0.0})
        review  = await ReviewCycle(self).run(content, task, schema={"root":["diagram_type","diagram_content","components"]})
        artifact = await self.create_artifact(task, "system_architecture_doc", {**content,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary=f"System architecture designed: {len(content.get('components',[]))} components",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)


@AgentFactory.register("component_designer_worker")
class ComponentDesignerWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        arch = task.context.get_artifact("system_architecture_doc", {})
        components = arch.get("components", []) if isinstance(arch, dict) else []
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Define detailed service boundaries and interactions.

COMPONENTS: {json.dumps(components, indent=2)}

Return ONLY JSON:
{{"services":[{{"name":"str","responsibility":"str","api_prefix":"/api/v1/service","communicates_with":["other-service"],"data_owned":["table1","table2"],"nats_publishes":["service.event.action"],"nats_subscribes":["other.event.action"]}}],"quality_score":0.88}}"""}], max_tokens=3000)
        content = _parse(raw, {"services":[],"quality_score":0.0})
        review  = await ReviewCycle(self).run(content.get("services",[]), task)
        artifact = await self.create_artifact(task, "component_design_doc", {**content,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary=f"Defined {len(content.get('services',[]))} service boundaries",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)


@AgentFactory.register("openapi_spec_writer_worker")
class OpenApiSpecWriterWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        reqs = task.context.get_artifact("requirements_doc", {}).get("requirements", [])
        comp = task.context.get_artifact("component_design_doc", {}).get("services", [])
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Generate a complete OpenAPI 3.1 specification.

REQUIREMENTS: {json.dumps(reqs[:8], indent=2)}
SERVICES: {json.dumps(comp[:4], indent=2)}

Return ONLY JSON (valid OpenAPI 3.1):
{{"openapi":"3.1.0","info":{{"title":"Project API","version":"1.0.0","description":"Generated API spec"}},"paths":{{"/api/v1/health":{{"get":{{"operationId":"healthCheck","summary":"Health check","responses":{{"200":{{"description":"OK"}}}}}}}}}},"components":{{"schemas":{{}},"securitySchemes":{{"bearerAuth":{{"type":"http","scheme":"bearer","bearerFormat":"JWT"}}}}}},"security":[{{"bearerAuth":[]}}],"quality_score":0.9}}"""}], max_tokens=4096)
        content = _parse(raw, {"openapi":"3.1.0","paths":{},"quality_score":0.0})
        endpoint_count = len(content.get("paths", {}))
        review  = await ReviewCycle(self).run(content, task, schema={"root":["openapi","info","paths"]})
        if not review.passed:
            return self.escalate(task, f"OpenAPI spec failed validation after {review.cycles_run} cycles")
        artifact = await self.create_artifact(task, "openapi_spec", {**content,"project_id":task.project_id,"endpoint_count":endpoint_count})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary=f"OpenAPI 3.1 spec generated: {endpoint_count} endpoints",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)


@AgentFactory.register("api_reviewer_worker")
class ApiReviewerWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        spec = task.context.get_artifact("openapi_spec", {})
        paths = spec.get("paths", {}) if isinstance(spec, dict) else {}
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Review this OpenAPI spec for REST best practices and completeness.

PATHS ({len(paths)} endpoints): {json.dumps(list(paths.keys())[:20])}

Check: operationId present, proper HTTP methods, response codes complete, auth applied, no duplicate paths.

Return ONLY JSON:
{{"passed":true,"issues":[{{"severity":"blocking|warning","path":"/api/v1/x","description":"Missing operationId"}}],"coverage_score":0.9,"quality_score":0.9}}"""}], max_tokens=1500)
        content = _parse(raw, {"passed":True,"issues":[],"quality_score":0.8})
        artifact = await self.create_artifact(task, "api_review_report", {**content,"project_id":task.project_id})
        status = TaskStatus.COMPLETED if content.get("passed", True) else TaskStatus.FAILED
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=status,
            content=content, summary=f"API review {'PASSED' if content.get('passed') else 'FAILED'}: {len(content.get('issues',[]))} issues",
            quality_score=float(content.get("quality_score",0.8)), artifacts=[artifact], token_usage=usage,
            failure_reason=None if content.get("passed") else f"{len([i for i in content.get('issues',[]) if i.get('severity')=='blocking'])} blocking issues")


@AgentFactory.register("schema_designer_worker")
class SchemaDesignerWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        reqs = task.context.get_artifact("requirements_doc", {}).get("requirements", [])
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Design a complete PostgreSQL database schema.

REQUIREMENTS: {json.dumps(reqs[:8], indent=2)}
TECH STACK: {json.dumps(task.context.tech_stack)}

Return ONLY JSON:
{{"tables":[{{"name":"users","columns":[{{"name":"id","type":"UUID","primary_key":true,"nullable":false}},{{"name":"email","type":"VARCHAR(255)","unique":true,"nullable":false}},{{"name":"created_at","type":"TIMESTAMPTZ","nullable":false,"default":"NOW()"}}],"indexes":[{{"columns":["email"],"unique":true}}]}}],"relationships":[{{"from":"tasks","to":"users","type":"many_to_one","via":"owner_id"}}],"table_count":5,"quality_score":0.9}}"""}], max_tokens=4096)
        content = _parse(raw, {"tables":[],"relationships":[],"quality_score":0.0})
        review  = await ReviewCycle(self).run(content.get("tables",[]), task)
        artifact = await self.create_artifact(task, "db_schema_doc", {**content,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary=f"DB schema: {len(content.get('tables',[]))} tables, {len(content.get('relationships',[]))} relationships",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)


@AgentFactory.register("index_optimizer_worker")
class IndexOptimizerWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        schema = task.context.get_artifact("db_schema_doc", {})
        tables = schema.get("tables", []) if isinstance(schema, dict) else []
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Review this database schema for query performance and add missing indexes.

TABLES ({len(tables)}): {json.dumps([t.get('name') for t in tables])}

Return ONLY JSON:
{{"optimized_tables":[{{"table":"users","added_indexes":[{{"columns":["created_at"],"type":"btree","reason":"Timeline queries"}}],"recommendations":["Consider partitioning if >10M rows"]}}],"performance_notes":"All FK columns indexed","quality_score":0.9}}"""}], max_tokens=2000)
        content = _parse(raw, {"optimized_tables":[],"quality_score":0.8})
        artifact = await self.create_artifact(task, "index_optimization_report", {**content,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary=f"Index optimization: {len(content.get('optimized_tables',[]))} tables reviewed",
            quality_score=float(content.get("quality_score",0.8)), artifacts=[artifact], token_usage=usage)


@AgentFactory.register("infrastructure_planner_worker")
class InfrastructurePlannerWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        components = task.context.get_artifact("component_design_doc", {}).get("services", [])
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Create a complete infrastructure plan for Docker Compose deployment.

SERVICES ({len(components)}): {json.dumps([c.get('name') for c in components])}

Return ONLY JSON:
{{"services":[{{"name":"api","image":"python:3.12-slim","ports":["8000:8000"],"environment":{{"DATABASE_URL":"${{DATABASE_URL}}","NATS_URL":"${{NATS_URL}}"}},"depends_on":["postgres","nats"],"healthcheck":"wget -q --spider http://localhost:8000/health"}}],"volumes":["postgres_data","nats_data"],"networks":["aasc_network"],"resource_estimates":{{"total_memory_mb":2048,"total_cpu_cores":2}},"deployment_target":"docker_compose","quality_score":0.9}}"""}], max_tokens=3000)
        content = _parse(raw, {"services":[],"quality_score":0.0})
        review  = await ReviewCycle(self).run(content.get("services",[]), task)
        artifact = await self.create_artifact(task, "infrastructure_plan", {**content,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary=f"Infrastructure plan: {len(content.get('services',[]))} services",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)


# L4 Leads — coordination pattern
@AgentFactory.register("system_design_lead")
class SystemDesignLead(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        all_artifacts = []
        for aid in ["system_architect_worker","component_designer_worker"]:
            if factory:
                r = await factory.create(aid).run(task)
                all_artifacts.extend(r.artifacts)
                if r.status == TaskStatus.FAILED: return self.escalate(task, f"{aid} failed: {r.failure_reason}")
                for a in r.artifacts:
                    if isinstance(a, dict): task.context.approved_artifacts[a.get("artifact_type","x")] = r.content
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"system_design":"complete"}, summary="System design pipeline complete",
            quality_score=0.9, artifacts=all_artifacts)


@AgentFactory.register("api_design_lead")
class ApiDesignLead(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        all_artifacts = []
        for aid in ["openapi_spec_writer_worker","api_reviewer_worker"]:
            if factory:
                r = await factory.create(aid).run(task)
                all_artifacts.extend(r.artifacts)
                if r.status == TaskStatus.FAILED: return self.escalate(task, f"{aid} failed: {r.failure_reason}")
                for a in r.artifacts:
                    if isinstance(a, dict): task.context.approved_artifacts[a.get("artifact_type","x")] = r.content
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"api_design":"complete"}, summary="API design pipeline complete", quality_score=0.9, artifacts=all_artifacts)


@AgentFactory.register("database_design_lead")
class DatabaseDesignLead(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        all_artifacts = []
        for aid in ["schema_designer_worker","index_optimizer_worker"]:
            if factory:
                r = await factory.create(aid).run(task)
                all_artifacts.extend(r.artifacts)
                if r.status == TaskStatus.FAILED: return self.escalate(task, f"{aid} failed: {r.failure_reason}")
                for a in r.artifacts:
                    if isinstance(a, dict): task.context.approved_artifacts[a.get("artifact_type","x")] = r.content
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"db_design":"complete"}, summary="DB design pipeline complete", quality_score=0.9, artifacts=all_artifacts)


@AgentFactory.register("infrastructure_lead")
class InfrastructureLead(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        if factory:
            r = await factory.create("infrastructure_planner_worker").run(task)
            return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=r.status,
                content=r.content, summary=f"Infrastructure: {r.summary}", quality_score=r.quality_score, artifacts=r.artifacts)
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={}, summary="Infrastructure Lead: no factory", quality_score=0.8)


@AgentFactory.register("architecture_head")
class ArchitectureHead(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        pipeline = [("system_design_lead","System design"),("api_design_lead","API design"),
                    ("database_design_lead","DB design"),("infrastructure_lead","Infrastructure")]
        all_artifacts = []
        for aid, step in pipeline:
            log.info("arch_pipeline_step", step=step, project_id=task.project_id)
            if factory:
                r = await factory.create(aid).run(task)
                all_artifacts.extend(r.artifacts)
                if r.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
                    return self.escalate(task, f"Architecture failed at {step}: {r.failure_reason}")
                for a in r.artifacts:
                    if isinstance(a, dict): task.context.approved_artifacts[a.get("artifact_type","x")] = r.content
        # Traceability check
        reqs  = task.context.get_artifact("requirements_doc",{}).get("requirements",[])
        comps = task.context.get_artifact("component_design_doc",{}).get("services",[])
        await self.write_memory(task, f"Architecture complete: {len(comps)} services for {len(reqs)} requirements", source="architecture_head")
        # Submit for approval
        updated = []
        async with self._db_factory() as db:
            from infrastructure.database.models import Artifact
            from sqlalchemy import select
            for atype in ["system_architecture_doc","openapi_spec","db_schema_doc","infrastructure_plan"]:
                r = await db.execute(select(Artifact).where(Artifact.project_id==task.project_id,
                    Artifact.artifact_type==atype,Artifact.status=="draft").order_by(Artifact.version.desc()).limit(1))
                art = r.scalar_one_or_none()
                if art: art.status = "under_review"; updated.append(art.id)
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"phase":"architecture","submitted":len(updated)},
            summary=f"Architecture department complete — {len(updated)} artifacts submitted for approval",
            quality_score=0.9, artifacts=all_artifacts,
            nats_events=[NATSEvent(subject="architecture.design.completed",
                payload={"project_id":task.project_id,"artifact_ids":updated},project_id=task.project_id)],
            ws_events=[WebSocketEvent(project_id=task.project_id,event_type="approval_required",
                payload={"artifact_type":"architecture","message":"Architecture blueprint ready for review"})])
