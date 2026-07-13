"""
services/architecture/workers/system_architect.py
===================================================
SystemArchitect — L5 worker.
Produces architecture_blueprint: services, boundaries, dependencies,
communication paths. This is the first artifact and the foundation
all other architecture workers build upon.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import ReviewCycle, TaskInput
from core.runtime.factory import AgentFactory


@AgentFactory.register("system_architect_worker")
class SystemArchitectWorker(BaseAgent):
    """
    Generates the system architecture blueprint.
    Input:  requirements_doc, feature_spec_doc
    Output: architecture_blueprint artifact
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        ctx   = task.context
        reqs  = ctx.get_artifact("requirements_doc",  {})
        feats = ctx.get_artifact("feature_spec_doc",  {})

        req_list  = reqs.get("requirements", [])  if isinstance(reqs,  dict) else []
        feat_list = feats.get("features",    [])  if isinstance(feats, dict) else []

        must_have  = [r for r in req_list  if r.get("priority") == "must"]
        must_feats = [f for f in feat_list if f.get("priority") == "must"]
        is_complex = len(must_have) > 8 or len(must_feats) > 5

        pattern = "microservices" if is_complex else "modular_monolith"

        system   = self.build_system_prompt(task)
        revision = f"\n\nREVISION FEEDBACK:\n{task.revision_feedback}" if task.revision_feedback else ""

        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": system},
            {"role": "user",   "content": f"""Design the complete system architecture for this project.

MUST-HAVE REQUIREMENTS ({len(must_have)} items):
{json.dumps(must_have[:10], indent=2)}

MUST-HAVE FEATURES ({len(must_feats)} items):
{json.dumps(must_feats[:8], indent=2)}

TECH STACK: {json.dumps(ctx.tech_stack, indent=2)}
RECOMMENDED PATTERN: {pattern} (based on {len(must_have)} must-have requirements)
{revision}

Return ONLY this JSON — no markdown, no explanation:
{{
  "architecture_pattern": "{pattern}",
  "design_rationale": "One paragraph explaining why this pattern fits the project scope",
  "services": [
    {{
      "name": "api-gateway",
      "type": "gateway",
      "responsibility": "Single sentence — what this service owns exclusively",
      "technology": "FastAPI + Nginx",
      "port": 8000,
      "dependencies": [],
      "scales_horizontally": true,
      "critical_path": true,
      "owned_data": []
    }}
  ],
  "communication": [
    {{
      "from": "service-a",
      "to": "service-b",
      "protocol": "REST | NATS | gRPC | WebSocket",
      "pattern": "sync | async | event",
      "description": "What information flows and why"
    }}
  ],
  "external_dependencies": [
    {{
      "name": "PostgreSQL",
      "type": "database | queue | cache | storage | auth | monitoring",
      "required": true,
      "notes": "Primary operational database"
    }}
  ],
  "quality_score": 0.0
}}"""}],
            max_tokens=4096,
        )

        content = _parse_json(raw, {
            "architecture_pattern": pattern,
            "services":             [],
            "communication":        [],
            "external_dependencies":[],
            "quality_score":        0.0,
        })

        review = await ReviewCycle(self).run(
            content, task,
            schema={"root": ["architecture_pattern", "services", "communication"]},
        )
        if not review.passed:
            return self.escalate(
                task,
                f"Architecture blueprint failed review after {review.cycles_run} cycles: "
                f"{review.critique_history[-1].blocking[:2] if review.critique_history else []}",
            )

        artifact = await self.create_artifact(
            task, "architecture_blueprint",
            {**content, "project_id": task.project_id},
        )

        await self.write_memory(
            task,
            f"Architecture pattern: {content.get('architecture_pattern')}. "
            f"Services: {[s.get('name') for s in content.get('services', [])]}",
            source="system_architect",
        )

        svc_count  = len(content.get("services", []))
        comm_count = len(content.get("communication", []))
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            content=content,
            summary=(f"Architecture blueprint: {svc_count} services, "
                     f"{comm_count} communication paths, "
                     f"pattern={content.get('architecture_pattern')}"),
            quality_score=review.final_score,
            artifacts=[artifact],
            token_usage=usage,
        )


def _parse_json(raw: str, fallback: Any) -> Any:
    try:
        c = raw.strip()
        if c.startswith("```"):
            parts = c.split("```")
            c = parts[1] if len(parts) > 1 else c
            if c.startswith("json"): c = c[4:]
        return json.loads(c.strip())
    except Exception:
        return fallback
