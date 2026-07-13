"""services/architecture/workers/infrastructure_architect.py — InfrastructureArchitect L5."""
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

@AgentFactory.register("infrastructure_architect_worker")
class InfrastructureArchitectWorker(BaseAgent):
    """Produces deployment_architecture. Consumes blueprint + db_schema."""
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx      = task.context
        bp       = ctx.get_artifact("architecture_blueprint", {})
        db       = ctx.get_artifact("database_schema", {})
        services = bp.get("services", []) if isinstance(bp, dict) else []
        tables   = db.get("tables", [])   if isinstance(db,  dict) else []

        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Create a complete Docker Compose deployment architecture.

APPLICATION SERVICES ({len(services)}): {json.dumps([{{"name":s.get("name"),"port":s.get("port")}} for s in services], indent=2)}
DATABASE TABLES ({len(tables)}): {json.dumps([t.get("name") for t in tables[:15]], indent=2)}
DEPLOYMENT TARGET: docker_compose (V1)

Return ONLY JSON:
{{
  "containers": [
    {{
      "name": "api-gateway",
      "image_base": "python:3.12-slim",
      "build_context": "./services/api_gateway",
      "ports": ["8000:8000"],
      "environment": {{"DATABASE_URL":"${{DATABASE_URL}}","NATS_URL":"${{NATS_URL}}","JWT_SECRET":"${{JWT_SECRET}}"}},
      "depends_on": ["postgres","nats"],
      "healthcheck": "wget -q --spider http://localhost:8000/health || exit 1",
      "restart_policy": "unless-stopped",
      "memory_limit_mb": 512,
      "cpu_limit": 0.5
    }}
  ],
  "infrastructure_services": [
    {{"name":"postgres","image":"postgres:16-alpine","volumes":["postgres_data:/var/lib/postgresql/data"],"environment":{{"POSTGRES_DB":"app","POSTGRES_USER":"app","POSTGRES_PASSWORD":"${{DB_PASSWORD}}"}}}},
    {{"name":"nats","image":"nats:2.10-alpine","command":"--jetstream --store_dir=/data","volumes":["nats_data:/data"]}},
    {{"name":"qdrant","image":"qdrant/qdrant:v1.9.2","volumes":["qdrant_data:/qdrant/storage"]}},
    {{"name":"redis","image":"redis:7-alpine","command":"redis-server --appendonly yes"}}
  ],
  "volumes": ["postgres_data","nats_data","qdrant_data","redis_data","artifact_data"],
  "networks": [{{"name":"app_internal","driver":"bridge"}}],
  "total_memory_estimate_mb": 0,
  "total_cpu_estimate_cores": 0.0,
  "deployment_target": "docker_compose",
  "production_path": "kubernetes (V2 — 12-factor services ready for migration)",
  "quality_score": 0.0
}}"""}], max_tokens=3500)

        content = _parse(raw, {"containers":[],"infrastructure_services":[],"volumes":[],"quality_score":0.0})
        content["total_memory_estimate_mb"] = sum(c.get("memory_limit_mb",256) for c in content.get("containers",[]))
        review  = await ReviewCycle(self).run(content.get("containers",[]), task, schema={"item":["name","image_base","ports","depends_on","healthcheck"]})
        artifact = await self.create_artifact(task, "deployment_architecture", {**content,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary=f"Deployment architecture: {len(content.get('containers',[]))} app containers + {len(content.get('infrastructure_services',[]))} infra services",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)
