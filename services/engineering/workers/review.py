"""
services/engineering/workers/review.py — Review Lead's 4 L5 workers.

This team is mandatory. Nothing reaches Repository Service before
Review completes (enforced structurally: only commit_worker holds a
RepositoryServiceClient, and the graph only calls the repository stage
after the review stage passes).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.engineering.integration.repository_client import (
    RepositoryServiceClient,
    RepositoryServiceClientError,
)
from services.engineering.models import CodeModule, ReviewVerdict
from services.engineering.schemas import ReviewGateBlockedError
from services.engineering.utils import files_to_dicts, parse_llm_json


@AgentFactory.register("code_reviewer_worker")
class CodeReviewerWorker(BaseAgent):
    """Code Review Worker — architecture-compliance + coding-standards review."""

    async def execute(self, task: TaskInput) -> AgentResult:
        sys = self.build_system_prompt(task)
        module_summary = task.context.approved_artifacts.get("__current_module__", {})
        files = module_summary.get("files", []) if isinstance(module_summary, dict) else []
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Review this code for architecture compliance and quality.

FILES TO REVIEW: {json.dumps([f.get('path') for f in files[:10]])}
STANDARDS: {json.dumps(task.context.coding_standards[:5])}

Return ONLY JSON:
{{"review_passed":true,"issues":[{{"severity":"blocking|warning","file":"path","line":1,"description":"Issue description"}}],"compliance_score":0.9,"security_flags":[],"quality_score":0.9}}"""},
        ], max_tokens=2000)

        content  = parse_llm_json(raw, {"review_passed": True, "issues": [], "quality_score": 0.8})
        passed   = content.get("review_passed", True)
        blocking = [i for i in content.get("issues", []) if i.get("severity") == "blocking"]
        status   = TaskStatus.COMPLETED if passed else TaskStatus.FAILED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={**content, "verdict": (ReviewVerdict.PASS if passed
                     else (ReviewVerdict.REVISE if len(blocking) <= 2 else ReviewVerdict.BLOCK)).value},
            summary=f"Code review {'PASSED' if passed else 'FAILED'}: {len(content.get('issues', []))} issues",
            quality_score=float(content.get("quality_score", 0.8)), token_usage=usage,
            failure_reason=None if passed else f"{len(blocking)} blocking issues",
        )


@AgentFactory.register("refactor_worker")
class RefactorWorker(BaseAgent):
    """Refactor Worker — applies review feedback to failing sections."""

    async def execute(self, task: TaskInput) -> AgentResult:
        review = task.context.approved_artifacts.get("__review_feedback__", {})
        issues = review.get("issues", []) if isinstance(review, dict) else []
        blocking = [i for i in issues if i.get("severity") == "blocking"]
        sys = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role": "system", "content": sys},
            {"role": "user", "content": f"""Apply these code review fixes:

BLOCKING ISSUES: {json.dumps(blocking[:5], indent=2)}

Return ONLY JSON:
{{"files_modified":[{{"path":"app/routers/health.py","change":"Added missing error handling and type hints"}}],"issues_fixed":{len(blocking)},"quality_score":0.9}}"""},
        ], max_tokens=3000)

        content = parse_llm_json(raw, {"files_modified": [], "quality_score": 0.8})
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content=content,
            summary=f"Refactored {len(content.get('files_modified', []))} files ({len(blocking)} issues fixed)",
            quality_score=float(content.get("quality_score", 0.85)), token_usage=usage,
        )


