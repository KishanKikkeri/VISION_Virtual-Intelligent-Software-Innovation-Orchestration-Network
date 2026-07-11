"""
services/engineering/base — shared base helpers for Engineering workers.
=============================================================================
Every concrete worker in services/engineering/workers inherits BaseAgent
(core/runtime/base_agent.py) directly, per the platform-wide rule. This
module supplies the additional pieces specific to Engineering: a common
`EngineeringWorkerMixin` with code-module creation + coding-contract
enforcement, so no worker hand-rolls artifact bookkeeping.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.contracts import AgentResult, TaskStatus
from core.runtime.context import ReviewCycle, TaskInput
from services.engineering.models import CodeFile, CodeModule, ModuleType
from services.engineering.schemas import CodingContractViolation
from services.engineering.utils import idempotency_key, parse_llm_json


class EngineeringWorkerMixin:
    """
    Mixin providing the common generate → review → package → artifact
    pipeline used by every Engineering worker. Concrete workers call
    `self.generate_module(...)` from inside `execute()`.
    """

    async def generate_module(
        self,
        task: TaskInput,
        module_type: ModuleType,
        messages: List[Dict[str, str]],
        review_schema: Optional[Dict[str, List[str]]] = None,
        max_tokens: int = 4096,
    ) -> AgentResult:
        raw, usage = await self.call_llm(task, messages, max_tokens=max_tokens)   # type: ignore[attr-defined]
        content = parse_llm_json(raw, {"files": [], "quality_score": 0.0})
        files = [CodeFile(**f) for f in content.get("files", [])]

        review = await ReviewCycle(self).run(
            content, task, schema=review_schema or {"item": ["path", "language", "content"]}
        )

        module = CodeModule(
            project_id=task.project_id,
            task_id=task.task_id,
            module_type=module_type,
            files=files,
            quality_score=review.final_score,
            generated_by=self.agent_id,                                          # type: ignore[attr-defined]
            review_passed=review.passed,
            idempotent_key=idempotency_key(task.project_id, task.task_id, self.agent_id),  # type: ignore[attr-defined]
        )

        violations = module.satisfies_coding_contract()
        if violations and not review.passed:
            return self.escalate(                                                # type: ignore[attr-defined]
                task,
                f"CodeModule failed coding contract: {violations}",
            )

        artifact = await self.create_artifact(                                   # type: ignore[attr-defined]
            task, "source_code",
            {"files": [f.model_dump() for f in files],
             "module_type": module_type.value,
             "project_id": task.project_id,
             "quality_score": review.final_score},
        )

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id,                        # type: ignore[attr-defined]
            status=TaskStatus.COMPLETED,
            content={**content, "module_id": module.module_id, "module_type": module_type.value},
            summary=f"Generated {len(files)} file(s) for {module_type.value}",
            quality_score=review.final_score,
            artifacts=[artifact],
            token_usage=usage,
        )
