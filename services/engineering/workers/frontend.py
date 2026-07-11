"""
services/engineering/workers/frontend.py — Frontend Lead's 4 L5 workers.
None of these may run without an approved ui_blueprint (Stage 3 rule:
"These must consume ui_blueprint. No UI generation without it.").
"""
from __future__ import annotations

import json

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import ReviewCycle, TaskInput
from core.runtime.factory import AgentFactory
from services.engineering.models import CodeFile, ModuleType
from services.engineering.utils import idempotency_key, parse_llm_json


def _require_ui_blueprint(task: TaskInput):
    bp = task.context.get_artifact("ui_blueprint")
    if not bp:
        return None
    return bp


@AgentFactory.register("component_worker")
class ComponentWorker(BaseAgent):
    """Component Worker — generates ui_blueprint.components."""

    async def execute(self, task: TaskInput) -> AgentResult:
        bp = _require_ui_blueprint(task)
        if bp is None:
            return self.escalate(task, "No ui_blueprint available — cannot generate UI components")

        components = bp.get("components", [])[:8] if isinstance(bp, dict) else []
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Generate reusable React/TypeScript UI components.

COMPONENTS FROM UI BLUEPRINT: {json.dumps(components, indent=2)}

Return ONLY JSON:
{{"files":[{{"path":"src/components/NavBar.tsx","language":"typescript","content":"export function NavBar() {{ return <nav />; }}"}}],"components_implemented":{json.dumps([c.get('name') for c in components])},"quality_score":0.87}}"""},
        ], max_tokens=4096)

        content  = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task)
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.COMPONENT.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.COMPONENT.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"Generated {len(files)} component files",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )


@AgentFactory.register("state_management_worker")
class StateManagementWorker(BaseAgent):
    """State Worker — generates ui_blueprint.state_boundaries. No dependencies."""

    async def execute(self, task: TaskInput) -> AgentResult:
        bp = _require_ui_blueprint(task)
        if bp is None:
            return self.escalate(task, "No ui_blueprint available — cannot generate state layer")

        boundaries = bp.get("state_boundaries", [])[:8] if isinstance(bp, dict) else []
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Implement state management (stores/context/hooks) for these state boundaries.

STATE BOUNDARIES: {json.dumps(boundaries, indent=2)}

Return ONLY JSON:
{{"files":[{{"path":"src/store/authStore.ts","language":"typescript","content":"export const useAuthStore = () => ({{ user: null }});"}}],"stores_implemented":{json.dumps([b.get('owner') for b in boundaries])},"quality_score":0.86}}"""},
        ], max_tokens=3500)

        content  = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task)
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.STATE.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.STATE.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"State management: {len(files)} files",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )


@AgentFactory.register("routing_worker")
class RoutingWorker(BaseAgent):
    """Routing Worker (new in M3.3) — generates ui_blueprint.routes + navigation."""

    async def execute(self, task: TaskInput) -> AgentResult:
        bp = _require_ui_blueprint(task)
        if bp is None:
            return self.escalate(task, "No ui_blueprint available — cannot generate routing")

        routes = bp.get("routes", [])[:12] if isinstance(bp, dict) else []
        nav    = bp.get("navigation", {}) if isinstance(bp, dict) else {}
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Generate route definitions and navigation guards.

ROUTES: {json.dumps(routes, indent=2)}
NAVIGATION: {json.dumps(nav, indent=2)}

Return ONLY JSON:
{{"files":[{{"path":"src/router/index.tsx","language":"typescript","content":"export const routes = [{{ path: '/dashboard', auth: true }}];"}}],"routes_implemented":{json.dumps([r.get('path') for r in routes])},"quality_score":0.86}}"""},
        ], max_tokens=3500)

        content  = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task)
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.ROUTING.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.ROUTING.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"Routing: {len(files)} files ({len(routes)} routes)",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )


@AgentFactory.register("page_worker")
class PageWorker(BaseAgent):
    """Page Worker — depends on component_worker + routing_worker."""

    async def execute(self, task: TaskInput) -> AgentResult:
        bp = _require_ui_blueprint(task)
        if bp is None:
            return self.escalate(task, "No ui_blueprint available — cannot generate pages")

        pages = bp.get("pages", [])[:8] if isinstance(bp, dict) else []
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Generate full pages from the UI blueprint.

PAGES: {json.dumps(pages, indent=2)}

Return ONLY JSON:
{{"files":[{{"path":"src/pages/Dashboard.tsx","language":"typescript","content":"export default function Dashboard() {{ return <div />; }}"}}],"pages_implemented":{json.dumps([p.get('name') for p in pages])},"quality_score":0.87}}"""},
        ], max_tokens=4096)

        content  = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task)
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.PAGE.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.PAGE.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"Pages: {len(files)} files ({len(pages)} pages)",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )
