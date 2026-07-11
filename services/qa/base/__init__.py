"""
services/qa/base — shared base helpers for QA test-generation workers.
==========================================================================
Every concrete worker in services/qa/workers inherits BaseAgent
(core/runtime/base_agent.py) directly, per the platform-wide rule. This
module supplies the additional piece specific to QA: a common
`QAWorkerMixin` with test-suite creation + test-contract enforcement,
mirroring services/engineering/base's EngineeringWorkerMixin.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.contracts import AgentResult, TaskStatus
from core.runtime.context import ReviewCycle, TaskInput
from services.qa.models import SuiteType, TestFile, TestSuite
from services.qa.utils import idempotency_key, parse_llm_json


class QAWorkerMixin:
    """
    Mixin providing the common generate -> review -> package -> artifact
    pipeline used by every QA test-generation worker. Concrete workers
    call `self.generate_suite(...)` from inside `execute()`.
    """

    async def generate_suite(
        self,
        task: TaskInput,
        suite_type: SuiteType,
        messages: List[Dict[str, str]],
        artifact_type: str,
        review_schema: Optional[Dict[str, List[str]]] = None,
        max_tokens: int = 4096,
        extra_content: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        raw, usage = await self.call_llm(task, messages, max_tokens=max_tokens)  # type: ignore[attr-defined]
        content = parse_llm_json(raw, {"files": [], "test_count": 0, "quality_score": 0.0})
        files = [TestFile(**f) for f in content.get("files", [])]
        test_count = content.get("test_count", len(files))

        review = await ReviewCycle(self).run(
            content, task, schema=review_schema or {"item": ["path", "language", "content"]}
        )

        suite = TestSuite(
            project_id=task.project_id,
            task_id=task.task_id,
            suite_type=suite_type,
            files=files,
            test_count=test_count,
            quality_score=review.final_score,
            generated_by=self.agent_id,  # type: ignore[attr-defined]
            idempotent_key=idempotency_key(task.project_id, task.task_id, self.agent_id),  # type: ignore[attr-defined]
        )

        violations = suite.satisfies_test_contract()
        if violations and not review.passed:
            return self.escalate(  # type: ignore[attr-defined]
                task, f"TestSuite failed test contract: {violations}",
            )

        artifact = await self.create_artifact(  # type: ignore[attr-defined]
            task, artifact_type,
            {"files": [f.model_dump() for f in files], "suite_type": suite_type.value,
             "test_count": test_count, "project_id": task.project_id,
             "quality_score": review.final_score},
        )

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id,  # type: ignore[attr-defined]
            status=TaskStatus.COMPLETED,
            content={**content, **(extra_content or {}), "suite_id": suite.suite_id,
                     "suite_type": suite_type.value, "test_count": test_count},
            summary=f"Generated {test_count} test(s) in {len(files)} file(s) for {suite_type.value}",
            quality_score=review.final_score,
            artifacts=[artifact],
            token_usage=usage,
        )
