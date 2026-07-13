"""services/architecture/workers/security_architect.py — SecurityArchitect L5."""
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

@AgentFactory.register("security_architect_worker")
class SecurityArchitectWorker(BaseAgent):
    """Produces security_architecture. Auth, secrets, RBAC, encryption."""
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx  = task.context
        bp   = ctx.get_artifact("architecture_blueprint", {})
        api  = ctx.get_artifact("api_spec", {})
        services = bp.get("services", []) if isinstance(bp, dict) else []

        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Design the security architecture for this system.

SERVICES: {json.dumps([s.get("name") for s in services], indent=2)}
API ENDPOINTS: {json.dumps(list(api.get("paths",{{}}}}).keys())[:12] if isinstance(api,dict) else [], indent=2)}

Return ONLY JSON:
{{
  "auth_strategy": {{
    "type": "JWT",
    "access_token_ttl_seconds": 3600,
    "refresh_token_ttl_seconds": 604800,
    "algorithm": "HS256",
    "token_rotation": true
  }},
  "rbac": {{
    "roles": ["owner","admin","developer","reviewer","observer"],
    "role_hierarchy": {{"owner":["admin","developer","reviewer","observer"],"admin":["developer","reviewer","observer"]}},
    "resource_permissions": [{{"resource":"projects","actions":["create","read","update","delete"],"min_role":"developer"}}]
  }},
  "secrets_management": {{
    "v1_strategy": ".env files with strict .gitignore",
    "v2_strategy": "HashiCorp Vault or AWS Secrets Manager",
    "required_secrets": ["DATABASE_URL","JWT_SECRET","NATS_URL"],
    "rotation_policy": "Manual V1, automated V2"
  }},
  "encryption": {{
    "in_transit": "TLS 1.3 (Nginx termination)",
    "at_rest": "PostgreSQL pgcrypto for PII fields",
    "pii_fields": ["users.email","users.full_name"]
  }},
  "api_security": {{
    "rate_limiting": "100 req/min per user, 1000 req/min per IP",
    "cors_policy": "Allowlist of frontend origins only",
    "input_validation": "Pydantic v2 on all request bodies",
    "sql_injection": "SQLAlchemy ORM + parameterized queries only"
  }},
  "audit_requirements": {{
    "log_all_auth_events": true,
    "log_all_approval_decisions": true,
    "log_pii_access": true,
    "retention_days": 90
  }},
  "quality_score": 0.0
}}"""}], max_tokens=3000)

        content = _parse(raw, {"auth_strategy":{},"rbac":{},"quality_score":0.0})
        review  = await ReviewCycle(self).run(content, task, schema={"root":["auth_strategy","rbac","secrets_management","encryption"]})
        artifact = await self.create_artifact(task, "security_architecture", {**content,"project_id":task.project_id})
        await self.write_memory(task, f"Security: JWT auth, {len(content.get('rbac',{}).get('roles',[]))} RBAC roles, TLS 1.3 in transit", source="security_architect")
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary=f"Security architecture: JWT auth, {len(content.get('rbac',{}).get('roles',[]))} roles, TLS 1.3",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)
