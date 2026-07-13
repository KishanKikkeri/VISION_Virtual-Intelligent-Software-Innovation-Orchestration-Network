"""
services/security/base — shared base helpers for LLM-driven Security
scan workers.
=========================================================================
Every concrete worker in services/security/workers inherits BaseAgent
(core/runtime/base_agent.py) directly, per the platform-wide rule. This
module supplies the additional piece specific to Security: a common
`SecurityWorkerMixin` for the LLM-driven static-analysis workers
(owasp_checker_worker, injection_check_worker), mirroring
services/qa/base's QAWorkerMixin. Deterministic workers (cve_scanner,
secret_scanner, compliance_validator) do not need this mixin — they
call create_artifact() directly, the same way QA's CoverageAnalyzerWorker
and RegressionSuiteWorker skip QAWorkerMixin.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.contracts import AgentResult, TaskStatus
from core.runtime.context import ReviewCycle, TaskInput
from services.security.models import CodeIssue, FindingSeverity
from services.security.utils import idempotency_key, parse_llm_json


class SecurityScanWorkerMixin:
    """
    Mixin providing the common generate -> review -> package -> artifact
    pipeline used by LLM-driven Security static-analysis workers.
    Concrete workers call `self.generate_findings(...)` from inside
    `execute()`.
    """

    async def generate_findings(
        self,
        task: TaskInput,
        messages: List[Dict[str, str]],
        artifact_type: str,
        result_key: str,
        review_schema: Optional[Dict[str, List[str]]] = None,
        max_tokens: int = 4096,
        extra_content: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        raw, usage = await self.call_llm(task, messages, max_tokens=max_tokens)  # type: ignore[attr-defined]
        content = parse_llm_json(raw, {"findings": [], "files_scanned": 0, "quality_score": 0.0})
        findings = [CodeIssue(**f) for f in content.get("findings", [])]

        review = await ReviewCycle(self).run(
            content, task, schema=review_schema or {"item": ["rule", "severity"]}
        )

        has_critical = any(f.severity == FindingSeverity.CRITICAL for f in findings)

        artifact = await self.create_artifact(  # type: ignore[attr-defined]
            task, artifact_type,
            {result_key: [f.model_dump() for f in findings],
             "files_scanned": content.get("files_scanned", 0),
             "project_id": task.project_id,
             "quality_score": review.final_score},
        )

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id,  # type: ignore[attr-defined]
            status=TaskStatus.COMPLETED,
            content={**content, **(extra_content or {}), result_key: [f.model_dump() for f in findings],
                     "finding_count": len(findings), "idempotent_key": idempotency_key(
                         task.project_id, task.task_id, self.agent_id)},  # type: ignore[attr-defined]
            summary=f"Found {len(findings)} issue(s) across {content.get('files_scanned', 0)} file(s)"
                    + (" — CRITICAL present" if has_critical else ""),
            quality_score=review.final_score,
            artifacts=[artifact],
            token_usage=usage,
        )
