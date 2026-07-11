"""services/engineering/workers/backend.py — Backend Lead's 4 L5 workers."""
from __future__ import annotations

import json

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import ReviewCycle, TaskInput
from core.runtime.factory import AgentFactory
from services.engineering.models import CodeFile, ModuleType
from services.engineering.utils import idempotency_key, parse_llm_json


def _tech(ctx) -> str:
    return ctx.tech_stack.get("backend", "Python + FastAPI")


@AgentFactory.register("database_layer_worker")
class DatabaseLayerWorker(BaseAgent):
    """Database Worker — ORM models + Alembic migration from database_schema."""

    async def execute(self, task: TaskInput) -> AgentResult:
        schema = task.context.get_artifact("database_schema", {})
        tables = [t.get("name") for t in schema.get("tables", [])[:8]] if isinstance(schema, dict) else []
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Generate SQLAlchemy 2 async ORM models and an Alembic migration for these tables.

TABLES: {json.dumps(tables)}
TECH: SQLAlchemy 2.0 async, PostgreSQL, UUID primary keys, TIMESTAMPTZ

Return ONLY JSON:
{{"files":[{{"path":"app/models/user.py","language":"python","content":"from sqlalchemy import String\\nfrom sqlalchemy.orm import Mapped, mapped_column\\nfrom app.db import Base\\n\\nclass User(Base):\\n    __tablename__ = 'users'\\n    id: Mapped[str] = mapped_column(String(36), primary_key=True)"}}],"tables_implemented":{json.dumps(tables[:5])},"quality_score":0.89}}"""},
        ], max_tokens=4096)

        content = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task,
                                                schema={"item": ["path", "language", "content"]})
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.DATABASE.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.DATABASE.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"Database layer: {len(files)} files ({len(tables)} tables)",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )


@AgentFactory.register("authentication_worker")
class AuthenticationWorker(BaseAgent):
    """Auth Worker — depends on database_layer_worker in the task graph."""

    async def execute(self, task: TaskInput) -> AgentResult:
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": """Generate JWT authentication implementation.

TECH: FastAPI, python-jose, passlib, PostgreSQL
Requirements: Register, Login, Refresh, Protected route middleware, RBAC

Return ONLY JSON:
{"files":[{"path":"app/auth/jwt.py","language":"python","content":"from jose import jwt\\nfrom passlib.context import CryptContext\\npwd_context = CryptContext(schemes=['bcrypt'])\\n\\ndef create_access_token(user_id: str) -> str:\\n    return jwt.encode({'sub':user_id}, 'secret', algorithm='HS256')"}}],"features_implemented":["register","login","refresh","middleware","rbac"],"quality_score":0.92}"""},
        ], max_tokens=4096)

        content = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task)
        if not review.passed:
            return self.escalate(task, "Auth implementation failed review — security-critical, no partial pass")
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.AUTH.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.AUTH.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"Auth implementation: {len(files)} files",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )


@AgentFactory.register("business_logic_worker")
class BusinessLogicWorker(BaseAgent):
    """Business Logic Worker — depends on database_layer_worker."""

    async def execute(self, task: TaskInput) -> AgentResult:
        stories = task.context.get_artifact("user_stories_doc", {}).get("user_stories", [])[:5]
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Generate core business logic services for these user stories.

USER STORIES: {json.dumps(stories, indent=2)}

Return ONLY JSON:
{{"files":[{{"path":"app/services/project_service.py","language":"python","content":"class ProjectService:\\n    def __init__(self, db): self.db = db\\n    async def create_project(self, user_id: str, name: str) -> dict:\\n        pass"}}],"services_implemented":["project_service","task_service"],"quality_score":0.88}}"""},
        ], max_tokens=4096)

        content  = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task)
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.BUSINESS_LOGIC.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.BUSINESS_LOGIC.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"Business logic: {len(files)} service files",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )


@AgentFactory.register("api_implementation_worker")
class ApiImplementationWorker(BaseAgent):
    """API Worker — depends on authentication_worker + business_logic_worker."""

    async def execute(self, task: TaskInput) -> AgentResult:
        spec  = task.context.get_artifact("openapi_spec", {})
        paths = list(spec.get("paths", {}).keys())[:10] if isinstance(spec, dict) else []
        tech  = _tech(task.context)
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Generate FastAPI endpoint handlers for these API paths.

PATHS: {json.dumps(paths)}
TECH: {tech}

Return ONLY JSON:
{{"files":[{{"path":"app/routers/health.py","language":"python","content":"from fastapi import APIRouter\\nrouter = APIRouter()\\n\\n@router.get('/health')\\nasync def health():\\n    return {{'status':'ok'}}"}}],"modules_implemented":{list(set(p.split('/')[3] if len(p.split('/'))>3 else 'core' for p in paths))},"quality_score":0.88}}"""},
        ], max_tokens=4096)

        content = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files    = [CodeFile(**f) for f in content.get("files", [])]
        review   = await ReviewCycle(self).run(content.get("files", []), task,
                                                schema={"item": ["path", "language", "content"]})
        artifact = await self.create_artifact(task, "source_code", {
            "files": [f.model_dump() for f in files], "module_type": ModuleType.API_ENDPOINT.value,
            "project_id": task.project_id, "quality_score": review.final_score,
        })
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={**content, "module_type": ModuleType.API_ENDPOINT.value,
                     "idempotent_key": idempotency_key(task.project_id, task.task_id, self.agent_id)},
            summary=f"Generated {len(files)} API endpoint files",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage,
        )
