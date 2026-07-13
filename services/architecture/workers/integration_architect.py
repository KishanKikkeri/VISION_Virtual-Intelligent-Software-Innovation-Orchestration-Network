"""services/architecture/workers/integration_architect.py — IntegrationArchitect L5."""
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

@AgentFactory.register("integration_architect_worker")
class IntegrationArchitectWorker(BaseAgent):
    """Produces integration_plan: external APIs, provider adapters, event contracts."""
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx  = task.context
        reqs = ctx.get_artifact("requirements_doc", {})
        bp   = ctx.get_artifact("architecture_blueprint", {})
        req_list  = reqs.get("requirements", []) if isinstance(reqs, dict) else []
        services  = bp.get("services", [])        if isinstance(bp, dict) else []

        # Identify integration signals in requirements
        integration_reqs = [r for r in req_list if any(
            k in r.get("description","").lower()
            for k in ["email","sms","payment","stripe","github","slack","webhook","oauth","sso"]
        )]

        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Design the integration architecture for this project.

INTEGRATION REQUIREMENTS: {json.dumps(integration_reqs[:8], indent=2)}
INTERNAL SERVICES: {json.dumps([s.get("name") for s in services], indent=2)}

Return ONLY JSON:
{{
  "external_integrations": [
    {{
      "name": "Email (SendGrid)",
      "type": "communication",
      "required": false,
      "adapter_pattern": "BaseCommunicationProvider with SendGrid + SMTP implementations",
      "fallback": "SMTP direct",
      "api_endpoint": "https://api.sendgrid.com/v3",
      "auth_method": "API Key (SENDGRID_API_KEY env var)",
      "events_triggered": ["user.registered","approval.requested","deployment.completed"]
    }}
  ],
  "internal_event_contracts": [
    {{
      "subject": "manager.task.assigned",
      "publisher": "manager-service",
      "subscribers": ["product-service","architecture-service"],
      "payload_schema": {{"project_id":"string","department":"string","task_type":"string"}},
      "delivery_guarantee": "at_least_once"
    }}
  ],
  "adapter_pattern": {{
    "description": "All external integrations implement a base interface. Swap providers without changing business logic.",
    "base_interfaces": ["BaseCommunicationProvider","BasePaymentProvider","BaseStorageProvider","BaseAuthProvider"]
  }},
  "webhook_endpoints": [
    {{"path":"/webhooks/github","purpose":"CI/CD pipeline events","auth":"HMAC signature"}}
  ],
  "quality_score": 0.0
}}"""}], max_tokens=3000)

        content = _parse(raw, {"external_integrations":[],"internal_event_contracts":[],"quality_score":0.0})
        review  = await ReviewCycle(self).run(content, task)
        artifact = await self.create_artifact(task, "integration_plan", {**content,"project_id":task.project_id})
        ext_count   = len(content.get("external_integrations", []))
        event_count = len(content.get("internal_event_contracts", []))
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary=f"Integration plan: {ext_count} external integrations, {event_count} NATS event contracts",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)
