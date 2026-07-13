"""services/architecture/workers/database_architect.py — DatabaseArchitect L5 worker."""
from __future__ import annotations
import json
from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import ReviewCycle, TaskInput
from core.runtime.factory import AgentFactory

def _parse(raw, fb=None):
    try:
        c=raw.strip()
        if c.startswith("```"): c=c.split("```")[1]; c=c[4:] if c.startswith("json") else c
        return json.loads(c.strip())
    except Exception: return fb or {}

@AgentFactory.register("database_architect_worker")
class DatabaseArchitectWorker(BaseAgent):
    """Produces database_schema. Consumes architecture_blueprint + api_spec."""
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx      = task.context
        bp       = ctx.get_artifact("architecture_blueprint", {})
        api      = ctx.get_artifact("api_spec", {})
        reqs     = ctx.get_artifact("requirements_doc", {})
        services = bp.get("services", []) if isinstance(bp, dict) else []
        req_list = reqs.get("requirements", []) if isinstance(reqs, dict) else []
        paths    = list(api.get("paths", {}).keys())[:15] if isinstance(api, dict) else []

        sys = self.build_system_prompt(task)
        revision = f"\n\nREVISION FEEDBACK:\n{task.revision_feedback}" if task.revision_feedback else ""
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Design the complete PostgreSQL database schema.

SERVICES ({len(services)}): {json.dumps([s.get("name") for s in services], indent=2)}
API ENDPOINTS ({len(paths)}): {json.dumps(paths, indent=2)}
KEY REQUIREMENTS: {json.dumps([r.get("title") for r in req_list if r.get("priority")=="must"][:10], indent=2)}
{revision}

Rules:
- All primary keys: UUID type with gen_random_uuid() default
- All timestamps: TIMESTAMPTZ (not TIMESTAMP)
- Every FK column must be explicitly listed
- Append-only tables (audit_events, token_ledger) must have no_update: true flag
- No circular FK dependencies

Return ONLY JSON:
{{
  "tables": [
    {{
      "name": "users",
      "owned_by_service": "manager-service",
      "no_update": false,
      "columns": [
        {{"name":"id","type":"UUID","primary_key":true,"nullable":false,"default":"gen_random_uuid()"}},
        {{"name":"email","type":"VARCHAR(255)","nullable":false,"unique":true}},
        {{"name":"created_at","type":"TIMESTAMPTZ","nullable":false,"default":"NOW()"}}
      ],
      "indexes": [{{"name":"idx_users_email","columns":["email"],"unique":true,"type":"btree"}}],
      "constraints": [{{"type":"CHECK","definition":"role IN ('owner','admin','developer')"}}]
    }}
  ],
  "relationships": [
    {{"from_table":"tasks","from_column":"project_id","to_table":"projects","to_column":"id","type":"many_to_one","on_delete":"CASCADE"}}
  ],
  "partitioning_strategy": "None for V1 — revisit at 10M rows per table",
  "table_count": 0,
  "quality_score": 0.0
}}"""}], max_tokens=4096)

        content = _parse(raw, {"tables":[],"relationships":[],"table_count":0,"quality_score":0.0})
        content["table_count"] = len(content.get("tables", []))
        review  = await ReviewCycle(self).run(content.get("tables",[]), task,
                                              schema={"item":["name","columns","owned_by_service"]})
        if not review.passed:
            return self.escalate(task, f"Database schema failed review: {review.critique_history[-1].blocking[:2] if review.critique_history else []}")
        artifact = await self.create_artifact(task, "database_schema",
            {**content, "project_id":task.project_id})
        await self.write_memory(task, f"DB schema: {content['table_count']} tables, {len(content.get('relationships',[]))} relationships", source="database_architect")
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary=f"DB schema: {content['table_count']} tables, {len(content.get('relationships',[]))} relationships",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)
