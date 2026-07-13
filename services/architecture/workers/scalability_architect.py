"""services/architecture/workers/scalability_architect.py — ScalabilityArchitect L5."""
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

@AgentFactory.register("scalability_architect_worker")
class ScalabilityArchitectWorker(BaseAgent):
    """Produces scaling_strategy: horizontal scaling, caching, queues, rate limits."""
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx      = task.context
        bp       = ctx.get_artifact("architecture_blueprint", {})
        services = bp.get("services", []) if isinstance(bp, dict) else []
        critical = [s for s in services if s.get("critical_path", False)]

        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [{"role":"system","content":sys},{"role":"user","content":f"""Design the scaling strategy for this system.

CRITICAL PATH SERVICES: {json.dumps([s.get("name") for s in critical], indent=2)}
ALL SERVICES: {json.dumps([s.get("name") for s in services], indent=2)}

Return ONLY JSON:
{{
  "horizontal_scaling": {{
    "stateless_services": ["api-gateway","manager-service"],
    "stateful_services": ["postgres","qdrant"],
    "scaling_trigger": "CPU > 70% for 5 minutes OR request queue depth > 100",
    "max_replicas_per_service": 10,
    "v1_note": "Single replica Docker Compose — scale-ready by design (12-factor)"
  }},
  "caching_strategy": {{
    "l1_cache": "In-process LRU cache (project context, 5 min TTL)",
    "l2_cache": "Redis (API responses, session data, 15 min TTL)",
    "cache_invalidation": "Event-driven via NATS on entity updates",
    "never_cache": ["audit_events","token_ledger","approval_decisions"]
  }},
  "queue_strategy": {{
    "task_queue": "NATS JetStream with work queue retention",
    "dead_letter_queue": "NATS DLQ with 30-day retention",
    "max_queue_depth": 10000,
    "consumer_groups": ["manager-workers","product-workers","engineering-workers"]
  }},
  "rate_limiting": {{
    "per_user_per_minute": 100,
    "per_ip_per_minute": 1000,
    "llm_calls_per_project_per_hour": 500,
    "enforcement": "Redis sliding window counter"
  }},
  "performance_targets": {{
    "api_p95_ms": 500,
    "api_p99_ms": 1000,
    "agent_run_p95_seconds": 30,
    "approval_gate_max_wait_days": 30
  }},
  "bottleneck_analysis": [
    {{"service":"LLM API calls","mitigation":"Async execution, provider failover, response caching for identical prompts"}}
  ],
  "quality_score": 0.0
}}"""}], max_tokens=2500)

        content = _parse(raw, {"horizontal_scaling":{},"caching_strategy":{},"quality_score":0.0})
        review  = await ReviewCycle(self).run(content, task)
        artifact = await self.create_artifact(task, "scaling_strategy", {**content,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content, summary="Scaling strategy: horizontal scaling + Redis L2 cache + NATS queues",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)