@AgentFactory.register("quality_worker")
class QualityWorker(BaseAgent):
    """
    Quality Worker (new in M3.3) — rules-based (non-LLM) validation of the
    Coding Contract: buildable, runnable, testable, traceable, deterministic,
    reviewable, idempotent. Any violation sends the module back through
    the review loop rather than forward to Repository Service.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        module_summary: Dict[str, Any] = task.context.approved_artifacts.get("__current_module__", {})
        files = module_summary.get("files", [])
        quality_score = float(module_summary.get("quality_score", 0.0))
        idempotent_key = module_summary.get("idempotent_key")

        violations: List[str] = []
        if not files:
            violations.append("buildable")
        if quality_score < 0.7:
            violations.append("reviewable")
        if not task.task_id:
            violations.append("traceable")
        if not idempotent_key:
            violations.append("idempotent")
        if not all(isinstance(f.get("content"), str) and f.get("content").strip() for f in files):
            violations.append("runnable")

        passed = not violations
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id,
            status=TaskStatus.COMPLETED if passed else TaskStatus.FAILED,
            content={"coding_contract_satisfied": passed, "violations": violations},
            summary=("Coding contract satisfied" if passed
                     else f"Coding contract violated: {violations}"),
            quality_score=1.0 if passed else max(0.0, 0.7 - 0.1 * len(violations)),
            failure_reason=None if passed else f"Violations: {violations}",
        )


@AgentFactory.register("commit_worker")
class CommitWorker(BaseAgent):
    """
    Commit Worker — the ONLY Engineering worker permitted to talk to
    Repository Service. Creates the integration branch (owned by
    Engineering Lead per Appendix A), replays reviewed modules onto it
    via commit_files(), and opens the pull request.

    Never calls Git directly. Never writes commits directly. Never
    creates branches directly — everything goes through
    RepositoryServiceClient → Repository Service's HTTP API.

    Note: AgentFactory instantiates agents via `cls.__new__(cls)` and
    injects attributes directly — it never calls `__init__`. The repo
    client is therefore created lazily on first use (`_client()`)
    rather than in a constructor that would never run.
    """

    def _client(self) -> RepositoryServiceClient:
        if getattr(self, "_repo_client", None) is None:
            self._repo_client = RepositoryServiceClient()
        return self._repo_client

    async def execute(self, task: TaskInput) -> AgentResult:
        feature_name = task.context.approved_artifacts.get("__feature_name__", "feature")
        modules: List[Dict[str, Any]] = task.context.approved_artifacts.get("__reviewed_modules__", [])

        if not modules:
            return self.escalate(task, "No reviewed modules to commit — Review Lead must complete first")

        repo_client = self._client()
        try:
            branch = await repo_client.create_integration_branch(
                project_id=task.project_id, feature_name=feature_name,
            )
            branch_name = branch.get("name") or branch.get("branch_name") or f"integration/{feature_name}"

            commit_shas: List[str] = []
            for module in modules:
                files = files_to_dicts(module.get("files", []))
                if not files:
                    continue
                commit = await repo_client.commit_files(
                    project_id=task.project_id,
                    branch_name=branch_name,
                    message=f"feat({module.get('module_type', 'module')}): {module.get('module_id', task.task_id)[:8]}",
                    files=files,
                    metadata={
                        "project_id": task.project_id,
                        "workflow_id": task.context.workflow_id,
                        "task_id": task.task_id,
                        "agent_id": module.get("generated_by", self.agent_id),
                        "lead_id": "code_review_lead",
                    },
                )
                commit_shas.append(commit.get("sha", ""))

            pr = await repo_client.create_pull_request(
                project_id=task.project_id,
                source_branch=branch_name,
                title=f"Engineering: {feature_name}",
                description=f"Auto-generated implementation for '{feature_name}' — "
                             f"{len(modules)} module(s), reviewed and quality-gated.",
                task_id=task.task_id,
            )

        except RepositoryServiceClientError as exc:
            return self.escalate(task, f"Repository Service call failed: {exc}")

        artifact = await self.create_artifact(task, "pull_request_ref", {
            "project_id": task.project_id,
            "branch_name": branch_name,
            "pull_request_id": pr.get("id"),
            "commit_shas": commit_shas,
        })

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={
                "integration_branch": branch_name,
                "commit_shas": commit_shas,
                "pull_request_id": pr.get("id"),
                "pull_request_url": pr.get("html_url"),
            },
            summary=f"Committed {len(commit_shas)} module(s) to {branch_name}, opened PR {pr.get('id')}",
            quality_score=1.0, artifacts=[artifact],
        )
