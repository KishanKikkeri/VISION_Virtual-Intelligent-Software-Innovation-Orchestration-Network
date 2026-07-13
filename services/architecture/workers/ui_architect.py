"""
services/architecture/workers/ui_architect.py — UiArchitectWorker L5.

Appendix A (M3.3 prerequisite patch).
Produces the 10th Architecture artifact: `ui_blueprint`.
Registered under the existing Platform Design Lead — no new workflow
node is introduced; it simply joins the existing parallel fan-out
(infra + security + scaling + integration + ui).

ui_blueprint covers exactly the ten areas called for in the spec:
  Pages, Routes, Components, Layouts, Navigation,
  Forms, Tables, User flows, State boundaries, API bindings.

The artifact passes through the standard Architecture approval gate
alongside architecture_blueprint / api_spec / database_schema /
deployment_architecture.
"""
from __future__ import annotations
import json
from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import ReviewCycle, TaskInput
from core.runtime.factory import AgentFactory


def _parse(raw, fb=None):
    try:
        c = raw.strip()
        if c.startswith("```"):
            c = c.split("```")[1]
            c = c[4:] if c.startswith("json") else c
        return json.loads(c.strip())
    except Exception:
        return fb or {}


@AgentFactory.register("ui_architect_worker")
class UiArchitectWorker(BaseAgent):
    """Produces ui_blueprint: pages, routes, components, layouts, nav, forms, tables, flows, state, API bindings."""

    async def execute(self, task: TaskInput) -> AgentResult:
        ctx = task.context
        bp  = ctx.get_artifact("architecture_blueprint", {})
        api = ctx.get_artifact("api_spec", {})
        services = bp.get("services", []) if isinstance(bp, dict) else []
        paths = list(api.get("paths", {}).keys())[:15] if isinstance(api, dict) else []

        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Design the UI blueprint for this system.

SERVICES: {json.dumps([s.get("name") for s in services], indent=2)}
API ENDPOINTS: {json.dumps(paths, indent=2)}

Return ONLY JSON:
{{
  "pages": [{{"name":"Dashboard","route":"/dashboard","description":"..."}}],
  "routes": [{{"path":"/dashboard","page":"Dashboard","auth_required":true}}],
  "components": [{{"name":"NavBar","type":"layout","reusable":true}}],
  "layouts": [{{"name":"AppShell","regions":["header","sidebar","content"]}}],
  "navigation": {{"primary":[{{"label":"Dashboard","route":"/dashboard"}}],"structure":"sidebar"}},
  "forms": [{{"name":"LoginForm","fields":[{{"name":"email","type":"email","required":true}}]}}],
  "tables": [{{"name":"ProjectsTable","columns":["name","status","updated_at"],"sortable":true}}],
  "user_flows": [{{"name":"Onboarding","steps":["signup","verify_email","create_project"]}}],
  "state_boundaries": [{{"scope":"global","owner":"AuthStore","data":["user","token"]}}],
  "api_bindings": [{{"component":"ProjectsTable","endpoint":"/projects","method":"GET"}}],
  "quality_score": 0.0
}}"""},
        ], max_tokens=3500)

        content = _parse(raw, {"pages": [], "routes": [], "components": [], "quality_score": 0.0})
        review  = await ReviewCycle(self).run(
            content, task,
            schema={"root": ["pages", "routes", "components", "layouts", "navigation",
                              "forms", "tables", "user_flows", "state_boundaries", "api_bindings"]},
        )
        artifact = await self.create_artifact(task, "ui_blueprint", {**content, "project_id": task.project_id})
        await self.write_memory(
            task,
            f"UI blueprint: {len(content.get('pages', []))} pages, {len(content.get('components', []))} components",
            source="ui_architect",
        )
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content,
            summary=f"UI blueprint: {len(content.get('pages', []))} pages, {len(content.get('routes', []))} routes",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )
