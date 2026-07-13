"""services/architecture/workers — 9 L5 worker implementations for M3.1."""
from __future__ import annotations
import json
from typing import Any, Dict, List
import structlog
from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import ReviewCycle, TaskInput
from core.runtime.factory import AgentFactory
log = structlog.get_logger(__name__)

def _j(raw, fb=None):
    try:
        c = raw.strip()
        if c.startswith("```"):
            p = c.split("```"); c = p[1] if len(p)>1 else c
            if c.startswith("json"): c = c[4:]
        return json.loads(c.strip())
    except Exception: return fb if fb is not None else {}

def _validate_openapi(spec):
    v = []
    if spec.get("openapi") != "3.1.0": v.append("openapi must be '3.1.0'")
    if not spec.get("info",{}).get("title"): v.append("info.title required")
    seen = set()
    for path, meths in spec.get("paths",{}).items():
        for meth, op in meths.items():
            if not isinstance(op,dict): continue
            oid = op.get("operationId")
            if not oid: v.append(f"Missing operationId: {meth.upper()} {path}")
            elif oid in seen: v.append(f"Duplicate operationId: {oid}")
            else: seen.add(oid)
            if not op.get("responses"): v.append(f"No responses: {meth.upper()} {path}")
    return v

def _validate_schema(schema):
    v = []; tnames = {t.get("name") for t in schema.get("tables",[])}
    for t in schema.get("tables",[]):
        cols = {c.get("name"):c for c in t.get("columns",[])}
        pks  = [c for c in t.get("columns",[]) if c.get("primary_key")]
        if not pks: v.append(f"'{t.get('name')}' has no PK")
        for pk in pks:
            if "UUID" not in pk.get("type","").upper(): v.append(f"PK in '{t.get('name')}' must be UUID")
        if "created_at" not in cols: v.append(f"'{t.get('name')}' missing created_at")
        elif "TIMESTAMPTZ" not in cols["created_at"].get("type","").upper():
            v.append(f"'{t.get('name')}'.created_at must be TIMESTAMPTZ")
    for r in schema.get("relationships",[]):
        if r.get("to_table") not in tnames: v.append(f"FK → unknown table '{r.get('to_table')}'")
        if not r.get("on_delete"): v.append(f"FK from '{r.get('from_table')}' missing on_delete")
    return v


@AgentFactory.register("system_architect_worker")
class SystemArchitect(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx   = task.context
        reqs  = ctx.get_artifact("requirements_doc",{}).get("requirements",[])
        feats = ctx.get_artifact("feature_spec_doc",{}).get("features",[])
        must_reqs  = [r for r in reqs  if r.get("priority")=="must"]
        must_feats = [f for f in feats if f.get("priority")=="must"]
        revision   = f"\n\nREVISION:\n{task.revision_feedback}" if task.revision_feedback else ""
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task,[{"role":"system","content":sys},{"role":"user","content":f"""Design a complete system architecture.

MUST-HAVE REQUIREMENTS ({len(must_reqs)}):
{json.dumps(must_reqs[:10],indent=2)}

MUST-HAVE FEATURES ({len(must_feats)}):
{json.dumps(must_feats[:8],indent=2)}

TECH STACK: {json.dumps(ctx.tech_stack,indent=2)}{revision}

Return ONLY JSON:
{{"diagram_type":"mermaid","diagram_content":"graph TD\\n  Client[Browser]-->Gateway[API Gateway :8000]\\n  Gateway-->Manager[Manager Service :8001]\\n  Manager-->DB[(PostgreSQL)]\\n  Manager-->Queue[(NATS JetStream)]\\n  Manager-->Vector[(Qdrant)]","components":[{{"name":"API Gateway","type":"gateway","description":"Single entry point, CORS, rate limiting","technology":"FastAPI + Nginx","dependencies":[],"exposed_port":8000,"internal":false,"scaling_notes":"Stateless — horizontal scaling"}},{{"name":"Manager Service","type":"service","description":"Orchestrates all agent workflows","technology":"Python + FastAPI + LangGraph","dependencies":["PostgreSQL","NATS","Qdrant"],"exposed_port":8001,"internal":false,"scaling_notes":"Stateful via LangGraph checkpointing"}}],"architecture_pattern":"microservices","communication_patterns":["REST","NATS JetStream","WebSocket"],"data_flow_summary":"Client → API Gateway → Service → PostgreSQL/Qdrant/NATS","quality_score":0.9}}

Rules: every must-have feature served by ≥1 component; valid Mermaid with --> and [] labels; no duplicate ports; architecture_pattern: microservices|monolith|modular_monolith|event_driven"""}],max_tokens=4096)
        content = _j(raw,{"diagram_type":"mermaid","diagram_content":"graph TD\n  A-->B","components":[],"architecture_pattern":"microservices","quality_score":0.0})
        content.setdefault("communication_patterns",["REST","NATS"])
        content.setdefault("data_flow_summary","Client → API → Service → DB")
        review = await ReviewCycle(self,max_cycles=3).run(content,task,schema={"root":["diagram_type","diagram_content","components","architecture_pattern"]})
        if not review.passed: return self.escalate(task,f"Blueprint review failed after {review.cycles_run} cycles")
        artifact = await self.create_artifact(task,"architecture_blueprint",{**content,"project_id":task.project_id})
        await self.write_memory(task,f"Architecture: {content.get('architecture_pattern')}, components: {', '.join(c.get('name','') for c in content.get('components',[])[:6])}",source="system_architect")
        return AgentResult(task_id=task.task_id,agent_id=self.agent_id,status=TaskStatus.COMPLETED,
            content=content,summary=f"Blueprint: {len(content.get('components',[]))} components, pattern={content.get('architecture_pattern')}",
            quality_score=review.final_score,artifacts=[artifact],token_usage=usage)


@AgentFactory.register("openapi_spec_writer_worker")
class APIArchitect(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx      = task.context
        reqs     = ctx.get_artifact("requirements_doc",{}).get("requirements",[])
        blueprint= ctx.get_artifact("architecture_blueprint",{})
        components=[c for c in blueprint.get("components",[]) if c.get("type")=="service"] if isinstance(blueprint,dict) else []
        func_reqs=[r for r in reqs if r.get("category")=="functional"]
        revision = f"\n\nREVISION:\n{task.revision_feedback}" if task.revision_feedback else ""
        sys = self.build_system_prompt(task)
        # Pre-compute to avoid f-string / set-literal collision
        services_json = json.dumps(
            [{"name": s.get("name"), "prefix": s.get("api_prefix", "/api/v1")}
             for s in components[:8]], indent=2)
        raw, usage = await self.call_llm(task,[{"role":"system","content":sys},{"role":"user","content":f"""Generate a complete OpenAPI 3.1 specification.

FUNCTIONAL REQUIREMENTS ({len(func_reqs)}):
{json.dumps(func_reqs[:12],indent=2)}

SERVICES: {services_json}{revision}

Return ONLY valid OpenAPI 3.1 JSON:
{{"openapi":"3.1.0","info":{{"title":"Project API","version":"1.0.0","description":"Generated by AASC Architecture Service"}},"servers":[{{"url":"/api/v1","description":"V1"}}],"paths":{{"/health":{{"get":{{"operationId":"healthCheck","summary":"Health check","tags":["System"],"security":[],"responses":{{"200":{{"description":"OK","content":{{"application/json":{{"schema":{{"$ref":"#/components/schemas/HealthResponse"}}}}}}}}}}}}}},"/auth/register":{{"post":{{"operationId":"registerUser","summary":"Register new user","tags":["Auth"],"security":[],"requestBody":{{"required":true,"content":{{"application/json":{{"schema":{{"$ref":"#/components/schemas/RegisterRequest"}}}}}}}},"responses":{{"201":{{"description":"Registered"}},"409":{{"description":"Email exists"}}}}}}}},"/auth/login":{{"post":{{"operationId":"loginUser","summary":"Login","tags":["Auth"],"security":[],"requestBody":{{"required":true,"content":{{"application/json":{{"schema":{{"$ref":"#/components/schemas/LoginRequest"}}}}}}}},"responses":{{"200":{{"description":"Token pair"}},"401":{{"description":"Invalid credentials"}}}}}}}}}},"components":{{"schemas":{{"HealthResponse":{{"type":"object","properties":{{"status":{{"type":"string"}}}}}},"RegisterRequest":{{"type":"object","required":["email","password"],"properties":{{"email":{{"type":"string","format":"email"}},"password":{{"type":"string","minLength":8}}}}}},"LoginRequest":{{"type":"object","required":["email","password"],"properties":{{"email":{{"type":"string"}},"password":{{"type":"string"}}}}}}}},"securitySchemes":{{"bearerAuth":{{"type":"http","scheme":"bearer","bearerFormat":"JWT"}}}}}},"security":[{{"bearerAuth":[]}}],"quality_score":0.9}}

Rules: every functional req ≥1 endpoint; every endpoint unique operationId; /auth/* use security:[]; 200/201 AND 400 responses minimum; no inline schemas (use $ref)"""}],max_tokens=4096,temperature=0.1)
        content = _j(raw,{"openapi":"3.1.0","info":{"title":"API","version":"1.0.0"},"paths":{},"components":{},"quality_score":0.0})
        violations = _validate_openapi(content)
        endpoint_count = len(content.get("paths",{}))
        review = await ReviewCycle(self,max_cycles=3).run(content,task,schema={"root":["openapi","info","paths","components"]})
        if not review.passed: return self.escalate(task,f"OpenAPI spec failed review: {violations[:2]}")
        artifact = await self.create_artifact(task,"api_spec",{**content,"project_id":task.project_id,"endpoint_count":endpoint_count,"validation_violations":violations})
        return AgentResult(task_id=task.task_id,agent_id=self.agent_id,status=TaskStatus.COMPLETED,
            content=content,summary=f"OpenAPI 3.1: {endpoint_count} endpoints, {len(violations)} violations",
            quality_score=review.final_score,artifacts=[artifact],token_usage=usage)


@AgentFactory.register("schema_designer_worker")
class DatabaseArchitect(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx    = task.context
        reqs   = ctx.get_artifact("requirements_doc",{}).get("requirements",[])
        api    = ctx.get_artifact("api_spec",{})
        bp     = ctx.get_artifact("architecture_blueprint",{})
        schemas= list(api.get("components",{}).get("schemas",{}).keys())[:20] if isinstance(api,dict) else []
        svcs   = [c.get("name") for c in bp.get("components",[]) if c.get("type")=="service"] if isinstance(bp,dict) else []
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task,[{"role":"system","content":sys},{"role":"user","content":f"""Design a complete PostgreSQL database schema.

REQUIREMENTS ({len(reqs)}): {json.dumps([r.get("title") for r in reqs[:10]])}
API SCHEMAS (derive tables): {json.dumps(schemas)}
SERVICES (for ownership): {json.dumps(svcs)}

Return ONLY JSON:
{{"tables":[{{"name":"users","owned_by_service":"auth-service","purpose":"Platform user accounts","columns":[{{"name":"id","type":"UUID","primary_key":true,"nullable":false,"default":"gen_random_uuid()"}},{{"name":"email","type":"VARCHAR(255)","nullable":false,"unique":true}},{{"name":"password_hash","type":"VARCHAR(255)","nullable":false}},{{"name":"role","type":"VARCHAR(50)","nullable":false,"default":"'developer'"}},{{"name":"is_active","type":"BOOLEAN","nullable":false,"default":"TRUE"}},{{"name":"created_at","type":"TIMESTAMPTZ","nullable":false,"default":"NOW()"}},{{"name":"updated_at","type":"TIMESTAMPTZ","nullable":false,"default":"NOW()"}}],"indexes":[{{"name":"idx_users_email","columns":["email"],"unique":true}}],"append_only":false}}],"relationships":[{{"from_table":"projects","from_column":"owner_id","to_table":"users","to_column":"id","type":"many_to_one","on_delete":"RESTRICT"}}],"append_only_tables":[],"table_count":0,"quality_score":0.9}}

MANDATORY: UUID PKs with gen_random_uuid(); TIMESTAMPTZ for all timestamps; every FK has on_delete; no circular FKs; table_count = len(tables)"""}],max_tokens=4096,temperature=0.1)
        content = _j(raw,{"tables":[],"relationships":[],"table_count":0,"quality_score":0.0})
        content["table_count"] = len(content.get("tables",[]))
        violations = _validate_schema(content)
        review = await ReviewCycle(self,max_cycles=3).run(content,task,schema={"root":["tables","relationships"]})
        if not review.passed: return self.escalate(task,f"DB schema failed: {violations[:2]}")
        artifact = await self.create_artifact(task,"database_schema",{**content,"project_id":task.project_id,"violations":violations})
        return AgentResult(task_id=task.task_id,agent_id=self.agent_id,status=TaskStatus.COMPLETED,
            content=content,summary=f"DB schema: {content['table_count']} tables, {len(content.get('relationships',[]))} FKs, {len(violations)} violations",
            quality_score=review.final_score,artifacts=[artifact],token_usage=usage)


@AgentFactory.register("infrastructure_planner_worker")
class InfrastructureArchitect(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx   = task.context
        bp    = ctx.get_artifact("architecture_blueprint",{})
        db    = ctx.get_artifact("database_schema",{})
        comps = [c for c in bp.get("components",[]) if c.get("type") in ("service","gateway","worker")] if isinstance(bp,dict) else []
        table_count = db.get("table_count",0) if isinstance(db,dict) else 0
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task,[{"role":"system","content":sys},{"role":"user","content":f"""Create a Docker Compose deployment architecture.

APP SERVICES: {json.dumps([{{"name":s.get("name"),"port":s.get("exposed_port",8000)}} for s in comps],indent=2)}
DB TABLES: {table_count}

Return ONLY JSON:
{{"services":[{{"name":"api-gateway","image_base":"python:3.12-slim","build_context":"./services/api_gateway","ports":["8000:8000"],"environment":{{"DATABASE_URL":"${{DATABASE_URL}}","NATS_URL":"${{NATS_URL}}","JWT_SECRET":"${{JWT_SECRET}}"}},"depends_on":["postgres","nats"],"healthcheck":"wget -q --spider http://localhost:8000/health || exit 1","healthcheck_interval":"10s","restart_policy":"unless-stopped","resource_limits":{{"memory":"512m","cpu":"0.5"}}}}],"infrastructure_services":[{{"name":"postgres","image":"postgres:16-alpine","volumes":["postgres_data:/var/lib/postgresql/data"]}},{{"name":"nats","image":"nats:2.10-alpine","command":"--jetstream"}},{{"name":"qdrant","image":"qdrant/qdrant:v1.9.2"}},{{"name":"redis","image":"redis:7-alpine"}}],"volumes":["postgres_data","nats_data","qdrant_data"],"networks":["app_internal"],"env_variables_required":["DATABASE_URL","NATS_URL","JWT_SECRET","DB_PASSWORD"],"total_memory_estimate_mb":2048,"total_cpu_estimate_cores":2.0,"deployment_target":"docker_compose","quality_score":0.9}}

Rules: every service has healthcheck; no duplicate ports; env vars use ${{VAR}} format"""}],max_tokens=3000)
        content = _j(raw,{"services":[],"infrastructure_services":[],"volumes":[],"quality_score":0.0})
        review = await ReviewCycle(self,max_cycles=2).run(content,task)
        artifact = await self.create_artifact(task,"deployment_architecture",{**content,"project_id":task.project_id})
        total = len(content.get("services",[]))+len(content.get("infrastructure_services",[]))
        return AgentResult(task_id=task.task_id,agent_id=self.agent_id,status=TaskStatus.COMPLETED,
            content=content,summary=f"Deployment: {total} containers ({len(content.get('services',[]))} app + {len(content.get('infrastructure_services',[]))} infra)",
            quality_score=review.final_score,artifacts=[artifact],token_usage=usage)


@AgentFactory.register("security_architect_worker")
class SecurityArchitect(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx  = task.context
        reqs = ctx.get_artifact("requirements_doc",{}).get("requirements",[])
        api  = ctx.get_artifact("api_spec",{})
        paths= list(api.get("paths",{}).keys())[:20] if isinstance(api,dict) else []
        sec_reqs=[r for r in reqs if any(k in (r.get("description","")+r.get("title","")).lower() for k in ["auth","security","permission","role","encrypt","token","password"])]
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task,[{"role":"system","content":sys},{"role":"user","content":f"""Design the security architecture.

SECURITY REQUIREMENTS: {json.dumps(sec_reqs[:8],indent=2)}
API ENDPOINTS ({len(paths)}): {json.dumps(paths[:15])}

Return ONLY JSON:
{{"authentication":{{"strategy":"JWT Bearer Token","token_types":["access_token (1h TTL)","refresh_token (7d TTL)"],"algorithm":"HS256","password_policy":"min 8 chars, bcrypt rounds=12"}},"authorization":{{"strategy":"RBAC","roles":["owner","admin","developer","reviewer","observer"],"role_permissions":{{"owner":["*"],"admin":["read:*","write:*"],"developer":["read:projects","write:tasks"],"reviewer":["read:*"],"observer":["read:projects"]}}}},"secrets_management":{{"v1_strategy":".env + Docker secrets","required_secrets":["JWT_SECRET","DATABASE_URL","NATS_URL"],"rotation_policy":"Manual V1; Vault V2","gitignore_required":[".env","*.pem","*.key"]}},"transport_security":{{"tls_required_in_production":true,"https_only":true,"hsts_enabled":true}},"input_validation":{{"framework":"Pydantic v2","principles":["Never trust client input","Validate at API boundary","Sanitise before DB write"]}},"owasp_mitigations":[{{"risk":"A01 Broken Access Control","mitigation":"RBAC middleware on every protected route"}},{{"risk":"A02 Cryptographic Failures","mitigation":"bcrypt for passwords, HTTPS in prod"}},{{"risk":"A03 Injection","mitigation":"SQLAlchemy parameterised queries"}},{{"risk":"A07 Auth Failures","mitigation":"JWT expiry + refresh rotation"}}],"quality_score":0.9}}"""}],max_tokens=2500)
        content = _j(raw,{"authentication":{},"authorization":{},"owasp_mitigations":[],"quality_score":0.0})
        review = await ReviewCycle(self,max_cycles=2).run(content,task)
        artifact = await self.create_artifact(task,"security_architecture",{**content,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id,agent_id=self.agent_id,status=TaskStatus.COMPLETED,
            content=content,summary=f"Security: RBAC + JWT, {len(content.get('owasp_mitigations',[]))} OWASP mitigations",
            quality_score=review.final_score,artifacts=[artifact],token_usage=usage)


@AgentFactory.register("scalability_architect_worker")
class ScalabilityArchitect(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx  = task.context
        reqs = ctx.get_artifact("requirements_doc",{}).get("requirements",[])
        nfrs = [r for r in reqs if r.get("category")=="non_functional"]
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task,[{"role":"system","content":sys},{"role":"user","content":f"""Design a comprehensive scaling strategy.

NON-FUNCTIONAL REQUIREMENTS: {json.dumps(nfrs[:8],indent=2)}

Return ONLY JSON:
{{"horizontal_scaling":{{"stateless_services":["api-gateway"],"scaling_triggers":{{"cpu_threshold_pct":70,"memory_threshold_pct":80,"rps_threshold":1000}},"min_replicas":1,"max_replicas":10}},"caching":{{"technology":"Redis 7","cache_targets":[{{"target":"user_sessions","ttl_seconds":3600,"strategy":"write-through"}},{{"target":"project_metadata","ttl_seconds":300,"strategy":"cache-aside"}}]}},"database_scaling":{{"connection_pooling":{{"min_connections":5,"max_connections":20,"library":"asyncpg"}},"read_replicas":"Phase 2 when reads exceed 10k req/min"}},"message_queue":{{"technology":"NATS JetStream","concurrency_model":"Work queue per department","backpressure":"Max 1000 pending per consumer"}},"rate_limiting":{{"limits":[{{"endpoint":"POST /auth/login","limit":"10 req/min"}},{{"endpoint":"POST /api/v1/*","limit":"100 req/min"}}]}},"performance_targets":{{"api_p95_ms":500,"api_p99_ms":1000,"agent_run_p95_s":30,"concurrent_projects":50}},"quality_score":0.88}}"""}],max_tokens=2000)
        content = _j(raw,{"horizontal_scaling":{},"caching":{},"quality_score":0.0})
        review = await ReviewCycle(self,max_cycles=2).run(content,task)
        artifact = await self.create_artifact(task,"scaling_strategy",{**content,"project_id":task.project_id})
        p95 = content.get("performance_targets",{}).get("api_p95_ms",500)
        return AgentResult(task_id=task.task_id,agent_id=self.agent_id,status=TaskStatus.COMPLETED,
            content=content,summary=f"Scaling: Redis cache + NATS queuing, p95={p95}ms target",
            quality_score=review.final_score,artifacts=[artifact],token_usage=usage)


@AgentFactory.register("integration_architect_worker")
class IntegrationArchitect(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx  = task.context
        reqs = ctx.get_artifact("requirements_doc",{}).get("requirements",[])
        kw   = ["email","payment","stripe","twilio","slack","github","google","aws","s3","webhook","oauth"]
        int_reqs = [r for r in reqs if any(k in (r.get("description","")+r.get("title","")).lower() for k in kw)]
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task,[{"role":"system","content":sys},{"role":"user","content":f"""Design all external integrations.

REQUIREMENTS MENTIONING EXTERNAL SERVICES: {json.dumps(int_reqs[:8],indent=2)}

Return ONLY JSON:
{{"external_integrations":[{{"name":"Email Service","provider_options":["SendGrid","AWS SES"],"selected_for_v1":"SendGrid","purpose":"Transactional emails","adapter_interface":"EmailProvider","required_env_vars":["SENDGRID_API_KEY"]}}],"provider_abstraction_pattern":{{"principle":"All providers behind abstract interface","base_interface":"class EmailProvider(ABC): async def send(...) -> bool"}},"event_contracts":[{{"nats_subject":"user.registered","published_by":"auth-service","subscribed_by":["email-service"],"payload_schema":{{"user_id":"uuid","email":"string"}}}}],"webhook_endpoints":[],"oauth_providers":[],"quality_score":0.87}}

If no external integrations required, return minimal valid JSON with empty arrays."""}],max_tokens=2000)
        content = _j(raw,{"external_integrations":[],"event_contracts":[],"quality_score":0.0})
        review = await ReviewCycle(self,max_cycles=2).run(content,task)
        artifact = await self.create_artifact(task,"integration_plan",{**content,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id,agent_id=self.agent_id,status=TaskStatus.COMPLETED,
            content=content,summary=f"Integration plan: {len(content.get('external_integrations',[]))} external, {len(content.get('event_contracts',[]))} event contracts",
            quality_score=review.final_score,artifacts=[artifact],token_usage=usage)


@AgentFactory.register("traceability_agent_worker")
class TraceabilityAgent(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx   = task.context
        reqs  = ctx.get_artifact("requirements_doc",{}).get("requirements",[])
        feats = ctx.get_artifact("feature_spec_doc",{}).get("features",[])
        api   = ctx.get_artifact("api_spec",{})
        bp    = ctx.get_artifact("architecture_blueprint",{})
        db    = ctx.get_artifact("database_schema",{})
        paths = list(api.get("paths",{}).keys())[:30] if isinstance(api,dict) else []
        comps = [c.get("name") for c in bp.get("components",[])[:10]] if isinstance(bp,dict) else []
        tables= [t.get("name") for t in db.get("tables",[])[:15]] if isinstance(db,dict) else []
        func_reqs = [r for r in reqs if r.get("category")=="functional"]
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task,[{"role":"system","content":sys},{"role":"user","content":f"""Create a complete traceability matrix.

FUNCTIONAL REQUIREMENTS ({len(func_reqs)}):
{json.dumps(func_reqs[:12],indent=2)}

API ENDPOINTS: {json.dumps(paths[:20])}
COMPONENTS: {json.dumps(comps)}
DB TABLES: {json.dumps(tables)}

Return ONLY JSON:
{{"traceability_matrix":[{{"requirement_id":"REQ-001","requirement_title":"User Registration","category":"functional","priority":"must","api_endpoints":["POST /api/v1/auth/register"],"components":["API Gateway","Auth Service"],"database_tables":["users"],"feature_names":["User Authentication"],"coverage_status":"fully_covered","coverage_notes":"Registration endpoint + users table"}}],"coverage_summary":{{"total_functional_requirements":{len(func_reqs)},"fully_covered":0,"partially_covered":0,"uncovered":0,"coverage_percentage":0.0}},"uncovered_requirements":[],"gaps_analysis":"No significant gaps identified","quality_score":0.92}}

Rules: every functional req has an entry; coverage_status: fully_covered|partially_covered|uncovered; coverage_percentage = (fully + partial*0.5)/total*100"""}],max_tokens=4096)
        content = _j(raw,{"traceability_matrix":[],"coverage_summary":{"coverage_percentage":0.0},"uncovered_requirements":[],"quality_score":0.0})
        matrix = content.get("traceability_matrix",[])
        fully   = sum(1 for m in matrix if m.get("coverage_status")=="fully_covered")
        partial = sum(1 for m in matrix if m.get("coverage_status")=="partially_covered")
        uncov   = [m.get("requirement_id") for m in matrix if m.get("coverage_status")=="uncovered"]
        total   = len(matrix) or 1
        content["coverage_summary"] = {"total_functional_requirements":total,"fully_covered":fully,
            "partially_covered":partial,"uncovered":len(uncov),"coverage_percentage":round((fully+partial*0.5)/total*100,1)}
        content["uncovered_requirements"] = uncov
        artifact = await self.create_artifact(task,"traceability_matrix",{**content,"project_id":task.project_id})
        # Write to requirement_dependencies table
        try:
            from sqlalchemy import text
            api_id = artifact.get("artifact_id","")
            async with self._db_factory() as db:
                for entry in matrix:
                    req_id = entry.get("requirement_id","")
                    if not req_id or not api_id: continue
                    for ep in entry.get("api_endpoints",[]):
                        await db.execute(text("INSERT INTO requirement_dependencies (project_id,source_entity_type,source_entity_id,relationship_type,target_entity_type,target_entity_id) VALUES (:pid,'requirement',:req,'implements','api_spec',:api) ON CONFLICT DO NOTHING"),
                            {"pid":task.project_id,"req":req_id,"api":api_id})
        except Exception as e: log.warning("traceability_db_write_failed",error=str(e))
        pct = content["coverage_summary"]["coverage_percentage"]
        return AgentResult(task_id=task.task_id,agent_id=self.agent_id,status=TaskStatus.COMPLETED,
            content=content,summary=f"Traceability: {len(matrix)} reqs, {pct:.1f}% coverage, {len(uncov)} uncovered",
            quality_score=float(content.get("quality_score",0.9)),artifacts=[artifact],token_usage=usage)


@AgentFactory.register("architecture_reviewer_worker")
class ArchitectureReviewer(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx   = task.context
        bp    = ctx.get_artifact("architecture_blueprint",{})
        api   = ctx.get_artifact("api_spec",{})
        db    = ctx.get_artifact("database_schema",{})
        dep   = ctx.get_artifact("deployment_architecture",{})
        sec   = ctx.get_artifact("security_architecture",{})
        trace = ctx.get_artifact("traceability_matrix",{})
        ec    = len(api.get("paths",{})) if isinstance(api,dict) else 0
        cc    = len(bp.get("components",[])) if isinstance(bp,dict) else 0
        tc    = db.get("table_count",0) if isinstance(db,dict) else 0
        sc    = len(dep.get("services",[])) if isinstance(dep,dict) else 0
        cov   = trace.get("coverage_summary",{}).get("coverage_percentage",0) if isinstance(trace,dict) else 0
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task,[{"role":"system","content":sys},{"role":"user","content":f"""Review the complete architecture package for consistency.

PACKAGE: blueprint={cc} components, api={ec} endpoints, db={tc} tables, deploy={sc} services, coverage={cov:.1f}%

Cross-cutting checks:
1. Every blueprint service → deployment container?
2. Every API endpoint → ≥1 DB table?
3. Auth endpoints covered by security architecture?
4. All env vars in deployment also in secrets management?
5. Traceability coverage ≥ 80%?

Return ONLY JSON:
{{"overall_passed":true,"scores":{{"blueprint_quality":0.9,"api_completeness":0.88,"schema_soundness":0.92,"deployment_readiness":0.87,"security_coverage":0.9,"traceability_coverage":{round(cov/100,2)}}},"overall_score":0.89,"blocking_issues":[],"warnings":[],"cross_cutting_issues":[],"recommendation":"Approve — architecture complete and internally consistent","quality_score":0.89}}

overall_passed=true ONLY IF: no blocking_issues AND traceability_coverage≥0.8 AND all scores≥0.7"""}],max_tokens=2000)
        content = _j(raw,{"overall_passed":False,"overall_score":0.0,"blocking_issues":["Review call failed"],"quality_score":0.0})
        passed  = content.get("overall_passed",False)
        score   = float(content.get("overall_score",0.0))
        blocking= content.get("blocking_issues",[])
        artifact = await self.create_artifact(task,"architecture_review_report",{**content,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id,agent_id=self.agent_id,
            status=TaskStatus.COMPLETED if passed else TaskStatus.FAILED,
            content=content,summary=f"Architecture review {'PASSED' if passed else 'FAILED'}: score={score:.2f}, {len(blocking)} blocking",
            quality_score=score,artifacts=[artifact],token_usage=usage,
            failure_reason=None if passed else f"{len(blocking)} blocking: {blocking[:2]}")


# ── Appendix A (M3.3 prerequisite) ────────────────────────────
# ui_architect_worker lives in its own module; import here so it
# registers with AgentFactory when services.architecture.workers loads.
from services.architecture.workers.ui_architect import UiArchitectWorker  # noqa: F401,E402
