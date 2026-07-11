"""services/engineering/workers/integration.py — Integration Lead's 3 L5 workers."""
from __future__ import annotations

import json

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import ReviewCycle, TaskInput
from core.runtime.factory import AgentFactory
from services.engineering.models import CodeFile, ModuleType
from services.engineering.utils import idempotency_key, parse_llm_json


@AgentFactory.register("internal_integration_worker")
class InternalIntegrationWorker(BaseAgent):
    """Internal Event Worker — inter-service event contracts/handlers. No dependencies."""

    async def execute(self, task: TaskInput) -> AgentResult:
        bp = task.context.get_artifact("architecture_blueprint", {})
        services = bp.get("services", []) if isinstance(bp, dict) else []
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Generate internal service event contracts and handlers (NATS-based).

SERVICES: {json.dumps([s.get('name') for s in services[:6]])}

Return ONLY JSON:
{{"files":[{{"path":"app/events/handlers.py","language":"python","content":"async def on_project_created(payload: dict) -> None:\\n    pass"}}],"events_implemented":["project.created","task.completed"],"quality_score":0.87}}"""},
        ], max_tokens=3000)

        content  = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task)
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.INTERNAL_EVENT.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.INTERNAL_EVENT.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"Internal event contracts: {len(files)} files",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )


@AgentFactory.register("third_party_integration_worker")
class ThirdPartyIntegrationWorker(BaseAgent):
    """External API Worker — third-party integrations. No dependencies."""

    async def execute(self, task: TaskInput) -> AgentResult:
        integ = task.context.get_artifact("integration_plan", {})
        providers = integ.get("providers", []) if isinstance(integ, dict) else []
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Implement external/third-party service integrations.

PROVIDERS: {json.dumps(providers[:6], indent=2)}

Return ONLY JSON:
{{"files":[{{"path":"app/integrations/email.py","language":"python","content":"import httpx\\n\\nasync def send_email(to: str, subject: str, body: str) -> bool:\\n    return True"}}],"integrations_implemented":["email"],"quality_score":0.85}}"""},
        ], max_tokens=3000)

        content  = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task)
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.EXTERNAL_API.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.EXTERNAL_API.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"Third-party integrations: {len(content.get('integrations_implemented', []))}",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )


@AgentFactory.register("messaging_worker")
class MessagingWorker(BaseAgent):
    """Messaging Worker (new in M3.3) — NATS publishers/subscribers + message contracts.
    Depends on internal_integration_worker in the task graph (shares event vocabulary)."""

    async def execute(self, task: TaskInput) -> AgentResult:
        events = task.context.approved_artifacts.get("internal_integration_worker", {})
        implemented = events.get("events_implemented", []) if isinstance(events, dict) else []
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Generate NATS publisher/subscriber wiring and message contracts.

EXISTING EVENTS: {json.dumps(implemented)}

Return ONLY JSON:
{{"files":[{{"path":"app/messaging/publisher.py","language":"python","content":"import nats\\n\\nasync def publish(subject: str, payload: dict) -> None:\\n    nc = await nats.connect()\\n    await nc.publish(subject, str(payload).encode())"}}],"subjects_implemented":["project.created"],"quality_score":0.86}}"""},
        ], max_tokens=3000)

        content  = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task)
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.MESSAGING.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.MESSAGING.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"Messaging: {len(files)} files",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )
